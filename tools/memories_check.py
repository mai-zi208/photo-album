r"""Memories ("精彩回忆") assertions: soundtrack, ffmpeg slideshow export, API.

Covers: bundled track listing + path whitelist, source selection (only
viewable stills, video/missing excluded, caller order preserved), export
guards (bad track / empty selection), a real end-to-end render that produces
a probe-able mp4 with the right length, per-job status/file lookups, and the
HTTP surface. Runs on a scratch library + DB; touches no real data.

Run:  .venv\Scripts\python.exe tools\memories_check.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"F:\Code\photo-album")

ROOT = Path(r"F:\Code\photo-album")
SCRATCH = ROOT / ".bench" / "_memories_check"
LIB = SCRATCH / "lib"
shutil.rmtree(SCRATCH, ignore_errors=True)
LIB.mkdir(parents=True)

import app.config as cfg
cfg.DATA_DIR = SCRATCH / ".data"
cfg.DB_PATH = cfg.DATA_DIR / "t.db"
cfg.THUMB_DIR = cfg.DATA_DIR / "thumbs"
cfg.SHARE_DIR = cfg.DATA_DIR / "share"
cfg.EXPORT_DIR = cfg.DATA_DIR / "memories"      # keep renders off the real disk

from PIL import Image, ImageDraw
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
import app.media as media
import app.memories as memories
# memories bound EXPORT_DIR at import; re-point to the scratch dir
memories.EXPORT_DIR = cfg.EXPORT_DIR

db.init_db()
conn = db.get_conn()

OK = []
def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    OK.append(name)


def put(name, seed, taken):
    p = LIB / name
    img = Image.new("RGB", (640, 400), (seed * 37 % 256, seed * 61 % 256, 90))
    ImageDraw.Draw(img).rectangle([30, 30, 600, 360], outline=(250, 250, 250))
    img.save(p, quality=90)
    os.utime(p, (taken, taken))
    return p


NOW = time.time()
files = [put(f"m{i}.jpg", i, NOW - i * 86400) for i in range(1, 5)]  # 4 stills

from app import ingest
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("scan indexed 4",
      conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 4)

IDS = [conn.execute("SELECT id FROM photos WHERE path=?", (str(f),)).fetchone()["id"]
       for f in files]

# --- soundtrack ------------------------------------------------------------
tr = memories.tracks()
check("3 bundled tracks", len(tr) == 3)
check("track shape", all({"name", "url", "duration"} <= set(t) for t in tr)
      and all(t["url"].startswith("/api/memories/track/") for t in tr))
check("probe gave durations", all(t["duration"] and t["duration"] > 3 for t in tr))
check("track_file whitelist", memories.track_file("warm") is not None)
check("track_file traversal blocked", memories.track_file("../evil") is None)
check("track_file unknown", memories.track_file("nope") is None)

# --- source selection ------------------------------------------------------
src = memories._sources(IDS)
check("sources = 4 stills", len(src) == 4)
check("caller order preserved", [s["id"] for s in src] == IDS)
check("sources use existing thumbs", all(Path(s["path"]).is_file() for s in src))

# a video member must be skipped even if requested
vid = "v" * 64
conn.execute("INSERT INTO photos(id,path,kind,taken_at,mtime,status,indexed_at)"
             " VALUES(?,?,?,?,?,?,?)",
             (vid, str(LIB / "clip.mp4"), "video", NOW, NOW, "ok", NOW))
conn.commit()
check("video excluded from sources",
      vid not in [s["id"] for s in memories._sources([vid] + IDS)])
# a missing-thumbnail / unknown id is dropped
check("unknown id dropped", len(memories._sources([IDS[0], "f" * 64])) == 1)
check("empty ids -> no sources", memories._sources([]) == [])

# --- export guards ---------------------------------------------------------
try:
    memories.start_export(IDS, track="does-not-exist")
    check("bad track rejected", False)
except memories.ExportError:
    check("bad track rejected", True)
try:
    memories.start_export(["f" * 64])       # nothing viewable
    check("empty selection rejected", False)
except memories.ExportError:
    check("empty selection rejected", True)

# --- real end-to-end render ------------------------------------------------
job = memories.start_export(IDS, track="warm", each=1.6, xfade=0.6)
check("export started", job["photos"] == 4 and job["job_id"])
check("total length math", abs(job["seconds"] - (4 * 1.6 - 3 * 0.6)) < 0.01)
st = None
for _ in range(160):
    st = memories.status(job["job_id"])
    if st and st["state"] in ("done", "error"):
        break
    time.sleep(0.5)
check("export finished", st and st["state"] == "done")
check("ids hidden in status", "ids" not in st)
fp = memories.file_path(job["job_id"])
check("output file exists", fp is not None and fp.is_file())
check("output non-trivial size", st["size"] > 40_000)
pr = media.probe(str(fp)) or {}
check("mp4 has video dimensions", pr.get("width") == 1280 and pr.get("height") == 720)
check("mp4 duration matches plan",
      pr.get("duration") and abs(pr["duration"] - 4.6) < 0.9)
check("status unknown job -> None", memories.status("deadbeef0000") is None)
check("file_path before done -> None", memories.file_path("deadbeef0000") is None)

# --- HTTP surface ----------------------------------------------------------
from fastapi.testclient import TestClient
import app.server as server
c = TestClient(server.app)

r = c.get("/api/memories/tracks")
check("GET tracks 200", r.status_code == 200 and len(r.json()["tracks"]) == 3)
r = c.get("/api/memories/track/warm")
check("GET track stream 200", r.status_code == 200 and "audio" in r.headers["content-type"])
check("GET track bytes present", len(r.content) > 1000)
check("GET track range 206",
      c.get("/api/memories/track/warm", headers={"Range": "bytes=0-100"}).status_code == 206)
check("GET track unknown -> 404", c.get("/api/memories/track/nope").status_code == 404)
check("POST export no ids -> 400", c.post("/api/memories/export", json={"ids": []}).status_code == 400)
check("POST export bad id -> 400",
      c.post("/api/memories/export", json={"ids": ["x"]}).status_code == 400)
check("POST export bad track -> 400",
      c.post("/api/memories/export", json={"ids": IDS, "track": "zzz"}).status_code == 400)
r = c.post("/api/memories/export", json={"ids": IDS[:2], "track": "night",
                                         "each": 1.5, "xfade": 0.5})
check("POST export ok", r.status_code == 200 and r.json()["job_id"])
jid = r.json()["job_id"]
check("GET status ok", c.get(f"/api/memories/status/{jid}").status_code == 200)
check("GET status unknown -> 404", c.get("/api/memories/status/nope").status_code == 404)
# wait for the http-job to finish, then download
done = None
for _ in range(160):
    done = c.get(f"/api/memories/status/{jid}").json()
    if done["state"] in ("done", "error"):
        break
    time.sleep(0.5)
check("http export done", done and done["state"] == "done")
dl = c.get(f"/api/memories/file/{jid}")
check("GET file streams mp4", dl.status_code == 200 and "video" in dl.headers["content-type"])
check("GET file not-ready -> 404", c.get("/api/memories/file/nope").status_code == 404)

memories.cleanup_old(keep_minutes=0)
check("cleanup removes old renders", not list(memories.EXPORT_DIR.glob("memory_*.mp4")))

print(f"memories_check: {len(OK)} assertions passed")
