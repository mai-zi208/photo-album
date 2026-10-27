"""Share-pack verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe -u tools/share_check.py

Builds a small library, packs a mix of photos/screenshots into a
self-contained HTML, then asserts:
  correct count + embedded base64 thumbnails;
  PRIVACY: no on-disk paths, no OCR text, no notes, no captions, no filenames
  leak into the pack (only date + kind labels are allowed);
  missing/broken rows are skipped, not packed;
  guards: empty selection, >MAX_ITEMS, garbage ids;
  traversal on /share/<name> is refused; the pack serves as text/html.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- isolate BEFORE importing db/ingest/watch/share/server ----
import app.config as config  # noqa: E402

SCRATCH = ROOT / ".bench" / "_share_test"
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

from app import db, ingest, ocr, share, watch  # noqa: E402
import app.caption as caption  # noqa: E402

ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}
import app.dups as dups  # noqa: E402
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
watch.QUIET_SEC = 0.5

from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402 (after patches)

client = TestClient(server.app)

# distinctive canary strings we plant in every sensitive field; the pack
# must NOT contain any of them
SECRET_OCR = "机密合同金额八百六十四万"
SECRET_NOTE = "私人备注老王电话13800000000"
SECRET_CAPTION = "云端描述泄露测试XYZ"


def put(path: Path, seed: int, fmt="JPEG") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (300, 200), (seed * 40 % 256, seed * 11 % 256, 90))
    img.save(path, fmt, quality=90)
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


# ---- build library: 4 photos + 2 screenshots ----
for i in range(4):
    put(LIB / f"p{i}.jpg", i + 1)
for i in range(2):
    put(LIB / f"s{i}.png", i + 9, "PNG")
ingest.start_scan(str(LIB))
until(lambda: not ingest.PROGRESS["running"], why="scan finish")
db.init_db()
rows = db.get_conn().execute(
    "SELECT id, kind FROM photos ORDER BY taken_at DESC").fetchall()
ids = [r["id"] for r in rows]
kinds = {r["id"]: r["kind"] for r in rows}
ok(len(ids) == 6, "6 photos indexed")

# ---- plant sensitive fields on the first photo ----
victim = ids[0]
ocr.save_result(victim, SECRET_OCR, True)
db.set_note(victim, SECRET_NOTE)
db.get_conn().execute(
    "UPDATE photos SET caption=?, caption_status='done' WHERE id=?",
    (SECRET_CAPTION, victim))
db.get_conn().commit()

# ---- 1. build a pack from all 6 ----
r = client.post("/api/share", json={"ids": ids, "title": "旅行照片"})
j = r.json()
ok(j.get("ok") is True and j["count"] == 6 and j["skipped"] == 0,
   f"pack built: {j.get('count')} imgs, {j.get('bytes')} bytes")
ok(j["url"].startswith("/share/") and j["url"].endswith(".html"),
   f"served url {j['url']}")

# ---- 2. fetch the pack and inspect its HTML ----
res = client.get(j["url"])
ok(res.status_code == 200 and "text/html" in res.headers["content-type"],
   "GET /share/<name> -> 200 text/html")
html = res.text
ok(html.count("data:image/webp;base64,") == 6, "6 thumbnails embedded as base64")
# decode the JSON payload the page carries
m = re.search(r'<script id="data" type="application/json">(.*?)</script>',
              html, re.S)
ok(bool(m), "page carries a JSON data island")
payload = json.loads(m.group(1).replace("<\\/", "</"))
ok(payload["title"] == "旅行照片" and len(payload["items"]) == 6,
   "payload title + item count")
kinds_in_pack = {it["kind"] for it in payload["items"]}
ok("照片" in kinds_in_pack and "截图" in kinds_in_pack,
   f"kind labels present: {kinds_in_pack}")

# ---- 3. PRIVACY: nothing sensitive leaked ----
for needle, label in [
    (SECRET_OCR, "OCR text"), (SECRET_NOTE, "note"), (SECRET_CAPTION, "caption"),
    (str(LIB), "library path"), (str(DATA), "data path"),
    ("p0.jpg", "filename"), ("s0.png", "screenshot filename"),
    ("F:\\Code", "windows path"),
]:
    ok(needle not in html, f"pack does NOT contain {label}")
# each item exposes only id/src/ts/kind — no path/ocr/note/caption keys
allowed = {"id", "src", "ts", "kind"}
extra = set().union(*[set(it.keys()) for it in payload["items"]]) - allowed
ok(not extra, f"items expose only {sorted(allowed)} (extra={sorted(extra)})")
# base64 decodes back to the real thumbnail bytes (lossless embed)
one = payload["items"][0]["src"].split(",", 1)[1]
raw = base64.b64decode(one)
ok(raw[:4] == b"RIFF" and raw[8:12] == b"WEBP", "embedded src is a valid WEBP")

# ---- 4. missing/broken rows are skipped, not packed ----
gone = ids[-1]
row = db.get_conn().execute(
    "SELECT path FROM photos WHERE id=?", (gone,)).fetchone()
Path(row["path"]).unlink()
until(lambda: db.get_conn().execute(
    "SELECT status FROM photos WHERE id=?", (gone,)).fetchone()["status"] == "missing",
    why="watcher marks deleted file missing")
j2 = client.post("/api/share", json={"ids": ids, "title": "含丢失"}).json()
ok(j2["ok"] is True and j2["count"] == 5 and j2["skipped"] == 1,
   f"missing row skipped: count={j2['count']} skipped={j2['skipped']}")

# ---- 5. guards ----
ok(client.post("/api/share", json={"ids": []}).json()["ok"] is False,
   "empty selection rejected")
ok(client.post("/api/share", json={"ids": ["0" * 64] * (share.MAX_ITEMS + 1)}
               ).json()["ok"] is False, "over-cap selection rejected")
g = client.post("/api/share", json={"ids": ["short", "not-a-hash", "x" * 64]}).json()
ok(g["ok"] is False, "garbage-only ids rejected")

# ---- 6. path traversal on the serving route is refused ----
evil = client.get("/share/..%2F..%2Ftest.db")
ok(evil.status_code == 404, f"traversal refused ({evil.status_code})")
evil2 = client.get("/share/" + "..\\..\\secret")
ok(evil2.status_code == 404, "backslash traversal refused")

# ---- 7. two packs are distinct files (no overwrite) ----
a = client.post("/api/share", json={"ids": ids[:2], "title": "A"}).json()
b = client.post("/api/share", json={"ids": ids[2:4], "title": "B"}).json()
ok(a["name"] != b["name"], "packs get unique filenames")

print("\nshare check: ALL PASSED")
