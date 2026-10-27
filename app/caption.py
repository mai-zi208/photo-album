"""Cloud lane: VLM captions via any OpenAI-compatible chat/completions API.

Provider-agnostic: base_url / model / api_key are user-supplied in
.data/caption_config.json (local file only, never in the DB, never echoed
whole to the UI). The default values point at DashScope compatible-mode as a
convenience; any OpenAI-compatible gateway works by editing the config.

Privacy by construction (product rule: 云端逐项开关，截图默认不外传):
- the lane is dead until the user pastes a key AND flips `enabled`;
- screenshots are never sent unless `allow_screenshots` is also on;
- only the 512px WebP thumbnail leaves the machine (base64 data URL),
  never the original file;
- existing photos are never backfilled automatically: the queue only takes
  rows explicitly marked pending (new ingested photos, or the detail-card
  button). caption_status defaults to 'off' for everything older.
"""
from __future__ import annotations

import base64
import json
import threading
import time

import httpx

from .config import DATA_DIR, thumb_path
from .db import get_conn

CONFIG_PATH = DATA_DIR / "caption_config.json"
DEFAULTS = {
    "enabled": False,
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "",
    "model": "qwen3-vl-plus",
    "allow_screenshots": False,
    "prompt": "用一两句中文描述这张照片的主体、场景与氛围。"
              "只描述能确定的内容，不要臆测文字和身份。直接输出描述本身。",
}

_lock = threading.Lock()
_progress_lock = threading.Lock()
PROGRESS = {
    "running": False,
    "done": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}
_stop = threading.Event()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def public_config() -> dict:
    """Config for the UI: the key never leaves the server whole."""
    cfg = load_config()
    key = cfg.get("api_key") or ""
    cfg["api_key"] = ("*" * 8 + key[-4:]) if key else ""
    cfg["has_key"] = bool(key)
    return cfg


def save_config(patch: dict) -> dict:
    cfg = load_config()
    for k in DEFAULTS:
        if k in patch:
            cfg[k] = patch[k]
    if not patch.get("api_key"):       # masked echo means "keep old key"
        cfg["api_key"] = load_config()["api_key"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return public_config()


def configured() -> bool:
    cfg = load_config()
    return bool(cfg["enabled"] and cfg["api_key"] and cfg["base_url"])


# ---------------------------------------------------------------------------
# the actual call
# ---------------------------------------------------------------------------


def generate_now(file_id: str, force: bool = False) -> dict:
    """Synchronous single-image caption (detail-card button).

    force=True skips the screenshot policy for this one image (the UI offers
    '这张也要'); a missing key still blocks everything."""
    cfg = load_config()
    if not (cfg["enabled"] and cfg["api_key"]):
        return {"ok": False, "reason": "云端未启用或未配置 API key"}
    row = get_conn().execute(
        "SELECT kind, path FROM photos WHERE id=?", (file_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "unknown photo"}
    if row["kind"] == "screenshot" and not cfg["allow_screenshots"] and not force:
        return {"ok": False, "reason": "截图默认不外传（可在云端设置中逐项允许）"}
    tp = thumb_path(file_id, 512)
    if not tp.exists():
        return {"ok": False, "reason": "没有可用缩略图"}
    conn = get_conn()
    conn.execute("UPDATE photos SET caption_status='running' WHERE id=?",
                 (file_id,))
    conn.commit()
    try:
        data_url = "data:image/webp;base64," + base64.b64encode(
            tp.read_bytes()).decode()
        text = _call_api(cfg, data_url)
        conn.execute(
            "UPDATE photos SET caption=?, caption_status='done' WHERE id=?",
            (text, file_id))
        conn.commit()
        return {"ok": True, "caption": text}
    except Exception as e:
        conn.execute("UPDATE photos SET caption_status='failed' WHERE id=?",
                     (file_id,))
        conn.commit()
        with _progress_lock:
            PROGRESS["last_error"] = f"{type(e).__name__}: {e}"
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def _call_api(cfg: dict, data_url: str) -> str:
    """One OpenAI-compatible chat/completions request; text out."""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": cfg["prompt"]},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    res = httpx.post(url, json=body, timeout=90.0, headers={
        "Authorization": f"Bearer {cfg['api_key']}"})
    res.raise_for_status()
    msg = res.json()["choices"][0]["message"]["content"]
    if isinstance(msg, list):  # some gateways return content parts
        msg = "".join(p.get("text", "") for p in msg)
    return (msg or "").strip()[:400]


# ---------------------------------------------------------------------------
# queue for newly ingested photos (no backfill of old rows, by design)
# ---------------------------------------------------------------------------


def on_ingested(file_id: str, kind: str) -> None:
    """Mark a brand-new row pending if the cloud lane is on for its type."""
    if kind == "video":  # the cloud lane speaks still images
        return
    cfg = load_config()
    if not (cfg["enabled"] and cfg["api_key"]):
        return
    if kind == "screenshot" and not cfg["allow_screenshots"]:
        return
    conn = get_conn()
    conn.execute("UPDATE photos SET caption_status='pending' WHERE id=?",
                 (file_id,))
    conn.commit()


def claim_pending() -> str | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT id FROM photos
           WHERE caption_status='pending' AND status='ok'
           ORDER BY taken_at DESC LIMIT 1""").fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE photos SET caption_status='running' WHERE id=?"
        " AND caption_status='pending'", (row["id"],))
    conn.commit()
    if cur.rowcount == 0:
        return claim_pending()
    return row["id"]


def _bump(**kw) -> None:
    with _progress_lock:
        for k, v in kw.items():
            PROGRESS[k] = PROGRESS.get(k, 0) + v


def _run() -> None:
    try:
        while not _stop.is_set():
            file_id = claim_pending()
            if file_id is None:
                break
            r = generate_now(file_id)
            _bump(done=1 if r["ok"] else 0, failed=0 if r["ok"] else 1)
    finally:
        get_conn().execute(
            "UPDATE photos SET caption_status='pending'"
            " WHERE caption_status='running'")
        get_conn().commit()
        with _progress_lock:
            PROGRESS["running"] = False
            PROGRESS["finished_at"] = time.time()


def start() -> dict:
    if not configured():
        return {"started": False, "reason": "cloud lane off / not configured"}
    conn = get_conn()
    conn.execute("UPDATE photos SET caption_status='pending'"
                 " WHERE caption_status='running'")
    conn.commit()
    pending = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE caption_status='pending'"
        " AND status='ok'").fetchone()["c"]
    if pending == 0:
        return {"started": False, "reason": "nothing pending"}
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "caption already running"}
        PROGRESS.update(running=True, done=0, failed=0,
                        started_at=time.time(), finished_at=None)
        _stop.clear()
    threading.Thread(target=_run, daemon=True, name="caption-worker").start()
    return {"started": True, "pending": pending}


def stop() -> dict:
    _stop.set()
    return {"stopping": True}


def progress_snapshot() -> dict:
    with _progress_lock:
        snap = dict(PROGRESS)
    snap["configured"] = configured()
    conn = get_conn()
    snap["pending"] = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE caption_status='pending'"
        " AND status='ok'").fetchone()["c"]
    return snap
