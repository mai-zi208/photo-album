"""On-this-day verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe -u tools/onthisday_check.py

Builds a small library whose mtimes land on the same MM-DD across several
years, then asserts via GET /api/onthisday:
  groups by year (descending) and orders items newest-first inside a group;
  the current year is EXCLUDED (memories only, not today's own photos);
  other month-days never show up;
  an explicit date=MM-DD parameter works;
  missing rows (status='missing') are excluded;
  count math + year_span string;
  invalid date formats -> 400;
  default (no param) = today's MM-DD.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- isolate BEFORE importing db/ingest/server ----
import app.config as config  # noqa: E402

SCRATCH = ROOT / ".bench" / "_onthisday_test"
LIB = SCRATCH / "library"
DATA = SCRATCH / "data"
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
LIB.mkdir(parents=True)
DATA.mkdir(parents=True)
config.DATA_DIR = DATA
config.DB_PATH = DATA / "test.db"
config.THUMB_DIR = DATA / "thumbs"
config.SHARE_DIR = DATA / "share"
config.SEM_MODEL_DIR = DATA / "models" / "chinese_clip_onnx"  # absent -> no-op

from PIL import Image  # noqa: E402

from app import db, ingest, ocr, watch  # noqa: E402
import app.dups as dups  # noqa: E402

ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.caption as caption  # noqa: E402
caption.start = lambda *a, **k: {"started": False, "reason": "test"}
watch.QUIET_SEC = 0.5

from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402 (after patches)

client = TestClient(server.app)


def put(path: Path, seed: int, when: tuple) -> Path:
    """Make a JPEG whose mtime (-> taken_at) is the local time `when`.
    `when` = (year, month, day, hour, minute)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (300, 200), (seed * 40 % 256, seed * 11 % 256, 90))
    img.save(path, "JPEG", quality=90)
    y, mo, dd, hh, mi = when
    ts = time.mktime((y, mo, dd, hh, mi, 0, 0, 0, -1))
    os.utime(path, (ts, ts))
    return path


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS {msg}")


def until(pred, timeout=25.0, why=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError(f"timeout waiting: {why}")


THIS_YEAR = time.localtime().tm_year
MD = (6, 15)  # test month-day, midday times so TZ drift can't shift the day

# ---- build library: same MM-DD across 3 past years + current year + other day
put(LIB / "a2020.jpg", 1, (2020, *MD, 10, 0))
put(LIB / "b2024late.jpg", 2, (2024, *MD, 20, 0))
put(LIB / "c2024early.jpg", 3, (2024, *MD, 8, 0))
put(LIB / "d2026.jpg", 4, (THIS_YEAR, *MD, 12, 0))       # must be excluded
put(LIB / "e2022otherday.jpg", 5, (2022, 7, 15, 12, 0))  # different MM-DD
put(LIB / "f2019gone.jpg", 6, (2019, *MD, 9, 0))         # will be 'missing'

ingest.start_scan(str(LIB))
until(lambda: not ingest.PROGRESS["running"], why="scan finish")
db.init_db()
rows = {Path(r["path"]).name: dict(r)
        for r in db.get_conn().execute("SELECT * FROM photos").fetchall()}
ok(len(rows) == 6, "6 photos indexed")
# taken_at must come from mtime (no EXIF on generated jpegs)
ok(time.localtime(rows["a2020.jpg"]["taken_at"]).tm_year == 2020,
   "mtime drives taken_at")

# ---- 1. grouping on an explicit date ----
d = client.get("/api/onthisday?date=06-15").json()
ok(d["date"] == "06-15", "date echo")
years = [g["year"] for g in d["years"]]
ok(years == sorted(years, reverse=True) and years == [2024, 2020, 2019],
   f"years grouped descending: {years}")
ok(THIS_YEAR not in years, f"current year {THIS_YEAR} excluded")
counts = {g["year"]: g["count"] for g in d["years"]}
ok(counts == {2024: 2, 2020: 1, 2019: 1}, f"per-year counts {counts}")
ok(d["count"] == sum(counts.values()), "total count math")
ok(d["year_span"] == "2019–2024", f"year_span = {d['year_span']!r}")

# ---- 2. items newest-first inside a group ----
g24 = next(g for g in d["years"] if g["year"] == 2024)
names = [Path(it["path"]).name for it in g24["items"]]
ok(names == ["b2024late.jpg", "c2024early.jpg"], f"order inside group {names}")

# ---- 3. other month-days never leak ----
ok(client.get("/api/onthisday?date=07-15").json()["count"] == 1,
   "07-15 sees only the 2022 photo")

# ---- 4. missing rows excluded ----
d0 = client.get("/api/onthisday?date=06-15").json()
ok(any(g["year"] == 2019 for g in d0["years"]), "2019 present before deletion")
conn = db.get_conn()
conn.execute("UPDATE photos SET status='missing' WHERE id=?",
             (rows["f2019gone.jpg"]["id"],))
conn.commit()
d1 = client.get("/api/onthisday?date=06-15").json()
ok(all(g["year"] != 2019 for g in d1["years"])
   and d1["count"] == d0["count"] - 1,
   "missing row drops out of on-this-day")

# ---- 5. single-past-year has no span ----
s = client.get("/api/onthisday?date=06-15").json()
one = client.get("/api/onthisday?date=07-15").json()
ok(one["year_span"] == "" and len(one["years"]) == 1,
   "single year -> empty year_span")

# ---- 6. invalid dates -> 400 ----
for bad in ("2024-06-15", "6-15", "13-01", "06-32", "ab-cd", "0615"):
    r = client.get(f"/api/onthisday?date={bad}")
    ok(r.status_code == 400, f"bad date {bad!r} -> 400")

# ---- 7. default = today's MM-DD ----
today = time.strftime("%m-%d", time.localtime())
dfault = client.get("/api/onthisday").json()
ok(dfault["date"] == today, f"default date = today ({today})")
ok(all(g["year"] != THIS_YEAR for g in dfault["years"]),
   "default view also excludes this year")

# ---- 8. item shape: only fields the UI needs ----
sample = d["years"][0]["items"][0]
for key in ("id", "path", "kind", "taken_at", "status", "width", "height"):
    ok(key in sample, f"item carries {key}")

print("\nonthisday check: ALL PASSED")
