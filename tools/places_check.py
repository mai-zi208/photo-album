r"""Places ("地点") assertions: EXIF GPS -> offline city reverse lookup.

The library's real photos carry no GPS (measured 2026-10-25: 0 / 2034), so this
suite builds *synthetic GPS EXIF* JPEGs with Pillow and exercises the whole
path: header parsing (N/S/E/W + DMS rationals), nearest-city math, the 80 km
honest threshold, ingest-time storage, the slow-lane backfill for rows indexed
before the feature, video exemption, re-scan persistence, SQL/HTTP filtering,
the /api/places grouping and the detail payload.

Runs on a scratch library + DB; touches no real data.

Run:  .venv\Scripts\python.exe tools\places_check.py
"""
import shutil
import sys
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, r"F:\Code\photo-album")

ROOT = Path(r"F:\Code\photo-album")
SCRATCH = ROOT / ".bench" / "_places_check"
LIB = SCRATCH / "lib"
EXTRA = SCRATCH / "extra"
shutil.rmtree(SCRATCH, ignore_errors=True)
LIB.mkdir(parents=True)
EXTRA.mkdir(parents=True)

import app.config as cfg
cfg.DATA_DIR = SCRATCH / ".data"
cfg.DB_PATH = cfg.DATA_DIR / "t.db"
cfg.THUMB_DIR = cfg.DATA_DIR / "thumbs"
cfg.SHARE_DIR = cfg.DATA_DIR / "share"

import os

from PIL import Image

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
people.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.places as places

_real_places_start = places.start_backfill
places.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}

db.init_db()
conn = db.get_conn()

OK = []
def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    OK.append(name)


# --- synthetic GPS EXIF -----------------------------------------------------
def _dms(v):
    v = abs(v)
    d = int(v)
    m = int((v - d) * 60)
    s = ((v - d) * 60 - m) * 60
    return (Fraction(d, 1), Fraction(m, 1),
            Fraction(s).limit_denominator(1000000))


def make_gps_jpg(path: Path, lat: float, lon: float, rgb=(60, 90, 120)):
    img = Image.new("RGB", (64, 48), rgb)
    ex = Image.Exif()
    gps = ex.get_ifd(0x8825)
    gps[1] = "S" if lat < 0 else "N"
    gps[2] = _dms(lat)
    gps[3] = "W" if lon < 0 else "E"
    gps[4] = _dms(lon)
    img.save(path, "JPEG", exif=ex)
    return path


def make_plain_jpg(path: Path, rgb=(150, 70, 40)):
    Image.new("RGB", (64, 48), rgb).save(path, "JPEG")
    return path


# --- 1. EXIF parsing + city math (pure, no DB) -----------------------------
probe = make_gps_jpg(EXTRA / "probe.jpg", 39.9042, 116.4074)
with Image.open(probe) as im:
    fix = places.gps_from_exif(im)
check("GPS IFD parsed", fix is not None)
check("latitude decoded (DMS->decimal)", abs(fix[0] - 39.9042) < 1e-3)
check("longitude decoded (DMS->decimal)", abs(fix[1] - 116.4074) < 1e-3)

plain = make_plain_jpg(EXTRA / "plain.jpg")
with Image.open(plain) as im:
    check("no GPS IFD -> None", places.gps_from_exif(im) is None)

south = make_gps_jpg(EXTRA / "south.jpg", -33.8688, 151.2093)
with Image.open(south) as im:
    f = places.gps_from_exif(im)
check("S ref negates latitude", f[0] < 0 and abs(f[0] + 33.8688) < 1e-3)
check("E ref keeps longitude positive", f[1] > 0)

west = make_gps_jpg(EXTRA / "west.jpg", 40.7128, -74.0060)
with Image.open(west) as im:
    f = places.gps_from_exif(im)
check("W ref negates longitude", f[1] < 0 and abs(f[1] + 74.0060) < 1e-3)

