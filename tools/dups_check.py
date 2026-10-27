"""Near-duplicate (pHash) verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe tools/dups_check.py

Covers:
  - fast path: phash computed during the normal scan, groups assigned right away
  - the original + its near-dupes (resize / recompress / brightness / grayscale)
    land in ONE group
  - visually different images stay ungrouped (no false merges)
  - backfill path: clearing phash then start_backfill() reproduces the grouping
  - API: /api/dups shape, /api/status dupes counters, dup_group in /api/photos
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- isolate: rewrite config paths BEFORE importing db/dups/ingest ----
import app.config as config  # noqa: E402

SCRATCH = ROOT / ".bench" / "_dups_test"
LIB = SCRATCH / "library"
DATA = SCRATCH / "data"
for d in (LIB, DATA):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
config.DATA_DIR = DATA
config.DB_PATH = DATA / "test.db"
config.THUMB_DIR = DATA / "thumbs"

from PIL import Image, ImageDraw, ImageEnhance  # noqa: E402

from app import db, dups, ingest  # noqa: E402


def scene(seed: int, w: int = 800, h: int = 600) -> Image.Image:
    """Structured, photo-ish content: gradient sky + shapes (stable for pHash)."""
    import random
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 8):  # coarse vertical gradient, fast enough
            c = (120 + y // 6 + seed * 7 % 40, 90 + x // 10, 200 - y // 3)
            for dx in range(min(8, w - x)):
                px[x + dx, y] = c
    d = ImageDraw.Draw(img)
    for _ in range(8):
        x0, y0 = rnd.randint(0, w - 120), rnd.randint(0, h - 120)
        box = [x0, y0, x0 + rnd.randint(40, 120), y0 + rnd.randint(40, 120)]
        color = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        if rnd.random() < 0.5:
            d.ellipse(box, fill=color)
        else:
            d.rectangle(box, fill=color)
    d.line([0, h, w // 2, h // 3, w, h], fill=(30, 30, 30), width=18)
    return img


def make_lib() -> dict:
    """Returns {logical_name: path}. family = original + 4 near-dupes."""
    paths = {}

    fam = LIB / "family"
    fam.mkdir()
    base = scene(7)
    p = fam / "original.jpg"; base.save(p, "JPEG", quality=92); paths["original"] = p
    p = fam / "resized.jpg"; base.resize((640, 480)).save(p, "JPEG", quality=88); paths["resize"] = p
    p = fam / "recompressed.jpg"; base.save(p, "JPEG", quality=50); paths["recompress"] = p
    p = fam / "darker.png"; ImageEnhance.Brightness(base).enhance(0.78).save(p); paths["brightness"] = p
    p = fam / "gray.jpg"; base.convert("L").convert("RGB").save(p, "JPEG", quality=85); paths["gray"] = p

    solo = LIB / "others"
    solo.mkdir()
    for i in range(8):
        p = solo / f"other_{i}.jpg"
        scene(100 + i * 13).save(p, "JPEG", quality=85)
        paths[f"other_{i}"] = p

    # spread taken_at so paging/ordering behaves like a real library
    t = time.time()
    for k, p in paths.items():
        import os
        off = 40 * 86400 * (abs(hash(k)) % 30)
        os.utime(p, (t - off, t - off))
    return paths


def group_map() -> dict:
    rows = db.get_conn().execute("SELECT id, path, dup_group FROM photos").fetchall()
    return {r["path"]: r["dup_group"] for r in rows}


def wait(fn, label: str = "") -> None:
    t0 = time.time()
    while fn():
        time.sleep(0.1)
        if time.time() - t0 > 60:
            print(f"!! wait timed out: {label}", flush=True)
            return


def main() -> int:
    problems = []
    paths = make_lib()
    print(f"library built: {len(paths)} files", flush=True)
    fam_names = ["original", "resize", "recompress", "brightness", "gray"]
    other_names = [k for k in paths if k.startswith("other_")]

    # OCR engine would add 20+s of model loading; this check is about hashes
    import app.ocr as ocr
    ocr.start_ocr = lambda *a, **k: {"started": False}

    db.init_db()
    dups.ensure_phash_schema()
    ingest.start_scan(str(LIB))
    wait(lambda: ingest.PROGRESS["running"] or dups.PROGRESS["running"], "scan+autobackfill")
    print(f"scanned: indexed={ingest.PROGRESS['indexed']} dup_exact={ingest.PROGRESS['duplicated']}")
    print(f"scan error: {ingest.PROGRESS['error']}")
    print(f"dups progress: {dups.progress_snapshot()}")

    # --- 1. fast path filled phash for every readable image ---
    conn = db.get_conn()
    no_hash = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE phash IS NULL AND status='ok'").fetchone()["c"]
    if no_hash:
        problems.append(f"[fastpath] {no_hash} rows missing phash after scan (ingest not wired?)")
    g = group_map()

    # --- 2. family collapses into exactly one group ---
    fam_groups = {g[str(p)] for p in (paths[n] for n in fam_names)}
    if fam_groups == {None} or None in fam_groups:
        problems.append(f"[family] some variants ungrouped: {fam_groups}")
    elif len(fam_groups) != 1:
        problems.append(f"[family] variants split across groups: {fam_groups}")
    else:
        gid = fam_groups.pop()
        members = [k for k, v in g.items() if v == gid]
        print(f"[family] OK: {len(fam_names)} variants in one group ({len(members)} members)")
        if len(members) != len(fam_names):
            problems.append(f"[family] group has {len(members)} members, expected {len(fam_names)}")
        # leader must be a group member with dup_group set too
        lead = conn.execute("SELECT id FROM photos WHERE dup_group=?", (gid,)).fetchone()
        if lead and lead["id"] != gid:
            pass  # gid is leader's id by construction; leader row also carries dup_group
        if conn.execute("SELECT COUNT(*) c FROM photos WHERE id=? AND dup_group=?",
                        (gid, gid)).fetchone()["c"] != 1:
            problems.append("[family] leader not present in its own group")

    # --- 3. no false merges: different scenes stay solo ---
    for n in other_names:
        if g[str(paths[n])] is not None:
            problems.append(f"[strict] {n} got grouped with someone (false positive)")
    grouped = sum(1 for v in g.values() if v)
    print(f"[strict] {len(other_names)} distinct scenes ungrouped; total grouped={grouped}")

    # --- 4. backfill path reproduces the grouping ---
    before = {k: v for k, v in g.items()}
    conn.execute("UPDATE photos SET phash=NULL, dup_group=NULL")
    conn.commit()
    r = dups.start_backfill()
    assert r.get("started"), r
    wait(lambda: dups.PROGRESS["running"], "manual backfill")
    print(f"[backfill] done={dups.PROGRESS['backfilled']} failed={dups.PROGRESS['failed']} "
          f"groups={dups.PROGRESS['groups']} dupes={dups.PROGRESS['dupes']}")
    after = group_map()
    part_before = {}
    part_after = {}
    for path, gid in before.items():
        if gid:
            part_before.setdefault(gid, set()).add(path)
    for path, gid in after.items():
        if gid:
            part_after.setdefault(gid, set()).add(path)
    if {frozenset(v) for v in part_before.values()} != {frozenset(v) for v in part_after.values()}:
        problems.append("[backfill] grouping differs from fast-path grouping")
    else:
        print("[backfill] identical partition to fast path")

    # --- 5. API shape ---
    from fastapi.testclient import TestClient
    from app.server import app as server_app
    client = TestClient(server_app)
    d = client.get("/api/dups").json()
    if d["count"] != 1 or d["dupes"] != 5:
        problems.append(f"[/api/dups] count={d['count']} dupes={d['dupes']}, expected 1/5")
    grp = d["groups"][0]
    ids = [i["id"] for i in grp["items"]]
    if grp["group_id"] not in ids:
        problems.append("[/api/dups] leader id missing from items")
    s = client.get("/api/status").json()
    if s["db"].get("dupes") != 5 or s["db"].get("dup_groups") != 1:
        problems.append(f"[/api/status] dup counters wrong: {s['db']}")
    if s["db"].get("hashed") != 13:
        problems.append(f"[/api/status] hashed={s['db'].get('hashed')} expected 13")
    ph = client.get("/api/photos?limit=100").json()
    with_flag = [i for i in ph["items"] if i.get("dup_group")]
    if len(with_flag) != 5:
        problems.append(f"[/api/photos] {len(with_flag)} items carry dup_group, expected 5")
    print(f"[api] /api/dups groups={d['count']} dupes={d['dupes']}; "
          f"photos items with dup flag={len(with_flag)}")

    if problems:
        print("\nFAILURES:")
        for x in problems:
            print(" -", x)
        return 1
    print("\nPHASH DEDUP ALL PASSED (fast path / family group / strictness / backfill / API)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    raise SystemExit(code)
