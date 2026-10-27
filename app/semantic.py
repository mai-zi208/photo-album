"""Slow channel #3: semantic embeddings (Chinese-CLIP ViT-B/16, ONNX int8).

Dual-tower model: images and Chinese text share one 512-d cosine space, so
natural-language queries ("猫", "红色汽车") and image-to-image similarity both
reduce to a dot product. Vectors live in sem_vec (float32 blob, L2-normalised);
at our scale (30k x 512 ~= 60MB) brute-force numpy beats any ANN index.

The encoders are lazily loaded (first use costs a couple of seconds) and the
backfill worker mirrors the OCR lane: newest-first, resumable, low priority.
"""
from __future__ import annotations

import threading
import time

import numpy as np
from PIL import Image, ImageOps

from .config import SEM_MODEL_DIR
from .db import get_conn

DIM = 512
TEXT_MAX_LEN = 52          # Chinese-CLIP context window

_vision = _text = _tok = _ip = None
_clip = None               # 单图模型（vision+text 在同一张 onnx 里）
_clip_names: dict[str, int] = {}
_dummy: dict = {}          # 单图模型跑单塔时给另一塔喂的占位输入
_load_lock = threading.Lock()

_progress_lock = threading.Lock()
PROGRESS = {
    "running": False,
    "done": 0,
    "failed": 0,
    "pending_at_start": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "model_loaded": False,
}
_stop = threading.Event()


def _single_path():
    onnx_dir = SEM_MODEL_DIR / "onnx"
    for name in ("model_quantized.onnx", "model.onnx"):
        p = onnx_dir / name
        if p.exists():
            return p
    return None


def layout() -> str:
    """dual（拆开的双塔）/ single（一整张 onnx）/ none（未安装）。"""
    onnx_dir = SEM_MODEL_DIR / "onnx"
    if (onnx_dir / "vision.onnx").exists() and (onnx_dir / "text.onnx").exists():
        return "dual"
    return "single" if _single_path() else "none"


def model_present() -> bool:
    return layout() != "none"


def load():
    """Create ORT sessions + HF preprocessors once (thread-safe)."""
    global _vision, _text, _clip, _clip_names, _tok, _ip
    with _load_lock:
        if _tok is None or _ip is None:
            from transformers import AutoImageProcessor, AutoTokenizer
            _tok = AutoTokenizer.from_pretrained(str(SEM_MODEL_DIR))
            _ip = AutoImageProcessor.from_pretrained(str(SEM_MODEL_DIR))
        if layout() == "dual":
            if _vision is None:
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.log_severity_level = 3
                _vision = ort.InferenceSession(
                    str(SEM_MODEL_DIR / "onnx" / "vision.onnx"), opts,
                    providers=["CPUExecutionProvider"], intra_op_num_threads=4)
                _text = ort.InferenceSession(
                    str(SEM_MODEL_DIR / "onnx" / "text.onnx"), opts,
                    providers=["CPUExecutionProvider"], intra_op_num_threads=4)
        elif _clip is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            _clip = ort.InferenceSession(
                str(_single_path()), opts,
                providers=["CPUExecutionProvider"], intra_op_num_threads=4)
            _clip_names = {o.name: i for i, o in enumerate(_clip.get_outputs())}
        with _progress_lock:
            PROGRESS["model_loaded"] = True
    return _vision, _text


def _l2(mat: np.ndarray) -> np.ndarray:
    return mat / np.maximum(np.linalg.norm(mat, axis=-1, keepdims=True), 1e-9)


def _pick(outs: list, name: str) -> np.ndarray:
    """按输出名取结果（兼容不同导出时的命名）。"""
    idx = _clip_names.get(name)
    if idx is None:
        for alt in (name.replace("_", ""), name.replace("embeds", "features"),
                    name.replace("embeds", "embeddings")):
            if alt in _clip_names:
                idx = _clip_names[alt]
                break
    if idx is None:
        raise RuntimeError(f"unexpected onnx outputs: {list(_clip_names)}")
    return outs[idx]


def _dummy_text():
    """单图模型里跑图像塔时，给文本塔喂一句占位文本。"""
    if "ids" not in _dummy:
        enc = _tok(text=["图片"], padding="max_length", truncation=True,
                   max_length=TEXT_MAX_LEN, return_tensors="np")
        _dummy["ids"] = enc["input_ids"].astype(np.int64)
        _dummy["mask"] = enc["attention_mask"].astype(np.int64)
    return _dummy["ids"], _dummy["mask"]


def _dummy_image():
    """单图模型里跑文本塔时，给图像塔喂一张占位白图。"""
    if "px" not in _dummy:
        img = Image.new("RGB", (224, 224), "white")
        _dummy["px"] = _ip(images=img, return_tensors="np")["pixel_values"]
    return _dummy["px"]


def embed_image(img: Image.Image) -> np.ndarray:
    load()
    px = _ip(images=img.convert("RGB"), return_tensors="np")["pixel_values"]
    if layout() == "dual":
        out = _vision.run(None, {"pixel_values": px.astype(np.float32)})[0]
        return _l2(out)[0]
    ids, mask = _dummy_text()
    outs = _clip.run(None, {"pixel_values": px.astype(np.float32),
                            "input_ids": ids, "attention_mask": mask})
    return _l2(_pick(outs, "image_embeds"))[0]


def embed_texts(texts: list[str]) -> np.ndarray:
    load()
    enc = _tok(text=texts, padding="max_length", truncation=True,
               max_length=TEXT_MAX_LEN, return_tensors="np")
    if layout() == "dual":
        out = _text.run(None, {
            "input_ids": enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64)})[0]
        return _l2(out)
    outs = _clip.run(None, {
        "pixel_values": _dummy_image(),
        "input_ids": enc["input_ids"].astype(np.int64),
        "attention_mask": enc["attention_mask"].astype(np.int64)})
    return _l2(_pick(outs, "text_embeds"))


