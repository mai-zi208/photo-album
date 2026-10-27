"""本地语义模型（Chinese-CLIP ONNX）按需下载安装。

首次点击语义检索 / 智能场景时由前端询问用户，同意后在这里后台静默下载：
模型取自 HuggingFace 上的 Xenova/chinese-clip-vit-base-patch16（中文 CLIP 的
ONNX 量化版，约 190MB），优先走 hf-mirror.com 国内镜像，失败再回退官方域名。
文件落在 <DATA_DIR>/models/chinese_clip_onnx，装好后永久复用，不再询问。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
from pathlib import Path

from . import config

REPO = "Xenova/chinese-clip-vit-base-patch16"
MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")
FILES = (
    "config.json",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "onnx/model_quantized.onnx",
)
CHUNK = 1 << 18

PROGRESS = {
    "running": False,
    "present": False,
    "downloaded": 0,
    "total": 0,
    "file": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_lock = threading.Lock()
_UA = "photo-album/1.0 (+local desktop app)"


def _present() -> bool:
    from . import semantic
    return semantic.layout() != "none"


def status() -> dict:
    with _lock:
        snap = dict(PROGRESS)
    snap["present"] = _present()
    if snap["present"]:
        snap["running"] = False
        snap["error"] = None
    return snap


def _size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return int(r.headers.get("Content-Length") or 0)


def _fetch(rel: str, dest: Path) -> None:
    """下载单个文件；多镜像依次尝试，先写 .part 再原子替换。"""
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for mirror in MIRRORS:
        url = f"{mirror}/{REPO}/resolve/main/{rel}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as r, \
                    open(tmp, "wb") as f:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    with _lock:
                        PROGRESS["downloaded"] += len(chunk)
            os.replace(tmp, dest)
            return
        except Exception as e:      # 镜像不可用就换下一个
            last = e
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    raise RuntimeError(f"{rel}: {last}")


def _run() -> None:
    from . import scenes, semantic
    root = config.SEM_MODEL_DIR
    try:
        (root / "onnx").mkdir(parents=True, exist_ok=True)
        total = 0
        for rel in FILES:
            for mirror in MIRRORS:
                try:
                    total += _size(f"{mirror}/{REPO}/resolve/main/{rel}")
                    break
                except Exception:
                    continue
        with _lock:
            PROGRESS["total"] = total
        for rel in FILES:
            with _lock:
                PROGRESS["file"] = rel
            _fetch(rel, root / rel)
        # 装好即预热并自动开始向量回填 / 主题分析
        try:
            semantic.load()
            semantic.start_backfill()
        except Exception:
            pass
        try:
            scenes.start_backfill()
        except Exception:
            pass
        with _lock:
            PROGRESS.update({"running": False, "error": None,
                             "finished_at": time.time(), "file": ""})
    except Exception as e:
        with _lock:
            PROGRESS.update({"running": False, "error": str(e),
                             "finished_at": time.time()})


def install() -> dict:
    """启动后台下载（幂等：已在下载或已装好时直接返回状态）。"""
    with _lock:
        if PROGRESS["running"]:
            return {"ok": True, "running": True}
    if _present():
        return {"ok": True, "present": True}
    with _lock:
        PROGRESS.update({
            "running": True, "present": False, "downloaded": 0, "total": 0,
            "file": "", "error": None, "started_at": time.time(),
            "finished_at": None,
        })
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "running": True}
