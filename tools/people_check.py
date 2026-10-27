r"""People ("人物") assertions: local face-detection lane + album filtering.

Scope is detection-only (photos that *contain* people), so these checks cover
the slow-lane plumbing, not identity clustering. Two layers:

  1. the *real* detector — OpenCV + its bundled Haar cascades must load and run
     a genuine detection pass over a real grayscale array (fully offline, zero
     model download);
  2. lane logic with a stubbed detector — pending->done transition, video
     exemption (kind='video' is pre-marked done and never counted), face_count
     persistence across a re-scan (content-hash PK, ON CONFLICT DO NOTHING),
     the has_face SQL filter, /api/people + /api/photos?has_face=1, and the
     summary tallies.

Runs on a scratch library + DB; touches no real data.

Run:  .venv\Scripts\python.exe tools\people_check.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"F:\Code\photo-album")

ROOT = Path(r"F:\Code\photo-album")
SCRATCH = ROOT / ".bench" / "_people_check"
LIB = SCRATCH / "lib"
shutil.rmtree(SCRATCH, ignore_errors=True)
LIB.mkdir(parents=True)

import app.config as cfg
cfg.DATA_DIR = SCRATCH / ".data"
cfg.DB_PATH = cfg.DATA_DIR / "t.db"
cfg.THUMB_DIR = cfg.DATA_DIR / "thumbs"
cfg.SHARE_DIR = cfg.DATA_DIR / "share"

from PIL import Image, ImageDraw
import numpy as np
import os

from app import db, ocr
ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}
import app.dups as dups
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.semantic as semantic
semantic.model_present = lambda: False
semantic.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.caption as caption
caption.start = lambda *a, **k: {"started": False, "reason": "test"}
import app.people as people

db.init_db()
conn = db.get_conn()

OK = []
def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    OK.append(name)


# --- 1. real detector: must load + run offline, no exceptions ---------------
check("opencv+haar available", people.available() is True)
check("cascades loaded (non-empty)", bool(people._load_cascades()))
noise = (np.random.rand(200, 200) * 255).astype("uint8")
n_real = people.detect_count(noise)
check("real detector returns int", isinstance(n_real, int) and n_real >= 0)
flat = np.full((160, 160), 128, dtype="uint8")
check("blank image -> 0 faces", people.detect_count(flat) == 0)
# IoU merge drops a near-duplicate box, keeps a disjoint one
merged = people._iou_merge([(0, 0, 60, 60), (4, 4, 60, 60), (200, 200, 50, 50)])
check("iou merge drops overlap, keeps distinct", len(merged) == 2)


def put(name, seed, taken):
    p = LIB / name
    img = Image.new("RGB", (400, 300), (seed * 53 % 256, seed * 97 % 256, 140))
    ImageDraw.Draw(img).ellipse([60, 60, 340, 240], outline=(20, 20, 20))
    img.save(p, quality=90)
    os.utime(p, (taken, taken))
    return p


NOW = time.time()
files = [put(f"p{i}.jpg", i, NOW - i * 3600) for i in range(1, 6)]  # 5 stills

# defer the auto-kicked people lane until ids are known + detector stubbed
_orig_start = people.start_backfill
people.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}

from app import ingest
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("scan indexed 5 stills",
      conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 5)

IDS = [conn.execute("SELECT id FROM photos WHERE path=?", (str(f),)).fetchone()["id"]
       for f in files]

# a video row: pre-marked face_status='done' by ingest, must never be pending.
# kept OUTSIDE LIB so the later re-scan's missing-sweep never touches it.
VID = "a" * 64
db.upsert_photo({"id": VID, "path": str(SCRATCH / "vids" / "clip.mp4"),
                 "kind": "video",
                 "taken_at": NOW, "mtime": NOW, "bytes": 1, "ext": ".mp4",
                 "status": "ok", "ocr_status": "done", "sem_status": "done",
                 "face_status": "done"})
check("video row inserted",
      conn.execute("SELECT face_status FROM photos WHERE id=?", (VID,)).fetchone()[0]
      == "done")

# --- 2. stub the detector for deterministic lane logic ----------------------
FACES = {IDS[0]: 3, IDS[1]: 1, IDS[2]: 2}   # 3 of 5 stills contain people
people._to_gray = lambda fid: fid            # bypass thumbnail decode
people.detect_count = lambda g: FACES.get(g, 0)
people.start_backfill = _orig_start           # restore the real starter

res = people.start_backfill()
check("backfill started", res.get("started") is True and res.get("pending") == 5)
for _ in range(100):
    if not people.progress_snapshot()["running"]:
        break
    time.sleep(0.2)
pr = people.progress_snapshot()
check("backfill finished", pr["running"] is False and pr["error"] is None)
check("only the 5 stills scanned (video exempt)", pr["scanned"] == 5)
check("3 photos flagged with faces", pr["with_faces"] == 3)

row = lambda i: conn.execute("SELECT face_count,face_status FROM photos WHERE id=?",
                             (i,)).fetchone()
check("face_count stored per photo",
      row(IDS[0])["face_count"] == 3 and row(IDS[1])["face_count"] == 1
      and row(IDS[2])["face_count"] == 2)
check("no-face stills stored 0",
      row(IDS[3])["face_count"] == 0 and row(IDS[4])["face_count"] == 0)
check("all stills marked done",
      all(row(i)["face_status"] == "done" for i in IDS))
check("video face_count untouched",
      conn.execute("SELECT face_count FROM photos WHERE id=?", (VID,)).fetchone()[0] is None)

# --- nothing pending now: start_backfill is a cheap no-op -------------------
again = people.start_backfill()
check("idle start is a no-op", again.get("started") is False
      and again.get("reason") == "nothing pending")

# --- re-scan must NOT wipe face results (content-hash PK) -------------------
people.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("re-scan kept face_count", row(IDS[0])["face_count"] == 3)
check("re-scan kept stills 'done'", all(row(i)["face_status"] == "done" for i in IDS))
check("re-scan added no duplicate row",
      conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 6)

# --- has_face SQL filter ----------------------------------------------------
f_ids = {r["id"] for r in db.query_photos(has_face=True)}
check("has_face=True -> the 3 with faces", f_ids == {IDS[0], IDS[1], IDS[2]})
nf_ids = {r["id"] for r in db.query_photos(has_face=False)}
check("has_face=False -> 2 faceless stills (video NULL excluded)",
      nf_ids == {IDS[3], IDS[4]})

# --- summary tallies --------------------------------------------------------
s = people.summary()
check("summary with_faces=3", s["with_faces"] == 3)
check("summary faces=6 (sum of counts)", s["faces"] == 6)
check("summary scanned=5", s["scanned"] == 5)
check("summary pending=0", s["pending"] == 0)
check("summary available", s["available"] is True)

# --- HTTP surface -----------------------------------------------------------
people.start_backfill = _orig_start            # real starter for the route
from fastapi.testclient import TestClient
import app.server as server
client = TestClient(app=server.app, raise_server_exceptions=False)

pj = client.get("/api/people").json()
check("GET /api/people with_faces", pj["with_faces"] == 3 and pj["faces"] == 6)
check("GET /api/people available", pj["available"] is True)

hp = client.get("/api/photos?has_face=1&limit=1000").json()
check("has_face=1 items=3", len(hp["items"]) == 3)
check("items carry face_count",
      all(it["face_count"] >= 1 for it in hp["items"]))
check("has_face=1 never includes the video",
      VID not in {it["id"] for it in hp["items"]})

hf = client.get("/api/photos?has_face=0&limit=1000").json()
check("has_face=0 items=2", len(hf["items"]) == 2)

ps = client.post("/api/people/scan").json()
check("POST /api/people/scan idle -> not started",
      ps.get("started") is False and ps.get("reason") == "nothing pending")

print(f"\npeople_check: {len(OK)} assertions passed")
