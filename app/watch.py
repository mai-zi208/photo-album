"""Incremental directory watching (watchdog) on top of the scan pipeline.

Design: filesystem events are noisy (editors copy, explorers move in two
steps, cloud sync rewrites). So the handler only records WHICH paths were
touched, and a worker thread processes the batch once the directory has been
quiet for QUIET_SEC. Each batch runs in two phases:

  1. present-first: surviving paths go through ingest.process_file, whose
     content-hash reconcile step re-points the row to its new location
     (a move = gone + present, so identity + taken_at survive).
  2. gone-after  : paths that vanished delete their index rows, so deletions
     made outside this tool (Explorer / 系统回收站) stay in sync.

Directories in the batch are handled as subtrees (prefix-forget / re-walk),
since renaming a folder fires a single directory event.
"""
from __future__ import annotations

import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import MEDIA_EXTS, is_ignored_path
from .db import forget_path, forget_prefix, get_conn

QUIET_SEC = 2.0          # settle time before a batch is processed
POLL_SEC = 0.3

_progress_lock = threading.Lock()
STATS = {
    "new": 0, "reconciled": 0, "duplicate": 0, "broken": 0, "missing": 0,
    "events": 0, "batches": 0,
}
_last_event_ts = 0.0
_last_batch_at: float | None = None
_error: str | None = None

_pending_lock = threading.Lock()
_pending_files: set[str] = set()
_pending_dirs: set[str] = set()

_observers: dict[str, Observer] = {}
_watch_lock = threading.Lock()
_worker: threading.Thread | None = None
_stop = threading.Event()


class _Handler(FileSystemEventHandler):
    def on_any_event(self, event) -> None:
        global _last_event_ts
        paths = [event.src_path]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(dest)
        hit = False
        with _pending_lock:
            for p in paths:
                if is_ignored_path(p):
                    continue
                if event.is_directory:
                    _pending_dirs.add(p)
                    hit = True
                elif os.path.splitext(p)[1].lower() in MEDIA_EXTS:
                    _pending_files.add(p)
                    hit = True
            if hit:
                STATS["events"] += 1
                _last_event_ts = time.time()


def _apply_batch(files: set[str], dirs: set[str]) -> None:
    from . import ingest  # late import: ingest -> watch would be circular
    conn = get_conn()
    # phase 1: present paths first -> normal ingest. 先跑存在的文件，移动
    # （旧路径消失 + 新路径出现）会在这里被 reconcile 接走，行不会丢。
    for d in dirs:
        if os.path.isdir(d):
            for p in ingest.iter_images(d):
                _count(ingest.process_file(p))
    for f in files:
        if os.path.isfile(f) and not is_ignored_path(f) \
                and os.path.splitext(f)[1].lower() in MEDIA_EXTS:
            _count(ingest.process_file(f))
    # phase 2: vanished paths -> 索引直接删除（工具外删除/移进回收站也要同步）
    for d in dirs:
        if not os.path.isdir(d):
            STATS["missing"] += forget_prefix(conn, d)
    for f in files:
        if not os.path.isfile(f) and not is_ignored_path(f):
            STATS["missing"] += forget_path(conn, f)
    # new/changed rows -> slow lanes resume without waiting for a manual rescan
    # （扫描进行中先不启动：扫描结束后 ingest 会统一拉起，避免写库互相抢锁）
    if not ingest.progress_snapshot()["running"]:
        ingest.kick_slow_lanes()


def _count(outcome: str) -> None:
    STATS[{"inserted": "new", "reconciled": "reconciled",
           "existing": "duplicate", "broken": "broken"}.get(outcome, "duplicate")] += 1


def _worker_loop() -> None:
    global _last_batch_at, _error, _pending_files, _pending_dirs
    while not _stop.wait(POLL_SEC):
        quiet = time.time() - _last_event_ts >= QUIET_SEC
        with _pending_lock:
            files, dirs = _pending_files, _pending_dirs
            if not quiet or not (files or dirs):
                continue
            _pending_files, _pending_dirs = set(), set()
        try:
            _apply_batch(files, dirs)
            STATS["batches"] += 1
            _last_batch_at = time.time()
        except Exception as e:
            _error = f"{type(e).__name__}: {e}"


def _ensure_worker() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    _worker = threading.Thread(target=_worker_loop, daemon=True, name="watch-batch")
    _worker.start()


def start(root: str) -> dict:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"started": False, "reason": f"not a directory: {root}"}
    with _watch_lock:
        if root in _observers:
            return {"started": False, "reason": "already watching", "root": root}
        obs = Observer()
        obs.daemon = True   # importing the server (or tests) must not hang exit
        try:
            obs.schedule(_Handler(), root, recursive=True)
            obs.start()
        except Exception as e:
            return {"started": False, "reason": f"{type(e).__name__}: {e}"}
        _observers[root] = obs
        _ensure_worker()
    return {"started": True, "root": root}


def stop(root: str | None = None) -> dict:
    with _watch_lock:
        roots = [root] if root else list(_observers)
        for r in (os.path.abspath(x) for x in roots):
            obs = _observers.pop(r, None)
            if obs:
                obs.stop()
                obs.join(timeout=3)
        alive = bool(_observers)
    if not alive:
        _stop.set()
    return {"roots": list(_observers)}


def watched_roots() -> list[str]:
    with _watch_lock:
        return list(_observers)


def progress_snapshot() -> dict:
    with _progress_lock:
        snap = dict(STATS)
    snap.update(
        running=bool(watched_roots()),
        roots=watched_roots(),
        last_event_at=_last_event_ts or None,
        last_batch_at=_last_batch_at,
        quiet_sec=QUIET_SEC,
        error=_error,
    )
    return snap
