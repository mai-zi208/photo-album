"""Notes + detail-card verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe -u tools/notes_check.py

Exercises the note bypass field end to end through the real API:
  save/clear note; detail endpoint (ocr_raw, duplicates, exists);
  /api/search merging (ocr | note | ocr+note sources); note badge data
  in /api/photos; a note survives a file move (content-hash identity);
  unknown id -> 404.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- isolate BEFORE importing db/ingest/watch/server ----
import app.config as config  # noqa: E402

SCRATCH = ROOT / ".bench" / "_notes_test"
LIB = SCRATCH / "library"
DATA = SCRATCH / "data"
for d in (SCRATCH,):
    if d.exists():
        shutil.rmtree(d)
LIB.mkdir(parents=True)
DATA.mkdir(parents=True)
config.DATA_DIR = DATA
config.DB_PATH = DATA / "test.db"
config.THUMB_DIR = DATA / "thumbs"

from PIL import Image  # noqa: E402

from app import db, ingest, ocr, watch  # noqa: E402

ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}
import app.dups as dups  # noqa: E402
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.semantic as semantic  # noqa: E402
semantic.model_present = lambda: False  # isolate the text lane from CLIP hits
semantic.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}

watch.QUIET_SEC = 0.5

from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402 (after patches: scan won't launch OCR/phash)

client = TestClient(server.app)


def put(path: Path, seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (300, 200), (seed * 40 % 256, seed * 11 % 256, 90))
    img.save(path, "JPEG", quality=90)
    return path


def until(pred, timeout=25.0, why=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError(f"timeout waiting: {why}")


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS {msg}")


# ---- build a small library ----
paths = [put(LIB / f"n{i:02d}.jpg", i) for i in range(6)]
ingest.start_scan(str(LIB))
until(lambda: not ingest.PROGRESS["running"], why="scan finish")
db.init_db()
ids = [r["id"] for r in db.get_conn().execute(
    "SELECT id FROM photos ORDER BY id").fetchall()]
ok(len(ids) == 6, f"6 photos indexed, ids {len(ids)}")

# seed fake OCR text for ids[0] (engine patched off; exercise the merge path)
ocr.save_result(ids[0], "季度合同审阅 meeting notes", True)

# ---- 1. save a note via API ----
r = client.post("/api/photo/note", json={"id": ids[1], "note": "  家庭聚餐 孩子长大了  "})
ok(r.status_code == 200, "POST note 200")
ok(r.json()["note"] == "家庭聚餐 孩子长大了", "note trimmed on save")

# ---- 2. detail endpoint ----
d = client.get(f"/api/photo/{ids[1]}").json()
ok(d["note"] == "家庭聚餐 孩子长大了", "detail carries note")
ok(d["exists"] is True and d["status"] == "ok", "detail exists/status")
ok(d["ocr_raw"] is None and d["duplicates"] == [], "detail empty ocr/dups shape")
d0 = client.get(f"/api/photo/{ids[0]}").json()
ok("合同" in (d0["ocr_raw"] or ""), "detail ocr_raw from fts")

bad = client.get("/api/photo/" + "0" * 64)
ok(bad.status_code == 404, "unknown id -> 404")
ok(client.get("/api/photo/short").status_code == 400, "bad id -> 400")

# ---- 3. search merges ocr + note hits ----
s = client.get("/api/search", params={"q": "合同"}).json()
ok(any(i["id"] == ids[0] and i["source"] == "ocr" for i in s["items"]),
   "ocr hit keeps source=ocr")

s = client.get("/api/search", params={"q": "聚餐"}).json()
hits = [i for i in s["items"] if i["id"] == ids[1]]
ok(hits and hits[0]["source"] == "note" and hits[0]["note"] == "家庭聚餐 孩子长大了",
   "note-only hit source=note with note field")

client.post("/api/photo/note", json={"id": ids[0], "note": "合同扫描件"})
s = client.get("/api/search", params={"q": "合同"}).json()
both = [i for i in s["items"] if i["id"] == ids[0]]
ok(both and "ocr" in both[0]["source"] and "note" in both[0]["source"]
   and both[0]["ocr_snippet"],
   "photo hit by ocr AND note -> source=ocr+note, snippet kept")

# ---- 4. /api/photos exposes note for tile badges ----
p = client.get("/api/photos", params={"limit": 100}).json()
by_id = {i["id"]: i for i in p["items"]}
ok(by_id[ids[1]].get("note") == "家庭聚餐 孩子长大了", "photos page includes note")
ok(by_id[ids[2]].get("note") is None, "photos page: note None when unset")

# ---- 5. note survives a move (identity = content hash) ----
def row_by_id(file_id):
    r = db.get_conn().execute("SELECT * FROM photos WHERE id=?", (file_id,)).fetchone()
    return dict(r) if r else None

src = Path(row_by_id(ids[1])["path"])   # ids are hash-ordered; locate the real file
moved = LIB / "sub" / (src.stem + "_moved" + src.suffix)
moved.parent.mkdir(parents=True, exist_ok=True)
src.rename(moved)
until(lambda: row_by_id(ids[1])["path"] == str(moved), why="watcher re-point")
d = client.get(f"/api/photo/{ids[1]}").json()
ok(d["note"] == "家庭聚餐 孩子长大了" and d["exists"] is True,
   "note survived move; detail re-points to new path")

# ---- 6. clear the note ----
client.post("/api/photo/note", json={"id": ids[1], "note": "   "})
ok(row_by_id(ids[1])["note"] is None, "blank note stored as NULL")
s = client.get("/api/search", params={"q": "聚餐"}).json()
ok(not any(i["id"] == ids[1] for i in s["items"]), "cleared note no longer searched")

# ---- 7. stats counts notes ----
st = db.stats()
ok(st["notes"] == 1, f"stats.notes==1 (got {st['notes']})")

print("\nnotes check: ALL PASSED")

