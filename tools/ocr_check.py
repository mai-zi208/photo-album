"""Focused OCR + search check (fast: only the text screenshots).

Usage:  .venv/Scripts/python.exe tools/ocr_check.py

1. Scans only .bench/library/text_screenshots (12 planted-phrase images).
2. Runs the OCR slow channel over them.
3. Asserts every planted phrase is findable via /api/search, measures rate.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for mk_test_images

# scratch DB/thumbs so this check never clobbers the real index, and isolate
# every non-OCR slow lane — /api/search now defaults to engine="all", whose
# semantic lane returns top-k nearest neighbours for *any* query, which would
# otherwise flood even a nonsense query and defeat the negative guard below.
import app.config as cfg
SCRATCH = Path(__file__).resolve().parent.parent / ".bench" / "_ocr_check"
cfg.DATA_DIR = SCRATCH / ".data"
cfg.DB_PATH = cfg.DATA_DIR / "t.db"
cfg.THUMB_DIR = cfg.DATA_DIR / "thumbs"

from app import db, ingest, ocr  # noqa: E402
import app.semantic as semantic  # noqa: E402
import app.dups as dups  # noqa: E402
import app.people as people  # noqa: E402
semantic.model_present = lambda: False
semantic.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
people.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
from mk_test_images import PHRASES  # noqa: E402


def main() -> int:
    import shutil
    shutil.rmtree(SCRATCH, ignore_errors=True)
    db.init_db()
    # fresh slate so counts are deterministic
    conn = db.get_conn()
    conn.execute("DELETE FROM photos")
    conn.execute("DELETE FROM ocr_fts")
    conn.commit()

    text_dir = Path(__file__).resolve().parent.parent / ".bench" / "library" / "text_screenshots"
    if not text_dir.is_dir():
        print("missing text screenshots; run mk_test_images.py first")
        return 1

    ingest.start_scan(str(text_dir))
    while ingest.PROGRESS["running"]:
        time.sleep(0.2)
    print(f"scanned {ingest.PROGRESS['seen']} text screenshots")

    # OCR: start_scan auto-kicked the worker; make sure it's running
    r = ocr.start_ocr()
    print("ocr start:", r)
    t0 = time.time()
    while ocr.PROGRESS["running"]:
        time.sleep(0.3)
    wall = time.time() - t0
    snap = ocr.progress_snapshot()
    print(f"OCR done={snap['done']} empty={snap['empty']} failed={snap['failed']} "
          f"in {wall:.1f}s = {snap['done']/max(wall,0.001):.1f} img/s")

    from fastapi.testclient import TestClient
    from app.server import app as fastapi_app
    client = TestClient(fastapi_app)

    problems = []
    if snap["failed"]:
        problems.append(f"{snap['failed']} OCR failures")
    if snap["empty"] > 2:
        problems.append(f"{snap['empty']} images yielded no text (expected ~0)")

    # every planted phrase must be findable. Probe with the leading CJK run
    # (up to 4 chars) so we never cut mid-alphanumeric-token like "CA1836".
    def probe_of(phrase: str) -> str:
        run = ""
        for ch in phrase:
            if "\u4e00" <= ch <= "\u9fff":
                run += ch
                if len(run) >= 4:
                    break
            elif run:
                break
        return run or phrase.split()[0]

    hit_ids = {}
    for phrase in PHRASES:
        probe = probe_of(phrase)
        res = client.get("/api/search", params={"q": probe}).json()
        ids = {i["id"] for i in res["items"]}
        hit_ids[phrase] = ids
        if not ids:
            problems.append(f"search {probe!r} (from {phrase!r}) -> 0 hits")
    # a nonsense query must return nothing
    neg = client.get("/api/search", params={"q": "紫色长颈鹿密码"}).json()
    if neg["items"]:
        problems.append(f"nonsense query returned {len(neg['items'])} hits")
    # english token search
    eng = client.get("/api/search", params={"q": "SF1234567890"}).json()
    print("search 'SF1234567890' ->", len(eng["items"]), "hits")

    print("\nper-phrase hit counts:")
    for phrase, ids in hit_ids.items():
        print(f"  {len(ids)}  {phrase}")

    if problems:
        print("\nFAILURES:")
        for p in problems:
            print(" -", p)
        return 1
    print("\nOCR + SEARCH ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