check("city table loaded", len(places.CITIES) > 150)
check("nearest city = 北京", places.nearest_city(39.9042, 116.4074)[0] == "北京")
check("nearest distance ~0", places.nearest_city(39.9042, 116.4074)[1] < 1)
check("place_for Beijing", places.place_for(39.9042, 116.4074) == "北京")
check("place_for Shanghai", places.place_for(31.2304, 121.4737) == "上海")
check("place_for Sydney (S hemisphere)", places.place_for(-33.8688, 151.2093) == "悉尼")
check("place_for New York (W hemisphere)", places.place_for(40.7128, -74.0060) == "纽约")
check("~100km outside a city -> 其他地点", places.place_for(40.80, 116.4074) == places.OTHER_PLACE)
check("mid-Pacific -> 其他地点", places.place_for(0.0, -160.0) == places.OTHER_PLACE)
check("null island rejected by parser",
      (lambda: (make_gps_jpg(EXTRA / "zero.jpg", 0.0, 0.0),
                places.gps_from_exif(Image.open(EXTRA / "zero.jpg")))[1])() is None)


# --- 2. ingest stores GPS + place -----------------------------------------
NOW = time.time()
files = {
    "bj1.jpg": make_gps_jpg(LIB / "bj1.jpg", 39.9042, 116.4074, (10, 20, 30)),
    "bj2.jpg": make_gps_jpg(LIB / "bj2.jpg", 39.9500, 116.3000, (30, 50, 70)),
    "sh.jpg": make_gps_jpg(LIB / "sh.jpg", 31.2304, 121.4737, (70, 90, 110)),
    "sea.jpg": make_gps_jpg(LIB / "sea.jpg", 0.0, -160.0, (110, 130, 150)),
    "syd.jpg": make_gps_jpg(LIB / "syd.jpg", -33.8688, 151.2093, (150, 170, 190)),
    "ny.jpg": make_gps_jpg(LIB / "ny.jpg", 40.7128, -74.0060, (190, 210, 230)),
    "nogps.jpg": make_plain_jpg(LIB / "nogps.jpg"),
}
for i, p in enumerate(files.values()):
    os.utime(p, (NOW - i * 3600, NOW - i * 3600))

from app import ingest
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("scan indexed 7 stills",
      conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 7)

IDS = {k: conn.execute("SELECT id FROM photos WHERE path=?",
                       (str(p),)).fetchone()["id"] for k, p in files.items()}


def row(k):
    return conn.execute(
        "SELECT gps_lat, gps_lon, place, gps_status FROM photos WHERE id=?",
        (IDS[k],)).fetchone()


check("ingest stored 北京 #1", row("bj1.jpg")["place"] == "北京")
check("ingest stored 北京 #2 (nearby coords)", row("bj2.jpg")["place"] == "北京")
check("ingest stored 上海", row("sh.jpg")["place"] == "上海")
check("ingest stored 其他地点 (far from all)", row("sea.jpg")["place"] == places.OTHER_PLACE)
check("ingest stored 悉尼", row("syd.jpg")["place"] == "悉尼")
check("ingest stored 纽约", row("ny.jpg")["place"] == "纽约")
check("no-GPS photo: place NULL", row("nogps.jpg")["place"] is None)
check("no-GPS photo: coords NULL",
      row("nogps.jpg")["gps_lat"] is None and row("nogps.jpg")["gps_lon"] is None)
check("every still marked gps_status='done'",
      all(row(k)["gps_status"] == "done" for k in files))
check("coords persisted for GPS photo",
      abs(row("bj1.jpg")["gps_lat"] - 39.9042) < 1e-3
      and abs(row("bj1.jpg")["gps_lon"] - 116.4074) < 1e-3)

# video: never enters the lane, never gets a place
VID = "a" * 64
db.upsert_photo({"id": VID, "path": str(SCRATCH / "vids" / "clip.mp4"),
                 "kind": "video", "taken_at": NOW, "mtime": NOW, "bytes": 1,
                 "ext": ".mp4", "status": "ok", "ocr_status": "done",
                 "sem_status": "done", "face_status": "done",
                 "gps_status": "done"})
vrow = conn.execute("SELECT place, gps_status FROM photos WHERE id=?",
                    (VID,)).fetchone()
check("video: gps_status done, place NULL",
      vrow["gps_status"] == "done" and vrow["place"] is None)


# --- 3. slow-lane backfill for rows indexed before the feature -------------
pending_gps = make_gps_jpg(EXTRA / "pending_gps.jpg", 31.2304, 121.4737,
                           (5, 100, 200))
before_id = "b" * 64
missing_id = "c" * 64
db.upsert_photo({"id": before_id, "path": str(pending_gps), "kind": "photo",
                 "taken_at": NOW - 9000, "mtime": NOW - 9000, "bytes": 1,
                 "ext": ".jpg", "status": "ok"})
