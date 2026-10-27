"""Gallery-shell API verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe -u tools/gallery_ui_check.py

Covers the 手机相册式 shell backends added for 二期:
  /api/grouped  (year/month/day covers, ok-status preferred, drill filters)
  /api/photos   (level_key / kind / ext / folder / status / indexed filters)
  /api/albums   (smart buckets + path-derived folder albums)
  /api/search   (filename substring + Chinese time-phrase sources)
  plain timeline paging untouched.
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

SCRATCH = ROOT / ".bench" / "_gallery_test"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (300, 200), (seed * 40 % 256, seed * 11 % 256, 90))
    fmt = "GIF" if path.suffix == ".gif" else (
        "PNG" if path.suffix == ".png" else "JPEG")
    img.save(path, fmt, quality=90)
    y, mo, dd, hh, mi = when
    ts = time.mktime((y, mo, dd, hh, mi, 0, 0, 0, -1))
    os.utime(path, (ts, ts))
    return path


def put_broken(path: Path, when: tuple) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not an image at all" * 5)
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


# ---- library: 2024 (albumA) + 2025 (albumB, newest file broken) + gif at root
put(LIB / "albumA" / "a1.jpg", 1, (2024, 3, 1, 10, 0))
put(LIB / "albumA" / "a2.jpg", 2, (2024, 3, 2, 10, 0))
put(LIB / "albumA" / "a3.png", 3, (2024, 5, 10, 12, 0))     # screenshot kind
put(LIB / "albumB" / "b1.jpg", 4, (2025, 1, 15, 9, 0))
put(LIB / "albumB" / "b2.jpg", 5, (2025, 1, 15, 9, 0))      # same-second tie
put_broken(LIB / "albumB" / "bzz.jpg", (2025, 6, 1, 23, 0))  # newest of 2025
put(LIB / "albumB2" / "trap.jpg", 6, (2025, 2, 2, 8, 0))    # prefix trap dir
put(LIB / "anim.gif", 7, (2025, 3, 3, 8, 0))

ingest.start_scan(str(LIB))
until(lambda: not ingest.PROGRESS["running"], why="scan finish")
db.init_db()
conn = db.get_conn()
rows = {Path(r["path"]).name: dict(r)
        for r in conn.execute("SELECT * FROM photos").fetchall()}
ok(len(rows) == 8, "8 files indexed")
ok(rows["bzz.jpg"]["status"] == "broken", "garbage file registered broken")
ok(rows["a3.png"]["kind"] == "screenshot", "png -> screenshot kind")
ok(rows["anim.gif"]["ext"] == ".gif", "gif ext recorded")

# ---- 1. /api/grouped year: order, counts, cover prefers a viewable row ----
g = client.get("/api/grouped?level=year").json()
keys = [x["key"] for x in g["groups"]]
ok(keys == ["2025", "2024"], f"year groups descending: {keys}")
cnt = {x["key"]: x["count"] for x in g["groups"]}
ok(cnt == {"2025": 5, "2024": 3}, f"year counts {cnt}")
cov25 = next(x for x in g["groups"] if x["key"] == "2025")["cover"]
ok(cov25["status"] == "ok" and Path(cov25["path"]).name != "bzz.jpg",
   "2025 cover skips the broken newest file")
for key in ("id", "path", "taken_at", "kind", "status"):
    ok(key in cov25, f"cover carries {key}")

# ---- 2. month / day drill filters ----
m = client.get("/api/grouped?level=month&year=2024").json()
mk = [(x["key"], x["count"]) for x in m["groups"]]
ok(mk == [("2024-05", 1), ("2024-03", 2)], f"months of 2024: {mk}")
d = client.get("/api/grouped?level=day&year=2024&month=3").json()
ok([x["key"] for x in d["groups"]] == ["2024-03-02", "2024-03-01"],
   "days of 2024-03")
ok(client.get("/api/grouped?level=hour").status_code == 400, "bad level -> 400")

# ---- 3. same-second tie: cover must be the ok row, not the other row ----
tie = client.get("/api/grouped?level=day&year=2025&month=1").json()["groups"][0]
ok(tie["count"] == 2 and tie["cover"]["status"] == "ok",
   "same-second tie resolved to a viewable cover")

# ---- 4. /api/photos level_key + filters ----
lv = client.get("/api/photos?level_key=2024-03-01").json()
ok([Path(i["path"]).name for i in lv["items"]] == ["a1.jpg"], "level_key day")
ok(lv["has_more"] is False and lv["next_cursor"] is None,
   "filtered listing is unpaged")
kd = client.get("/api/photos?kind=screenshot").json()
ok([Path(i["path"]).name for i in kd["items"]] == ["a3.png"], "kind filter")
ex = client.get("/api/photos?ext=.gif").json()
ok([Path(i["path"]).name for i in ex["items"]] == ["anim.gif"], "ext filter")
st = client.get("/api/photos?status=broken").json()
ok([Path(i["path"]).name for i in st["items"]] == ["bzz.jpg"], "status filter")
fo = client.get(f"/api/photos?folder={LIB / 'albumB'}\\").json()
names = sorted(Path(i["path"]).name for i in fo["items"])
ok(names == ["b1.jpg", "b2.jpg", "bzz.jpg"],
   f"folder prefix excludes albumB2 sibling: {names}")
idx = client.get(f"/api/photos?order=indexed&indexed_from={time.time() - 120}").json()
ok(len(idx["items"]) == 8, "indexed order returns fresh rows")
idx2 = client.get(f"/api/photos?indexed_from={time.time() + 3600}").json()
ok(idx2["items"] == [], "future indexed_from -> empty")
ok(client.get("/api/photos?level_key=bad").status_code == 400, "bad level_key -> 400")

# ---- 5. paging path untouched ----
pg = client.get("/api/photos?limit=3").json()
ok(len(pg["items"]) == 3 and pg["has_more"] and pg["next_cursor"],
   "timeline paging still cursors")

# ---- 6. /api/albums ----
al = client.get("/api/albums").json()
smart = {a["key"]: a for a in al["smart"]}
ok(smart["camera"]["count"] == 5, "smart camera = jpgs")
ok(smart["screenshot"]["count"] == 1, "smart screenshot = 1")
ok(smart["gif"]["count"] == 1, "smart gif = 1")
ok(smart["recent"]["count"] == 7, "smart recent = all non-broken (fresh scan)")
ok(smart["missing"]["count"] == 0, "smart missing = 0")
ok(smart["camera"]["cover"] != rows["bzz.jpg"]["id"], "album cover avoids broken")
fols = {f["folder"]: f for f in al["folders"]}
ok(all(str(LIB) in k for k in fols), f"folder albums derive from root: {list(fols)}")
by = {Path(k).name: v["count"] for k, v in fols.items()}
ok(by.get("albumA") == 3 and by.get("albumB") == 2 and by.get("library") == 1,
   f"per-folder counts (broken excluded): {by}")

# ---- 7. search: filename + Chinese time phrases ----
def sources_of(q):
    j = client.get(f"/api/search?q={q}&engine=text").json()
    return {Path(i["path"]).name: i.get("source") for i in j["items"]}
s1 = sources_of("a2.jpg")
ok(s1.get("a2.jpg", "").find("file") >= 0, f"filename hit tagged file: {s1}")
s2 = sources_of("2024年3月2日")
ok(list(s2) == ["a2.jpg"] and s2["a2.jpg"].find("time") >= 0,
   f"date phrase -> time source: {s2}")
s3 = sources_of("2025年1月")
ok(sorted(s3) == ["b1.jpg", "b2.jpg"], f"year-month phrase: {sorted(s3)}")
# bare 月日 phrases resolve to THIS year — give it a target, rescan
put(LIB / "albumA" / "t2026.jpg", 9, (time.localtime().tm_year, 3, 1, 10, 0))
ingest.start_scan(str(LIB))
until(lambda: not ingest.PROGRESS["running"], why="incremental rescan")
s4 = sources_of("3月1日")
ok(list(s4) == ["t2026.jpg"], f"bare month-day phrase hits this year: {s4}")
s5 = sources_of("13月")
ok(s5 == {}, "nonsense month -> no time hits")

print("\ngallery ui check: ALL PASSED")
