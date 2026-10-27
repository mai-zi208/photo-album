"""Incremental watch verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe -u tools/watch_check.py

Exercises the real filesystem through the watcher:
  emoji/recycle dirs -> never indexed;
  add -> indexed;  move -> same row re-pointed (identity kept);
  delete -> index row forgotten (外部删除同步);
  restore -> row re-created at its old path;  edit-in-place -> new row, old row gone;
  folder rename -> whole subtree re-pointed;
  watcher-down case -> rescan sweep deletes the vanished rows.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- isolate BEFORE importing db/ingest/watch ----
import app.config as config  # noqa: E402

SCRATCH = ROOT / ".bench" / "_watch_test"
LIB = SCRATCH / "library"
DATA = SCRATCH / "data"
for d in (LIB, DATA):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
config.DATA_DIR = DATA
config.DB_PATH = DATA / "test.db"
config.THUMB_DIR = DATA / "thumbs"

from PIL import Image  # noqa: E402

from app import db, ingest, watch  # noqa: E402

watch.QUIET_SEC = 0.5   # fast settle for tests (prod default 2.0)


def put(path: Path, seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (300, 200), (seed * 30 % 256, seed * 7 % 256, 128))
    img.save(path, "JPEG", quality=90)
    return path


def row_of(path: str) -> dict | None:
    r = db.get_conn().execute(
        "SELECT * FROM photos WHERE path=?", (path,)).fetchone()
    return dict(r) if r else None


def by_id(file_id: str) -> dict | None:
    r = db.get_conn().execute("SELECT * FROM photos WHERE id=?", (file_id,)).fetchone()
    return dict(r) if r else None


def until(pred, timeout=25.0, why=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError(f"timeout waiting: {why}")


def main() -> int:
    problems = []
    import app.ocr as ocr
    ocr.start_ocr = lambda *a, **k: {"started": False}

    db.init_db()
    files = [put(LIB / f"f{i}.jpg", i) for i in range(1, 5)]
    sub = put(LIB / "sub" / "f5.jpg", 5)
    # 修改意见 1：微信/QQ 表情包与回收站目录不进索引
    ignored = [put(LIB / "CustomEmotion" / "emoji.jpg", 21),
               put(LIB / "$Recycle.Bin" / "gone.jpg", 22)]

    ingest.start_scan(str(LIB))
    until(lambda: not ingest.PROGRESS["running"], why="initial scan")
    if db.stats()["total"] != 5:
        problems.append(f"initial scan: total {db.stats()['total']} != 5")
    if any(row_of(str(p)) for p in ignored):
        problems.append("[ignore] emoji / recycle-bin files entered the index")
    until(lambda: watch.watched_roots() == [str(LIB)], why="watch auto-start")
    print("[boot] scanned 5, emoji+recycle skipped", watch.watched_roots())

    # ---- 1. add ----
    f6 = put(LIB / "f6.jpg", 6)
    put(LIB / "EmotionIcon" / "live.jpg", 23)   # 监听也不能收表情包
    r6 = until(lambda: row_of(str(f6)), why="f6 indexed by watcher")
    print(f"[add] f6 indexed, status={r6['status']}")

    # ---- 2. move (identity must survive: same id, new path) ----
    moved = LIB / "moved" / "f6.jpg"
    moved.parent.mkdir(exist_ok=True)
    time.sleep(watch.QUIET_SEC + 0.5)   # let the add batch settle first
    os.rename(f6, moved)
    until(lambda: (r := by_id(r6["id"])) and r["path"] == str(moved)
          and r["status"] == "ok", why="f6 re-pointed after move")
    total = db.get_conn().execute("SELECT COUNT(*) c FROM photos").fetchone()["c"]
    if total != 6:
        problems.append(f"[move] row count {total} != 6 (move created a new row)")
    else:
        print("[move] same row, new path, no duplicate row")

    # ---- 3. delete -> index row forgotten (工具外删除同步) ----
    time.sleep(watch.QUIET_SEC + 0.5)
    os.remove(moved)
    until(lambda: by_id(r6["id"]) is None, why="f6 row forgotten after delete")
    from fastapi.testclient import TestClient
    from app.server import app as server_app
    client = TestClient(server_app)
    ids = {i["id"] for i in client.get("/api/photos?limit=100").json()["items"]}
    if r6["id"] in ids:
        problems.append("[delete] forgotten row still visible in /api/photos")
    if watch.progress_snapshot()["missing"] < 1:
        problems.append("[delete] watcher.missing counter did not move")
    print("[delete] index row gone, no ghost row in API")

    # ---- 4. restore -> row comes back at the same path ----
    time.sleep(watch.QUIET_SEC + 0.5)
    put(moved, 6)
    until(lambda: (r := by_id(r6["id"])) and r["status"] == "ok"
          and r["path"] == str(moved), why="f6 restored")
    print("[restore] same id re-indexed at its old path")

    # ---- 5. edit in place -> new row, old row gone ----
    time.sleep(watch.QUIET_SEC + 0.5)
    old_f1_id = row_of(str(files[0]))["id"]
    put(files[0], 99)   # different content, same path
    def edit_state():
        new = row_of(str(files[0]))
        return new and new["id"] != old_f1_id and by_id(old_f1_id) is None
    until(edit_state, why="f1 edited: new row ok, old row forgotten")
    print("[edit] new content indexed, previous row forgotten")

    # ---- 6. folder rename -> whole subtree re-pointed ----
    time.sleep(watch.QUIET_SEC + 0.5)
    r5 = row_of(str(sub))
    os.rename(LIB / "sub", LIB / "sub2")
    until(lambda: (r := by_id(r5["id"])) and r["path"] == str(LIB / "sub2" / "f5.jpg")
          and r["status"] == "ok", why="f5 re-pointed after folder rename")
    print("[rename-dir] subtree followed, identity kept")

    # ---- 7. watcher down -> rescan sweep catches the loss ----
    time.sleep(watch.QUIET_SEC + 0.5)
    watch.stop(str(LIB))
    f2_id = row_of(str(files[1]))["id"]
    os.remove(files[1])
    ingest.start_scan(str(LIB))
    until(lambda: not ingest.PROGRESS["running"], why="sweep rescan")
    swept = ingest.progress_snapshot()["swept_missing"]
    gone = by_id(f2_id)
    if swept < 1 or gone is not None:
        problems.append(f"[sweep] swept={swept} row_exists={gone is not None},"
                        f" expected >=1/gone")
    else:
        print(f"[sweep] watcher-down loss caught on rescan (swept={swept})")

    # ---- 8. counters sane ----
    snap = watch.progress_snapshot()
    print(f"[watch] batches={snap['batches']} events={snap['events']} "
          f"new={snap['new']} reconciled={snap['reconciled']} missing={snap['missing']} "
          f"error={snap['error']}")
    if snap["error"]:
        problems.append(f"watch worker error: {snap['error']}")
    if snap["reconciled"] < 2:
        problems.append(f"reconciled={snap['reconciled']}, expected >=2 (move + dir rename)")

    if problems:
        print("\nFAILURES:")
        for x in problems:
            print(" -", x)
        return 1
    print("\nWATCH ALL PASSED (add/move/delete/restore/edit/rename-dir/sweep)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        watch.stop()
        time.sleep(1)
        shutil.rmtree(SCRATCH, ignore_errors=True)
    raise SystemExit(code)
