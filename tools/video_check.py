r"""Video/GIF pipeline assertions on a scratch library + scratch DB.

Covers: ffmpeg poster ingest, slow-lane exemptions, /media Range streaming,
albums video bucket, broken video, watch picking up new videos.
Run:  .venv\Scripts\python.exe tools\video_check.py
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"F:\Code\photo-album")

ROOT = Path(r"F:\Code\photo-album")
SCRATCH = ROOT / ".bench" / "_video_test"
LIB = SCRATCH / "lib"
shutil.rmtree(SCRATCH, ignore_errors=True)
LIB.mkdir(parents=True)

import app.config as cfg
cfg.DATA_DIR = SCRATCH / ".data"
cfg.DB_PATH = cfg.DATA_DIR / "t.db"
cfg.THUMB_DIR = cfg.DATA_DIR / "thumbs"
cfg.SHARE_DIR = cfg.DATA_DIR / "share"

from app import db, media
from app import ingest

OK = []
def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    OK.append(name)

exe = media.ffmpeg_exe()
check("ffmpeg exe available", exe and Path(exe).exists())

# --- fixtures ------------------------------------------------------------
vid = LIB / "clip.mp4"
subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                "-pix_fmt", "yuv420p", str(vid)], check=True)
gif = LIB / "anim.gif"
from PIL import Image
frames = [Image.new("P", (64, 64), i * 40) for i in range(4)]
frames[0].save(gif, save_all=True, append_images=frames[1:], duration=200, loop=0)
jpg = LIB / "photo.jpg"
Image.new("RGB", (200, 120), (60, 90, 120)).save(jpg, quality=90)
bad = LIB / "broken.mp4"
bad.write_bytes(b"not a video at all, definitely not decodable" * 40)

db.init_db()
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
snap = ingest.progress_snapshot()
check("scan saw 4 files", snap["seen"] == 4)
check("scan indexed 4", snap["indexed"] == 4)
check("scan clean of lane errors", snap["error"] is None)

conn = db.get_conn()
def row(p):
    return conn.execute("SELECT * FROM photos WHERE path=?", (str(p),)).fetchone()

vr, gr, jr, br = row(vid), row(gif), row(jpg), row(bad)
check("video row exists", vr is not None and vr["kind"] == "video")
check("video status ok", vr["status"] == "ok")
check("video duration from probe", vr["duration"] and 1.5 < vr["duration"] < 2.6)
check("video wh from probe", (vr["width"], vr["height"]) == (320, 240))
check("video ext", vr["ext"] == ".mp4")
check("video opts out of OCR lane", vr["ocr_status"] == "done")
check("video opts out of semantic lane", vr["sem_status"] == "done")
check("video has no phash", vr["phash"] is None)
check("video caption lane off", vr["caption_status"] == "off")
check("poster thumbs built",
      cfg.thumb_path(vr["id"], 512).exists() and cfg.thumb_path(vr["id"], 128).exists())
from PIL import Image as PILImage
with PILImage.open(cfg.thumb_path(vr["id"], 512)) as t:
    check("poster is a real frame (colourful)", t.size[0] <= 512 and len(t.getcolors(1 << 16) or []) > 3)
check("gif stays an image kind", gr["kind"] != "video" and gr["ext"] == ".gif")
check("gif still lanes normally (pending)", gr["ocr_status"] == "pending")
check("broken video registered", br["status"] == "broken" and br["kind"] == "video")
check("broken video opts out too", br["ocr_status"] == "done" and br["sem_status"] == "done")

# dups backfill must skip videos entirely
n = conn.execute("SELECT COUNT(*) c FROM photos WHERE phash IS NULL AND status='ok'"
                 " AND kind!='video'").fetchone()["c"]
check("dups backfill todo excludes video", n == 0)  # fresh rows carry phash; mp4 never gets one

# --- API layer -----------------------------------------------------------
from fastapi.testclient import TestClient
from app import server
client = TestClient(server.app)

r = client.get(f"/media/{vr['id']}")
check("/media full stream 200", r.status_code == 200)
check("/media content-type", r.headers.get("content-type") == "video/mp4")
check("/media bytes exact", len(r.content) == vid.stat().st_size)
size = vid.stat().st_size
r = client.get(f"/media/{vr['id']}", headers={"Range": f"bytes=0-{min(99, size - 1)}"})
check("/media Range -> 206", r.status_code == 206)
check("/media content-range", r.headers.get("content-range")
      == f"bytes 0-{min(99, size - 1)}/{size}")
r = client.get(f"/media/{gr['id']}")
check("/media gif type", r.status_code == 200 and r.headers.get("content-type") == "image/gif")
check("/media unknown id 404", client.get("/media/" + "f" * 64).status_code == 404)
check("/media bad id 400", client.get("/media/short").status_code == 400)
# a row whose file was deleted -> gone通路 404, not a crash
# (must be *different content*: identical bytes dedup onto clip.mp4's row)
tmp = LIB / "gone.mp4"
subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-f", "lavfi", "-i", "rgbtestsrc=duration=1:size=160x120:rate=10",
                "-shortest", "-pix_fmt", "yuv420p", str(tmp)], check=True)
ingest.process_file(str(tmp))
tid = conn.execute("SELECT id FROM photos WHERE path=?", (str(tmp),)).fetchone()["id"]
tmp.unlink()
db.sweep_missing(str(LIB))  # watcher was down: sweep makes the row honest
check("/media deleted file 404", client.get(f"/media/{tid}").status_code == 404)

a = client.get("/api/albums").json()
vc = next(c for c in a["smart"] if c["key"] == "video")
check("albums video count", vc["count"] == 1)
check("albums video cover is a live video", vc["cover"] is not None)
names = [c["name"] for c in a["smart"]]
check("video album after camera", names.index("视频") == names.index("相机") + 1)

p = client.get("/api/photos?kind=video").json()
it = next(i for i in p["items"] if i["id"] == vr["id"])
check("photos kind=video finds the mp4", it is not None)
bok = client.get("/api/photos?kind=video&status=ok").json()
check("photos status=ok excludes broken video",
      br["id"] not in {i["id"] for i in bok["items"]}
      and vr["id"] in {i["id"] for i in bok["items"]})
brk = client.get("/api/photos?kind=video&status=broken").json()
check("photos status=broken shows it", {i["id"] for i in brk["items"]} == {br["id"]})
check("list item carries ext+duration", it["ext"] == ".mp4" and it["duration"])
pg = client.get("/api/photos").json()
ids = {i["id"] for i in pg["items"]}
check("paged timeline includes video+gif+broken", {vr["id"], gr["id"], br["id"]} <= ids)
for i in pg["items"]:
    check("paged items have ext", "ext" in i and "duration" in i)
    break
s = client.get("/api/search?q=clip").json()
hit = [i for i in s["items"] if i["id"] == vr["id"]]
check("filename search finds video", hit and "file" in hit[0]["source"])
check("search items carry duration badge data", hit[0]["duration"])

d = client.get(f"/api/photo/{vr['id']}").json()
check("detail has duration/kind/exists", d["duration"] and d["kind"] == "video" and d["exists"])

g = client.get("/api/grouped?level=day").json()
day_ids = {i["id"] for grp in g["groups"] for i in grp.get("items", [])} or \
          {i["id"] for grp in g["groups"] for i in []}
lvs = [(grp["key"], grp["count"]) for grp in g["groups"]]
check("grouped day covers the folder", sum(c for _, c in lvs) >= 4)

# --- watch picks up videos ------------------------------------------------
from app import watch
watch.start(str(LIB))
vid2 = LIB / "clip2.mp4"
subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=1:size=160x120:rate=10",
                "-pix_fmt", "yuv420p", str(vid2)], check=True)
deadline = time.time() + 20
found = None
while time.time() < deadline:
    found = conn.execute("SELECT * FROM photos WHERE path=?", (str(vid2),)).fetchone()
    if found:
        break
    time.sleep(0.5)
check("watch ingests new video", found is not None and found["kind"] == "video")
watch.stop(str(LIB))

print(f"video_check: {len(OK)} assertions passed")