def embed_file(path: str) -> np.ndarray:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        return embed_image(img)


# ---------------------------------------------------------------------------
# vector store (brute-force numpy; rows are pre-normalised)
# ---------------------------------------------------------------------------

_cache = {"ver": -1, "ids": [], "mat": None}
_cache_lock = threading.Lock()
_version = 0


def _bump_version() -> None:
    global _version
    _version += 1


def _matrix() -> tuple[list[str], np.ndarray]:
    with _cache_lock:
        if _cache["ver"] != _version:
            rows = get_conn().execute(
                """SELECT s.id AS id, s.vec AS vec FROM sem_vec s
                   JOIN photos p ON p.id = s.id
                   WHERE p.status != 'missing' AND s.dim = ?""",
                (DIM,)).fetchall()
            ids = [r["id"] for r in rows]
            mat = (np.stack([np.frombuffer(r["vec"], dtype=np.float32)
                             for r in rows]) if rows
                   else np.zeros((0, DIM), dtype=np.float32))
            _cache.update(ver=_version, ids=ids, mat=mat)
        return _cache["ids"], _cache["mat"]


def search_vec(query: np.ndarray, k: int,
               exclude_id: str | None = None) -> list[tuple[str, float]]:
    ids, mat = _matrix()
    if not len(ids):
        return []
    scores = mat @ query
    if exclude_id is not None and exclude_id in ids:
        scores[ids.index(exclude_id)] = -9.0
    k = min(k, len(ids))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(ids[i], float(scores[i])) for i in top]


def search_text(q: str, k: int = 100) -> list[tuple[str, float]]:
    return search_vec(embed_texts([q])[0], k)


def similar_to(file_id: str, k: int = 24) -> list[tuple[str, float]]:
    ids, mat = _matrix()
    if file_id in ids:
        query = mat[ids.index(file_id)]
    else:  # not embedded yet: compute on the fly from the file
        row = get_conn().execute(
            "SELECT path FROM photos WHERE id=?", (file_id,)).fetchone()
        if row is None:
            return []
        query = embed_file(row["path"])
    return search_vec(query, k + 1, exclude_id=file_id)[:k]


def save_vec(file_id: str, vec: np.ndarray) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO sem_vec(id, dim, vec) VALUES(?,?,?)",
        (file_id, DIM, vec.astype(np.float32).tobytes()))
    conn.execute("UPDATE photos SET sem_status='done' WHERE id=?", (file_id,))
    conn.commit()
    _bump_version()


# ---------------------------------------------------------------------------
# backfill worker (mirrors the OCR lane)
# ---------------------------------------------------------------------------


def _bump(**kw) -> None:
    with _progress_lock:
        for k, v in kw.items():
            PROGRESS[k] = PROGRESS.get(k, 0) + v


def progress_snapshot() -> dict:
    with _progress_lock:
        snap = dict(PROGRESS)
    if snap["running"] and snap["started_at"]:
        el = time.time() - snap["started_at"]
        snap["elapsed"] = round(el, 1)
        snap["rate_per_sec"] = round(snap["done"] / max(el, 0.001), 2)
    conn = get_conn()
    snap["pending"] = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE sem_status='pending' AND status='ok'"
    ).fetchone()["c"]
    snap["model_present"] = model_present()
    return snap


def claim_pending() -> tuple[str, str] | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT id, path FROM photos
           WHERE sem_status='pending' AND status='ok'
           ORDER BY taken_at DESC LIMIT 1""").fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE photos SET sem_status='running' WHERE id=? AND sem_status='pending'",
        (row["id"],))
    conn.commit()
    if cur.rowcount == 0:
        return claim_pending()
    return row["id"], row["path"]


def _run() -> None:
    try:
        load()
        while not _stop.is_set():
            claim = claim_pending()
            if claim is None:
                break
            file_id, path = claim
            try:
                save_vec(file_id, embed_file(path))
                _bump(done=1)
            except Exception:
                conn = get_conn()
                conn.execute(
                    "UPDATE photos SET sem_status='failed' WHERE id=?", (file_id,))
                conn.commit()
                _bump(failed=1)

    except Exception as e:
        with _progress_lock:
            PROGRESS["error"] = f"{type(e).__name__}: {e}"
    finally:
        get_conn().execute(
            "UPDATE photos SET sem_status='pending' WHERE sem_status='running'")
        get_conn().commit()
        with _progress_lock:
            PROGRESS["running"] = False
            PROGRESS["finished_at"] = time.time()


def start_backfill() -> dict:
    if not model_present():
        return {"started": False,
                "reason": "semantic model not installed (.data/models/chinese_clip_onnx)"}
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "semantic already running"}
        conn = get_conn()
        conn.execute(
            "UPDATE photos SET sem_status='pending' WHERE sem_status='running'")
        conn.commit()
        pending = conn.execute(
            "SELECT COUNT(*) c FROM photos WHERE sem_status='pending' AND status='ok'"
        ).fetchone()["c"]
        if pending == 0:
            return {"started": False, "reason": "nothing pending"}
        PROGRESS.update(running=True, done=0, failed=0,
                        pending_at_start=pending, started_at=time.time(),
                        finished_at=None, error=None)
        _stop.clear()
    threading.Thread(target=_run, daemon=True, name="semantic-worker").start()
    return {"started": True, "pending": pending}


def stop_backfill() -> dict:
    _stop.set()
    return {"stopping": True}
