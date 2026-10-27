r"""User collections (图集/收藏集) assertions on a scratch library + DB.

Covers: CRUD, member dedup, membership queries, stats/cover, album photo
filter, delete-leaves-photos, missing-file exclusion, re-scan keeps members,
API surface.
Run:  .venv\Scripts\python.exe tools\albums_check.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"F:\Code\photo-album")

ROOT = Path(r"F:\Code\photo-album")
SCRATCH = ROOT / ".bench" / "_albums_check"
LIB = SCRATCH / "lib"
shutil.rmtree(SCRATCH, ignore_errors=True)
LIB.mkdir(parents=True)

import app.config as cfg
cfg.DATA_DIR = SCRATCH / ".data"
cfg.DB_PATH = cfg.DATA_DIR / "t.db"
cfg.THUMB_DIR = cfg.DATA_DIR / "thumbs"
cfg.SHARE_DIR = cfg.DATA_DIR / "share"

from PIL import Image, ImageDraw

from app import db, ocr
ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}
import app.dups as dups
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.semantic as semantic
semantic.model_present = lambda: False
semantic.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.caption as caption
caption.start = lambda *a, **k: {"started": False, "reason": "test"}

db.init_db()
conn = db.get_conn()

OK = []
def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    OK.append(name)


def put(name: str, seed: int, taken: float) -> Path:
    p = LIB / name
    img = Image.new("RGB", (300, 200), (seed * 37 % 256, seed * 61 % 256, 90))
    ImageDraw.Draw(img).rectangle([20, 20, 280, 180], outline=(250, 250, 250))
    img.save(p, quality=90)
    import os
    os.utime(p, (taken, taken))
    return p


NOW = time.time()
f1 = put("p1.jpg", 1, NOW - 5 * 86400)   # oldest
f2 = put("p2.jpg", 2, NOW - 3 * 86400)
f3 = put("p3.jpg", 3, NOW - 1 * 86400)   # newest -> cover candidate
f4 = put("p4.jpg", 4, NOW - 2 * 86400)

from app import ingest, albums
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("scan indexed 4", conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 4)

ID = {p.name: conn.execute("SELECT id FROM photos WHERE path=?",
                           (str(p),)).fetchone()["id"]
      for p in (f1, f2, f3, f4)}

# --- kernel CRUD -----------------------------------------------------------
check("create ok", albums.create("旅行 2026")["ok"])
check("create dup name rejected", not albums.create("旅行 2026")["ok"])
check("create blank rejected", not albums.create("   ")["ok"])
aid = albums.create("家庭")["id"]
check("add 2 known + 1 unknown -> added 2",
      (lambda r: r["ok"] and r["added"] == 2 and r["total"] == 2)(
          albums.add(aid, [ID["p1.jpg"], ID["p2.jpg"], "f" * 64])))
check("re-add idempotent",
      albums.add(aid, [ID["p1.jpg"]])["added"] == 0)
st = [a for a in albums.albums_with_stats() if a["id"] == aid][0]
check("stats count 2", st["count"] == 2)
check("cover is newest ok member", st["cover"] == ID["p2.jpg"])
check("albums_for p1", [a["id"] for a in albums.albums_for(ID["p1.jpg"])] == [aid])
check("albums_for p3 empty", albums.albums_for(ID["p3.jpg"]) == [])
check("rename ok", albums.rename(aid, "家人")["name"] == "家人")
check("rename clash rejected", not albums.rename(aid, "旅行 2026")["ok"])
check("rename unknown album", not albums.rename("zzz", "x")["ok"])
check("remove member", albums.remove(aid, [ID["p2.jpg"]])["ok"])
check("count after remove", albums.get_album(aid)["count"] == 1)
check("get unknown album None", albums.get_album("zzz") is None)

# --- missing files excluded from counts ------------------------------------
f1.unlink()
db.sweep_missing(str(LIB))
check("missing excluded from count", albums.get_album(aid)["count"] == 0)
st = [a for a in albums.albums_with_stats() if a["id"] == aid][0]
check("missing excluded from cover", st["cover"] is None)

# re-scan reconciles the restored file: membership survives by content id
import os
put("p1.jpg", 1, NOW - 5 * 86400)
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("re-scan same id", ID["p1.jpg"] == conn.execute(
    "SELECT id FROM photos WHERE path=?", (str(LIB / 'p1.jpg'),)).fetchone()["id"])
check("membership survives rescan", albums.get_album(aid)["count"] == 1)

# --- delete album leaves photos ---------------------------------------------
albums.delete(aid)
check("album gone", albums.get_album(aid) is None)
check("members row gone", conn.execute(
    "SELECT COUNT(*) c FROM album_photos WHERE album_id=?", (aid,)).fetchone()["c"] == 0)
check("photos untouched", conn.execute(
    "SELECT COUNT(*) c FROM photos WHERE status='ok'").fetchone()["c"] == 4)
check("delete unknown", not albums.delete(aid)["ok"])

# --- API surface -------------------------------------------------------------
from fastapi.testclient import TestClient
import app.server as server
c = TestClient(server.app)

r = c.post("/api/albums", json={"name": "旅行"})
check("POST /api/albums", r.status_code == 200 and r.json()["ok"])
nid = r.json()["id"]
check("POST dup -> 400", c.post("/api/albums", json={"name": "旅行"}).status_code == 400)
check("POST blank -> 400", c.post("/api/albums", json={"name": " "}).status_code == 400)
r = c.post("/api/albums/add", json={"id": nid, "ids": [ID["p3.jpg"], ID["p4.jpg"]]})
check("POST add", r.status_code == 200 and r.json()["added"] == 2)
check("POST add bad id -> 400",
      c.post("/api/albums/add", json={"id": nid, "ids": ["x"]}).status_code == 400)
check("POST add unknown album -> 404",
      c.post("/api/albums/add", json={"id": "zzz", "ids": [ID["p3.jpg"]]}).status_code == 404)
data = c.get("/api/albums").json()
check("GET /api/albums has user key", "user" in data)
u = [a for a in data["user"] if a["id"] == nid][0]
check("user card count/cover", u["count"] == 2 and u["cover"] == ID["p3.jpg"])
check("GET /api/albums/{id}", c.get(f"/api/albums/{nid}").json()["name"] == "旅行")
check("GET /api/albums/{id} 404", c.get("/api/albums/zzz").status_code == 404)
check("POST rename route beats {id}",
      c.post("/api/albums/rename", json={"id": nid, "name": "海边"}).json()["name"] == "海边")
check("POST remove",
      c.post("/api/albums/remove", json={"id": nid, "ids": [ID["p4.jpg"]]}).json()["ok"])
items = c.get(f"/api/photos?album={nid}").json()["items"]
check("GET /api/photos?album=", [i["id"] for i in items] == [ID["p3.jpg"]])
check("album filter composes with kind",
      c.get(f"/api/photos?album={nid}&kind=photo").json()["items"])
check("unknown album filter -> empty",
      c.get("/api/photos?album=zzz").json()["items"] == [])
check("GET /api/albums_of", [a["id"] for a in
      c.get(f"/api/albums_of/{ID['p3.jpg']}").json()["albums"]] == [nid])
check("albums_of bad id -> 400", c.get("/api/albums_of/x").status_code == 400)
check("DELETE /api/albums/{id}", c.delete(f"/api/albums/{nid}").json()["ok"])
check("DELETE unknown -> 404", c.delete("/api/albums/zzz").status_code == 404)

print(f"albums_check: {len(OK)} assertions passed")
