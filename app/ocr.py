"""Slow channel: background OCR worker.

Runs after (or alongside) the fast channel, one worker thread with a lazily
created RapidOCR engine. Screenshots are processed first because text is
their primary content; photos follow newest-first. Recognised text goes to
SQLite FTS5 with a CJK-bigram tokeniser so Chinese substring search works.
"""
from __future__ import annotations

import os
import re
import threading
import time

from .config import DATA_DIR
from .db import get_conn

_engine = None
_engine_lock = threading.Lock()

_progress_lock = threading.Lock()
PROGRESS = {
    "running": False,
    "done": 0,
    "empty": 0,      # processed but no text found
    "failed": 0,
    "pending_at_start": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_stop = threading.Event()


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
        "SELECT COUNT(*) c FROM photos WHERE ocr_status='pending' AND status='ok'"
    ).fetchone()["c"]
    return snap


# ---------------------------------------------------------------------------
# tokenising for FTS5 (unicode61 chokes on unspaced CJK)
# ---------------------------------------------------------------------------

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> str:
    """CJK runs -> overlapping bigrams (single char kept if isolated);
    latin/digits -> lowercase words. Space-joined, ready for fts5 unicode61."""
    tokens: list[str] = []
    low = text.lower()
    chars = list(low)
    n = len(chars)
    i = 0
    while i < n:
        ch = chars[i]
        if _CJK.match(ch):
            j = i
            while j < n and _CJK.match(chars[j]):
                j += 1
            run = chars[i:j]
            if len(run) == 1:
                tokens.append(run[0])
            else:
                tokens.extend(run[k] + run[k + 1] for k in range(len(run) - 1))
                tokens.extend(run)  # unigrams too: 2-char queries match either
            i = j
        elif ch.isascii() and ch.isalnum():
            m = _ASCII_WORD.match(low, i)
            tokens.append(m.group(0))
            i = m.end()
        else:
            i += 1
    return " ".join(tokens)


def build_query_match(q: str) -> str:
    """Turn a user query into an FTS5 MATCH expression (all tokens ANDed)."""
    toks = tokenize(q).split()
    if not toks:
        return ""
    quoted = [t.replace('"', '""') for t in toks]
    return " AND ".join(f'"{t}"' for t in quoted)


# ---------------------------------------------------------------------------
# engine + worker
# ---------------------------------------------------------------------------


def get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR
            _engine = RapidOCR()
    return _engine


def ocr_file(path: str) -> str:
    result = get_engine()(path)
    txts = getattr(result, "txts", None)
    if not txts:
        return ""
    return "\n".join(str(t) for t in txts)


def save_result(file_id: str, raw: str, ok: bool) -> None:
    conn = get_conn()
    if ok:
        conn.execute("DELETE FROM ocr_fts WHERE id=?", (file_id,))
        if raw:
            conn.execute(
                "INSERT INTO ocr_fts(tokens, raw, id) VALUES(?,?,?)",
                (tokenize(raw), raw, file_id),
            )
        conn.execute(
            "UPDATE photos SET ocr_status='done' WHERE id=?", (file_id,))
    else:
        conn.execute(
            "UPDATE photos SET ocr_status='failed' WHERE id=?", (file_id,))
    conn.commit()


def claim_pending() -> tuple[str, str] | None:
    """Atomically grab the next photo to OCR. Screenshots first, then photos
    newest-first; anything already claimed flips to 'running' so concurrent
    claims never double up."""
    conn = get_conn()
    row = conn.execute(
        """SELECT id, path FROM photos
           WHERE ocr_status='pending' AND status='ok'
           ORDER BY CASE kind WHEN 'screenshot' THEN 0 ELSE 1 END, taken_at DESC
           LIMIT 1""").fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE photos SET ocr_status='running' WHERE id=? AND ocr_status='pending'",
        (row["id"],))
    conn.commit()
    if cur.rowcount == 0:
        return claim_pending()  # lost the race, take the next one
    return row["id"], row["path"]


def _run() -> None:
    try:
        while not _stop.is_set():
            claim = claim_pending()
            if claim is None:
                break
            file_id, path = claim
            try:
                raw = ocr_file(path)
                save_result(file_id, raw, ok=True)
                _bump(done=1, empty=0 if raw else 1)
            except Exception:
                save_result(file_id, "", ok=False)
                _bump(failed=1)
    except Exception as e:
        with _progress_lock:
            PROGRESS["error"] = f"{type(e).__name__}: {e}"
    finally:
        # a crash mid-row must not leave 'running' stragglers
        get_conn().execute(
            "UPDATE photos SET ocr_status='pending' WHERE ocr_status='running'")
        get_conn().commit()
        with _progress_lock:
            PROGRESS["running"] = False
            PROGRESS["finished_at"] = time.time()


def start_ocr() -> dict:
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "ocr already running"}
        conn = get_conn()
        # reclaim stragglers left 'running' by a killed process
        conn.execute(
            "UPDATE photos SET ocr_status='pending' WHERE ocr_status='running'")
        conn.commit()
        pending = conn.execute(
            "SELECT COUNT(*) c FROM photos WHERE ocr_status='pending' AND status='ok'"
        ).fetchone()["c"]
        if pending == 0:
            return {"started": False, "reason": "nothing pending"}
        PROGRESS.update(running=True, done=0, empty=0, failed=0,
                        pending_at_start=pending, started_at=time.time(),
                        finished_at=None, error=None)
        _stop.clear()
    threading.Thread(target=_run, daemon=True, name="ocr-worker").start()
    return {"started": True, "pending": pending}


def stop_ocr() -> dict:
    _stop.set()
    return {"stopping": True}
