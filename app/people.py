"""People lane: local, offline face *detection* (not identification).

Scope agreed with the user: the 发现页"人物" album lists photos that contain
people — it does NOT cluster by identity, so there is no per-person naming and
no face embeddings. That keeps the whole feature to one lightweight detector.

Zero model download: OpenCV's Haar cascade XMLs ship *inside* the
opencv-python-headless wheel (pinned to 4.x here, since 5.0 dropped
CascadeClassifier). Detection runs fully on-device on the already-built 512px
thumbnails — nothing is uploaded, and the thumbnails are already EXIF-oriented
and decoded, so this lane never touches heic/avif originals.

It rides the same slow-lane contract as OCR / pHash / semantics:
  - new rows default face_status='pending'; videos are pre-marked 'done'
    (nothing to detect on a poster frame we don't own) and excluded by query;
  - start_backfill() is a cheap no-op when idle/empty, so ingest.kick_slow_lanes
    can call it after every scan/watch batch;
  - a decode failure marks the row 'failed' rather than crashing the batch.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from .config import thumb_path
from .db import get_conn

_progress_lock = threading.Lock()
PROGRESS = {
    "running": False, "scanned": 0, "with_faces": 0, "failed": 0,
    "total": 0, "started_at": None, "finished_at": None, "error": None,
}

# frontal + profile gives side-glance coverage without a second model file.
_CASCADE_FILES = ("haarcascade_frontalface_default.xml",
                  "haarcascade_profileface.xml")
_MIN_SIZE = (28, 28)        # ignore specks; ~5% of a 512 thumb
_SCALE = 1.15
_MIN_NEIGHBORS = 5

_cascades: list | None = None
_cascade_lock = threading.Lock()


def available() -> bool:
    """True when OpenCV + its bundled cascades can be loaded at all."""
    return _load_cascades() is not None


def _load_cascades() -> list | None:
    """Lazily build the CascadeClassifiers once; None if opencv is unusable."""
    global _cascades
    if _cascades is not None:
        return _cascades or None
    with _cascade_lock:
        if _cascades is not None:
            return _cascades or None
        try:
            import cv2
            base = cv2.data.haarcascades
            cls = []
            for name in _CASCADE_FILES:
                c = cv2.CascadeClassifier(base + name)
                if not c.empty():
                    cls.append(c)
            _cascades = cls
        except Exception:
            _cascades = []        # sentinel: tried, unavailable
        return _cascades or None


def _iou_merge(boxes: list, thresh: float = 0.3) -> list:
    """Greedy drop of near-duplicate boxes (frontal vs profile can both fire
    on a three-quarter face). Keeps the larger box."""
    out: list = []
    for b in sorted(boxes, key=lambda r: r[2] * r[3], reverse=True):
        x, y, w, h = b
        dup = False
        for ox, oy, ow, oh in out:
            ix = max(0, min(x + w, ox + ow) - max(x, ox))
            iy = max(0, min(y + h, oy + oh) - max(y, oy))
            inter = ix * iy
            union = w * h + ow * oh - inter
            if union > 0 and inter / union > thresh:
                dup = True
                break
        if not dup:
            out.append(b)
    return out


def detect_count(gray: np.ndarray) -> int:
    """Number of distinct faces in a 2-D uint8 grayscale array (0 if none)."""
    cascades = _load_cascades()
    if not cascades:
        return 0
    boxes: list = []
    for c in cascades:
        found = c.detectMultiScale(gray, scaleFactor=_SCALE,
                                   minNeighbors=_MIN_NEIGHBORS,
                                   minSize=_MIN_SIZE)
        for (x, y, w, h) in _as_iter(found):
            boxes.append((int(x), int(y), int(w), int(h)))
    return len(_iou_merge(boxes))


def _as_iter(found):
    """detectMultiScale returns () or an (N,4) array/tuple across versions."""
    if found is None:
        return []
    try:
        if len(found) == 0:
            return []
    except TypeError:
        return []
    return found


def _to_gray(file_id: str):
    """Load a photo's 512 (or 128) thumbnail as a 2-D uint8 grayscale array."""
    import cv2
    from PIL import Image
    tp = thumb_path(file_id, 512)
    if not tp.exists():
        tp = thumb_path(file_id, 128)
    if not tp.exists():
        return None
    with Image.open(tp) as im:
        arr = np.asarray(im.convert("L"))
    return np.ascontiguousarray(arr)


def _bump(**kw) -> None:
    with _progress_lock:
        for k, v in kw.items():
            PROGRESS[k] = PROGRESS.get(k, 0) + v


def _backfill_worker() -> None:
    conn = get_conn()
    try:
        todo = conn.execute(
            "SELECT id FROM photos WHERE face_status='pending' AND status='ok'"
            " AND kind!='video' ORDER BY taken_at DESC").fetchall()
        with _progress_lock:
            PROGRESS["total"] = len(todo)
        for r in todo:
            fid = r["id"]
            try:
                gray = _to_gray(fid)
                if gray is None:
                    raise FileNotFoundError("no thumbnail")
                n = detect_count(gray)
                conn.execute(
                    "UPDATE photos SET face_count=?, face_status='done' WHERE id=?",
                    (n, fid))
                _bump(scanned=1)
                if n > 0:
                    _bump(with_faces=1)
            except Exception:
                conn.execute(
                    "UPDATE photos SET face_status='failed' WHERE id=?", (fid,))
                _bump(failed=1)
            if PROGRESS["scanned"] % 200 == 0:
                conn.commit()
        conn.commit()
    except Exception as e:
        with _progress_lock:
            PROGRESS["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _progress_lock:
            PROGRESS["running"] = False
            PROGRESS["finished_at"] = time.time()


def start_backfill() -> dict:
    conn = get_conn()
    pending = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE face_status='pending'"
        " AND status='ok' AND kind!='video'").fetchone()["c"]
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "backfill already running"}
        if pending == 0:
            return {"started": False, "reason": "nothing pending"}
        if not available():
            return {"started": False, "reason": "opencv/haar unavailable"}
        PROGRESS.update(running=True, scanned=0, with_faces=0, failed=0,
                        total=pending, started_at=time.time(), finished_at=None,
                        error=None)
    threading.Thread(target=_backfill_worker, daemon=True, name="people-backfill").start()
    return {"started": True, "pending": pending}


def progress_snapshot() -> dict:
    with _progress_lock:
        return dict(PROGRESS)


def summary() -> dict:
    conn = get_conn()
    with_faces = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE face_count>0 AND status='ok'"
        " AND kind!='video'").fetchone()["c"]
    faces = conn.execute(
        "SELECT COALESCE(SUM(face_count),0) s FROM photos"
        " WHERE status='ok' AND kind!='video'").fetchone()["s"]
    scanned = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE face_status='done' AND kind!='video'"
        " AND status='ok'").fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE face_status='pending'"
        " AND status='ok' AND kind!='video'").fetchone()["c"]
    return {"with_faces": with_faces, "faces": int(faces or 0),
            "scanned": scanned, "pending": pending,
            "available": available(), "progress": progress_snapshot()}
