"""Scan pipeline: walk a folder, content-hash each image, read metadata,
build two-level WebP thumbnails, and register everything in SQLite.

Fast channel first: metadata + thumbnails make the library browsable within
minutes; heavy analysis (OCR etc.) will hang off this same job queue later.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import blake3
from PIL import Image, ImageOps

try:  # HEIC/HEIF support via plugin; optional on some installs
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

from . import caption, dups, media, places
from .config import (MEDIA_EXTS, VIDEO_EXTS, is_ignored_dir_name,
                     is_ignored_path, known_folders, thumb_path)
from .db import (demote_previous_at_path, get_conn, reconcile_existing,
                 retry_locked, sweep_missing, upsert_photo)

SCREENSHOT_EXTS = {".png", ".bmp"}
PLACEHOLDER_KIND = "other"

# ---------------------------------------------------------------------------
# progress bookkeeping (single background job at a time in the MVP)
# ---------------------------------------------------------------------------

_progress_lock = threading.Lock()
PROGRESS = {
    "running": False,
    "root": None,
    "seen": 0,
    "indexed": 0,
    "duplicated": 0,
    "broken": 0,
    "skipped": 0,
    "swept_missing": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _update(**kw) -> None:
    with _progress_lock:
        PROGRESS.update(kw)


def bump(**kw) -> None:
    """Increment counters used by the worker."""
    with _progress_lock:
        for k, v in kw.items():
            PROGRESS[k] = PROGRESS.get(k, 0) + v


def progress_snapshot() -> dict:
    with _progress_lock:
        snap = dict(PROGRESS)
    if snap["running"] and snap["started_at"]:
        snap["elapsed"] = round(time.time() - snap["started_at"], 1)
        rate = snap["seen"] / max(snap["elapsed"], 0.001)
        snap["rate_per_sec"] = round(rate, 1)
    return snap


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def hash_file(path: str) -> str:
    hasher = blake3.blake3()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _exif_taken_at(img: Image.Image) -> float | None:
    """Prefer DateTimeOriginal from the Exif IFD, fall back to DateTime."""
    try:
        exif = img.getexif()
        value = None
        try:
            ifd = exif.get_ifd(0x8769)
            value = ifd.get(36867) or ifd.get(306)
        except Exception:
            pass
        value = value or exif.get(306)
        if value:
            dt = datetime.strptime(str(value)[:19], "%Y:%m:%d %H:%M:%S")
            return dt.timestamp()
    except Exception:
        return None
    return None


def classify(ext: str, has_exif_time: bool, width: int, height: int) -> str:
    """Cheap, honest first pass at screenshot vs photo (no ML yet)."""
    if ext in SCREENSHOT_EXTS and not has_exif_time:
        return "screenshot"
    if has_exif_time:
        return "photo"
    if ext in {".jpg", ".jpeg", ".heic", ".heif"}:
        return "photo"
    return PLACEHOLDER_KIND


def build_thumbs(file_id: str, img: Image.Image) -> None:
    """Generate 512px WebP; the 128px grid thumb derives from it (cheap)."""
    base = ImageOps.exif_transpose(img)
    if base.mode in ("P", "RGBA", "LA"):
        bg = Image.new("RGB", base.size, (28, 28, 30))
        rgba = base.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        base = bg
    elif base.mode != "RGB":
        base = base.convert("RGB")
    big = thumb_path(file_id, 512)
    small = thumb_path(file_id, 128)
    if big.exists() and small.exists():
        return
    big.parent.mkdir(parents=True, exist_ok=True)
    thumb = base.copy()
    thumb.thumbnail((512, 512), Image.Resampling.LANCZOS)
    if not big.exists():
        tmp = _tmp_beside(big)
        thumb.save(tmp, "WEBP", quality=75, method=4)
        os.replace(tmp, big)
    if not small.exists():
        small_thumb = thumb.copy()
        small_thumb.thumbnail((128, 128), Image.Resampling.BILINEAR)
        tmp = _tmp_beside(small)
        small_thumb.save(tmp, "WEBP", quality=70, method=6)
        os.replace(tmp, small)


def _tmp_beside(target: Path) -> Path:
    """同目录临时文件名带线程号：同一张图被并发处理时不会互相抢 tmp。"""
    return target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")


# ---------------------------------------------------------------------------
# per-file worker
# ---------------------------------------------------------------------------


FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000


def _is_cloud_placeholder(st: os.stat_result) -> bool:
    """OneDrive「仅在线」文件：本地只是占位符，读取必报 OSError(22)。

    这类文件不读、不索引、不记 broken，直接跳过（计入 skipped），
    避免把云占位当成坏图混进图库。"""
    return bool(getattr(st, "st_file_attributes", 0)
                & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def _process_video(path: str, ext: str) -> str:
    """Video lane of the fast channel: hash + probe + poster-frame thumbnail.

    No transcoding ever — the original file is the deliverable, streamed
    later via /media. Videos opt out of every slow lane up front
    (ocr_status/sem_status start as 'done', no phash, caption skips them).
    """
    try:
        st = os.stat(path)
        if _is_cloud_placeholder(st):
            bump(skipped=1)
            return "skipped"
        file_id = hash_file(path)
        info = media.probe(path)
        width = height = None
        duration = None
        img = None
        if info is not None:
            duration = info.get("duration")
            width, height = info.get("width"), info.get("height")
            img = media.poster_any(path, duration)
        status = "ok" if img is not None else "broken"
        if img is not None:
            build_thumbs(file_id, img)
        outcome = upsert_photo({
            "id": file_id, "path": path, "kind": "video",
            "taken_at": st.st_mtime, "mtime": st.st_mtime,
            "width": width, "height": height, "bytes": st.st_size,
            "ext": ext, "status": status, "duration": duration,
            "ocr_status": "done", "sem_status": "done", "face_status": "done",
            "gps_status": "done",   # poster frame: no camera GPS to find
        })
    except Exception:
        try:
            st = os.stat(path)
            upsert_photo({
                "id": hash_file(path), "path": path, "kind": "video",
                "taken_at": st.st_mtime, "mtime": st.st_mtime,
                "width": None, "height": None, "bytes": st.st_size,
                "ext": ext, "status": "broken",
                "ocr_status": "done", "sem_status": "done", "face_status": "done",
                "gps_status": "done",
            })
        except Exception:
            pass
        bump(broken=1)
        return "broken"
    if outcome == "existing":
        bump(duplicated=1)
        if reconcile_existing(file_id, path, st.st_mtime):
            return "reconciled"
    else:
        demote_previous_at_path(path, file_id)
        bump(indexed=1)
        caption.on_ingested(file_id, "video")  # policy: never caption videos
    return outcome


def process_file(path: str) -> str:
    """Returns outcome: inserted | existing | skipped | broken"""
    name = os.path.basename(path)
    if is_ignored_path(path):
        # 回收站 / 微信QQ 表情包等目录不进索引
        bump(skipped=1)
        return "skipped"
    if name.startswith("._"):
        # macOS AppleDouble 元数据（拷贝 zip 时常见），内容不是图片
        bump(skipped=1)
        return "skipped"
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return _process_video(path, ext)
    try:
        st = os.stat(path)
        if _is_cloud_placeholder(st):
            bump(skipped=1)
            return "skipped"
        file_id = hash_file(path)
        width = height = None
        kind = PLACEHOLDER_KIND
        taken_at = st.st_mtime
        gps = None
        with Image.open(path) as img:
            width, height = img.size          # true size before draft() downscales decode
            exif_time = _exif_taken_at(img)
            gps = places.gps_from_exif(img)   # header-only read, no pixel decode
            img.draft("RGB", (2048, 2048))    # decode faster for big photos
            if not (width and height):
                width, height = img.size
            if exif_time:
                taken_at = exif_time
            kind = classify(ext, exif_time is not None, width or 0, height or 0)
            build_thumbs(file_id, img)
            ph = dups.phash_from_pil(img)
        gps_lat = gps_lon = place = None
        if gps:
            gps_lat, gps_lon = gps
            place = places.place_for(gps_lat, gps_lon)
        outcome = upsert_photo({
            "id": file_id, "path": path, "kind": kind,
            "taken_at": taken_at, "mtime": st.st_mtime,
            "width": width, "height": height, "bytes": st.st_size,
            "ext": ext, "status": "ok",
            "phash": dups.to_signed(ph) if ph is not None else None,
            "gps_lat": gps_lat, "gps_lon": gps_lon, "place": place,
            "gps_status": "done",   # EXIF is already open: lane has nothing left
        })
    except Exception:
        # Unreadable file: still register it with a placeholder status so the
        # grid can show a "broken" card instead of a hole.
        try:
            st = os.stat(path)
            file_id = hash_file(path)
            upsert_photo({
                "id": file_id, "path": path, "kind": PLACEHOLDER_KIND,
                "taken_at": st.st_mtime, "mtime": st.st_mtime,
                "width": None, "height": None, "bytes": st.st_size,
                "ext": ext, "status": "broken",
                "gps_status": "done",
            })
        except Exception:
            pass
        bump(broken=1)
        return "broken"
    if outcome == "existing":
        bump(duplicated=1)
        # same content seen again: a move (old path gone / row marked missing)
        # re-attaches the row; a true duplicate just stays counted
        if reconcile_existing(file_id, path, st.st_mtime):
            return "reconciled"
    else:
        demote_previous_at_path(path, file_id)  # edited in place -> old row retires
        bump(indexed=1)
        # brand-new row: the cloud caption lane queues it only if the user
        # switched it on (and it is not a screenshot under the default policy)
        caption.on_ingested(file_id, kind)
    return outcome


# ---------------------------------------------------------------------------
# background job
# ---------------------------------------------------------------------------


def _scan_one(path: str) -> str:
    bump(seen=1)
    for attempt in range(4):
        try:
            return process_file(path)
        except sqlite3.OperationalError as e:
            # 写库撞上后台 lane 时重试，别把好图记成 broken
            if "locked" not in str(e).lower() or attempt == 3:
                raise
            time.sleep(0.3 * (attempt + 1))
    return "broken"


def kick_slow_lanes() -> None:
    """(Re)start every background lane with pending work. Each start_* is a
    cheap no-op when the lane is running or has nothing to do, so the watcher
    can call this after each batch (closes the gap where files that arrive
    between scans never get OCR'd / grouped / embedded)."""
    from . import ocr, semantic, people, places, scenes
    ocr.start_ocr()
    dups.start_backfill()
    semantic.start_backfill()
    people.start_backfill()
    places.start_backfill()
    scenes.start_backfill()
    caption.start()


def iter_images(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith((".", "$")) and not is_ignored_dir_name(d)]
        for name in filenames:
            if name.startswith("._"):      # macOS AppleDouble 元数据
                continue
            if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                p = os.path.join(dirpath, name)
                if not is_ignored_path(p):
                    yield p


def _scan_root(root: str) -> None:
    """Scan one root (fast channel). Progress counters accumulate across the
    roots of a startup sweep; the root field shows what is being walked."""
    conn = get_conn()
    _update(root=root)
    # decode/encode release the GIL in Pillow's C paths, so a modest
    # thread pool scales well on many-core machines
    workers = max(4, min(8, (os.cpu_count() or 4) - 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(_scan_one, iter_images(root), chunksize=4):
            pass
    retry_locked(lambda: (
        conn.execute(
            "INSERT INTO scan_state(root,last_scan) VALUES(?,?)"
            " ON CONFLICT(root) DO UPDATE SET last_scan=excluded.last_scan",
            (root, time.time()),
        ),
        conn.commit(),
    ))
    # files that vanished since last scan (watcher was down) -> missing
    swept = retry_locked(lambda: sweep_missing(root))
    bump(swept_missing=swept)
    # keep this folder fresh without a manual rescan
    from . import watch
    watch.start(root)


def _run_scan(roots: list[str]) -> None:
    try:
        for root in roots:
            _scan_root(root)
        # fast channel done -> kick every slow lane that has pending work
        kick_slow_lanes()
    except Exception as e:  # job-level failure stays visible in /api/status
        _update(error=f"{type(e).__name__}: {e}")
    finally:
        _update(running=False, finished_at=time.time())


def common_roots() -> list[str]:
    """系统常用文件夹：图片（默认，可写）、桌面、下载、文档。

    以 Windows 注册表里的真实路径为准（用户可能把「图片」搬到 D:\\图片），
    取不到再退回 %USERPROFILE% 下的同名目录。"""
    home = Path.home()
    known = known_folders()
    candidates = [Path(known[k]) for k in ("pictures", "desktop", "downloads",
                                         "documents") if k in known]
    candidates += [
        home / "Pictures", home / "Desktop", home / "Downloads",
        home / "Documents",
    ]
    seen: set[str] = set()
    roots: list[str] = []
    for p in candidates:
        try:
            if not p.is_dir():
                continue
            key = os.path.normcase(os.path.realpath(str(p)))
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        roots.append(str(p))
    return roots


def _stale_roots(roots: list[str], max_age: float = 2 * 3600) -> list[str]:
    """首次运行全部扫描；之后只重扫超过 max_age 没扫过的（其余靠 watcher）。"""
    conn = get_conn()
    last = {r["root"]: r["last_scan"] for r in
            conn.execute("SELECT root, last_scan FROM scan_state").fetchall()}
    now = time.time()
    return [r for r in roots
            if now - (last.get(r) or 0) > max_age]


def start_scan(root: str) -> dict:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"started": False, "reason": f"not a directory: {root}"}
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "scan already running",
                    "root": PROGRESS["root"]}
        PROGRESS.update(
            running=True, root=root, seen=0, indexed=0, duplicated=0,
            broken=0, skipped=0, swept_missing=0, started_at=time.time(),
            finished_at=None, error=None,
        )
    threading.Thread(target=_run_scan, args=([root],), daemon=True).start()
    return {"started": True, "root": root}


def start_common_scan() -> dict:
    """开机自检：把系统常用文件夹里新增的图片补进索引（增量、后台）。
    首次运行会扫全部常用目录；之后每个目录最多每 2 小时重扫一次。"""
    roots = common_roots()
    stale = _stale_roots(roots)
    if not stale:
        return {"started": False, "reason": "common folders are fresh",
                "roots": roots}
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "scan already running",
                    "root": PROGRESS["root"]}
        PROGRESS.update(
            running=True, root=stale[0], seen=0, indexed=0, duplicated=0,
            broken=0, skipped=0, swept_missing=0, started_at=time.time(),
            finished_at=None, error=None,
        )
    threading.Thread(target=_run_scan, args=(stale,), daemon=True).start()
    return {"started": True, "roots": stale}