db.upsert_photo({"id": missing_id, "path": str(EXTRA / "gone.jpg"),
                 "kind": "photo", "taken_at": NOW - 9100, "mtime": NOW - 9100,
                 "bytes": 1, "ext": ".jpg", "status": "ok"})
check("pre-feature rows start pending",
      conn.execute("SELECT gps_status FROM photos WHERE id=?",
                   (before_id,)).fetchone()[0] == "pending")
check("backfill sees 2 pending",
      places.summary()["pending"] == 2)

places.start_backfill = _real_places_start
res = places.start_backfill()
check("backfill started", res.get("started") is True and res.get("pending") == 2)
for _ in range(100):
    if not places.progress_snapshot()["running"]:
        break
    time.sleep(0.2)
pr = places.progress_snapshot()
check("backfill finished", pr["running"] is False and pr["error"] is None)
check("backfill total 2", pr["total"] == 2)
check("backfill scanned 1 (successes)", pr["scanned"] == 1)
check("backfill located 1", pr["located"] == 1)
check("backfill failed 1 (unreadable path)", pr["failed"] == 1)
brow = conn.execute("SELECT place, gps_lat, gps_status FROM photos WHERE id=?",
                    (before_id,)).fetchone()
check("backfilled place=上海", brow["place"] == "上海" and brow["gps_status"] == "done")
check("backfilled coords stored", abs(brow["gps_lat"] - 31.2304) < 1e-3)
check("unreadable file marked failed",
      conn.execute("SELECT gps_status FROM photos WHERE id=?",
                   (missing_id,)).fetchone()[0] == "failed")
again = places.start_backfill()
check("idle start is a no-op",
      again.get("started") is False and again.get("reason") == "nothing pending")


# --- 4. re-scan must NOT wipe GPS (content-hash PK) ------------------------
places.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("re-scan kept 北京", row("bj1.jpg")["place"] == "北京")
check("re-scan kept coords", abs(row("bj1.jpg")["gps_lat"] - 39.9042) < 1e-3)
check("re-scan added no duplicate row",
      conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 10)


# --- 5. filtering + grouping ----------------------------------------------
bj = {r["id"] for r in db.query_photos(place="北京")}
check("place filter -> 2 Beijing photos", bj == {IDS["bj1.jpg"], IDS["bj2.jpg"]})
sh = {r["id"] for r in db.query_photos(place="上海")}
check("place filter -> Shanghai (incl. backfilled)",
      sh == {IDS["sh.jpg"], before_id})
none_ids = {r["id"] for r in db.query_photos(place=places.OTHER_PLACE)}
check("其他地点 filter separate", none_ids == {IDS["sea.jpg"]})

groups = {g["place"]: g for g in db.place_groups()}
check("grouping counts", groups["北京"]["count"] == 2
      and groups["上海"]["count"] == 2)
check("grouping cover is a viewable photo", all(
      g["cover"] for g in groups.values()))
check("biggest group first", list(groups)[0] in ("北京", "上海"))


# --- 6. summary / HTTP surface --------------------------------------------
places.start_backfill = _real_places_start
from fastapi.testclient import TestClient
import app.server as server
client = TestClient(app=server.app, raise_server_exceptions=False)

psum = client.get("/api/places").json()
check("GET /api/places with_gps=7", psum["with_gps"] == 7)
check("GET /api/places named=6", psum["named"] == 6)
check("GET /api/places other=1", psum["other"] == 1)
check("GET /api/places pending=0", psum["pending"] == 0)
check("GET /api/places places list", isinstance(psum["places"], list)
      and {p["place"] for p in psum["places"]} >= {"北京", "上海", "悉尼", "纽约"})

hp = client.get("/api/photos", params={"place": "北京", "limit": 1000}).json()
check("GET /api/photos?place=北京 -> 2", len(hp["items"]) == 2)
check("filtered items carry no video",
      VID not in {it["id"] for it in hp["items"]})

d = client.get("/api/photo/" + IDS["bj1.jpg"]).json()
check("detail carries place", d["place"] == "北京")
check("detail carries gps coords", abs(d["gps_lat"] - 39.9042) < 1e-3)

ps = client.post("/api/places/scan").json()
check("POST /api/places/scan idle -> not started",
      ps.get("started") is False and ps.get("reason") == "nothing pending")

print(f"\nplaces_check: {len(OK)} assertions passed")
