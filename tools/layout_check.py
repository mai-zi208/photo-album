"""Layout engine verification — operates ONLY on a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe tools/layout_check.py

Isolates itself by pointing config paths at .bench/_layout_test before the
db/layout modules bind their paths, so the real index and user files are
never touched. Covers:
  - copy mode: originals intact, copies created, DB repointed; undo -> copies
    trashed, DB back to originals.
  - move mode: originals gone, files relocated, DB repointed; undo -> byte-level
    restoration verified by BLAKE3.
  - missing source -> step fails cleanly, no crash.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- isolate: rewrite config paths BEFORE importing db/layout ----
import app.config as config  # noqa: E402

SCRATCH = ROOT / ".bench" / "_layout_test"
LIB = SCRATCH / "library"
OUT = SCRATCH / "organized"
DATA = SCRATCH / "data"
for d in (LIB, OUT, DATA):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
config.DATA_DIR = DATA
config.DB_PATH = DATA / "test.db"
config.THUMB_DIR = DATA / "thumbs"

import blake3  # noqa: E402
from PIL import Image  # noqa: E402
from app import db, ingest, layout  # noqa: E402


def make_lib(n: int = 18) -> dict:
    """Create n dated images; return {path: blake3} of originals."""
    db.init_db()
    hashes = {}
    now = time.time()
    for i in range(n):
        ts = now - (i + 1) * 40 * 86400        # spread across months
        struct = time.localtime(ts)
        folder = LIB / time.strftime("%Y", struct) / time.strftime("%Y-%m", struct)
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"img_{i}.jpg"
        img = Image.new("RGB", (200, 200), (i * 10 % 256, 40, 200 - i * 5 % 200))
        img.save(p, "JPEG")
        os.utime(p, (ts, ts))
        hashes[str(p)] = blake3.blake3(p.read_bytes()).hexdigest()
    return hashes


def wait_apply():
    while layout.APPLY["running"]:
        time.sleep(0.1)
    return layout.apply_progress()


def paths_in_db() -> set:
    return {r["path"] for r in db.get_conn().execute("SELECT path FROM photos")}


def main() -> int:
    problems = []
    originals = make_lib()
    ingest.start_scan(str(LIB))
    while ingest.PROGRESS["running"]:
        time.sleep(0.1)
    n = ingest.PROGRESS["indexed"]
    print(f"scanned {n} images into scratch db")
    if n != len(originals):
        problems.append(f"expected {len(originals)} indexed, got {n}")

    # ---------- COPY mode ----------
    p = layout.plan(str(OUT / "copy"), limit=None)
    print(f"[copy] plan: {p['files']} files -> {p['folders']} folders")
    if p["files"] != n:
        problems.append(f"[copy] plan files {p['files']} != {n}")
    r = layout.start_apply("copy"); assert r["started"], r
    pr = wait_apply()
    if pr["failed"]:
        problems.append(f"[copy] apply failures: {pr['failed']}")
    db_paths = paths_in_db()
    copies = [x for x in db_paths if x.startswith(str(OUT / "copy"))]
    if len(copies) != n:
        problems.append(f"[copy] expected {n} repointed paths, got {len(copies)}")
    missing_copies = [x for x in copies if not os.path.exists(x)]
    if missing_copies:
        problems.append(f"[copy] {len(missing_copies)} copies not on disk")
    still_originals = [x for x in originals if os.path.exists(x)]
    if len(still_originals) != n:
        problems.append(f"[copy] originals damaged! {len(still_originals)}/{n} remain")
    print(f"[copy] applied: {len(copies)} copies, {len(still_originals)} originals intact")
    u = layout.undo()
    print("[copy] undo:", u)
    leftover = [x for x in copies if os.path.exists(x)]
    if leftover:
        problems.append(f"[copy] undo left {len(leftover)} copies undeleted")
    if paths_in_db() != set(originals):
        problems.append("[copy] undo did not restore DB paths to originals")

    # ---------- MOVE mode ----------
    p = layout.plan(str(OUT / "moved"))
    r = layout.start_apply("move"); assert r["started"], r
    pr = wait_apply()
    if pr["failed"]:
        problems.append(f"[move] apply failures: {pr['failed']}")
    gone = [x for x in originals if os.path.exists(x)]
    if gone:
        problems.append(f"[move] {len(gone)} originals still in place after move")
    moved = [x for x in paths_in_db() if x.startswith(str(OUT / "moved"))]
    if len(moved) != n:
        problems.append(f"[move] expected {n} moved files, got {len(moved)}")
    # content preserved through the move?
    bad_hash = [x for x in moved
                if blake3.blake3(Path(x).read_bytes()).hexdigest()
                not in set(originals.values())]
    if bad_hash:
        problems.append(f"[move] {len(bad_hash)} files changed content on move")
    print(f"[move] applied: {len(moved)} relocated, content hashes match")
    u = layout.undo()
    print("[move] undo:", u)
    restored = [x for x in originals if os.path.exists(x)]
    if len(restored) != n:
        problems.append(f"[move] undo restored only {len(restored)}/{n} originals")
    rehash_bad = [x for x, h in originals.items()
                  if os.path.exists(x) and blake3.blake3(Path(x).read_bytes()).hexdigest() != h]
    if rehash_bad:
        problems.append(f"[move] {len(rehash_bad)} restored files differ by hash")
    if u.get("failed"):
        problems.append(f"[move] undo reported {u['failed']} failures")

    # ---------- missing-source handling ----------
    # delete one original, then re-plan+apply: that step must fail, not crash
    victim = next(iter(originals))
    db.get_conn().execute("UPDATE photos SET path=? WHERE id=(SELECT id FROM photos LIMIT 1)",
                          (str(LIB / "does_not_exist.jpg"),))
    db.get_conn().commit()
    p = layout.plan(str(OUT / "ghost"))
    r = layout.start_apply("move"); assert r["started"], r
    pr = wait_apply()
    if pr["failed"] < 1:
        problems.append("[missing] expected >=1 failed step for missing source, got 0")
    else:
        print(f"[missing] handled cleanly: {pr['failed']} failed step(s)")
    layout.undo()

    if problems:
        print("\nFAILURES:")
        for x in problems:
            print(" -", x)
        return 1
    print("\nLAYOUT ENGINE ALL PASSED (copy/move/undo/missing)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    raise SystemExit(code)
