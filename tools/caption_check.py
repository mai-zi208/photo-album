"""Cloud-caption lane verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe -u tools/caption_check.py

Spins up a fake OpenAI-compatible /chat/completions endpoint on 127.0.0.1
(no real provider is ever contacted) and exercises the whole lane:
  config masking + keep-old-key patch; lane-off rejection; photo generate;
  screenshot policy (default reject, per-item force, global allow);
  caption search attribution (source contains 'caption'); watcher-ingested
  new photo -> auto pending -> auto done via kick_slow_lanes; /api/status
  and stats expose the lane.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- isolate BEFORE importing db/ingest/watch/caption/server ----
import app.config as config  # noqa: E402

SCRATCH = ROOT / ".bench" / "_caption_test"
LIB = SCRATCH / "library"
DATA = SCRATCH / "data"
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
LIB.mkdir(parents=True)
DATA.mkdir(parents=True)
config.DATA_DIR = DATA
config.DB_PATH = DATA / "test.db"
config.THUMB_DIR = DATA / "thumbs"
config.SEM_MODEL_DIR = DATA / "models" / "chinese_clip_onnx"  # absent -> no-op

from PIL import Image  # noqa: E402

from app import caption, db, ingest, ocr, watch  # noqa: E402

ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}
import app.dups as dups  # noqa: E402
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}

watch.QUIET_SEC = 0.5

# ---- fake OpenAI-compatible endpoint ----
REQUESTS = []          # parsed bodies the mock received
MOCK_REPLY = "一只橘猫趴在窗台的阳光下打盹"


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        REQUESTS.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "body": body})
        out = json.dumps({"choices": [{"message": {"content": MOCK_REPLY}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


mock = ThreadingHTTPServer(("127.0.0.1", 8798), MockHandler)
threading.Thread(target=mock.serve_forever, daemon=True, name="mock-vlm").start()
MOCK_URL = "http://127.0.0.1:8798/v1"

from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402 (after patches)

client = TestClient(server.app)


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


def status_of(file_id):
    r = db.get_conn().execute(
        "SELECT caption, caption_status FROM photos WHERE id=?", (file_id,)).fetchone()
    return dict(r)


# ---- build a small library: 3 photos (jpg) + 1 screenshot (png) ----
put(LIB / "p0.jpg", 2)
put(LIB / "p1.jpg", 3)
put(LIB / "p2.jpg", 4)
put(LIB / "s3.png", 5, "PNG")
ingest.start_scan(str(LIB))
until(lambda: not ingest.PROGRESS["running"], why="scan finish")
db.init_db()
rows = db.get_conn().execute(
    "SELECT id, kind FROM photos ORDER BY id").fetchall()
ids = [r["id"] for r in rows]
kinds = {r["id"]: r["kind"] for r in rows}
ok(len(ids) == 4, f"4 photos indexed")
shot_id = next(i for i in ids if kinds[i] == "screenshot")
photo_ids = [i for i in ids if kinds[i] == "photo"]

# ---- 1. lane off: generate rejects, no network ----
r = client.post(f"/api/caption/{photo_ids[0]}")
ok(r.status_code == 200 and r.json()["ok"] is False and "未启用" in r.json()["reason"],
   f"lane off -> rejected: {r.json()['reason']}")
ok(not REQUESTS, "no request reached mock while disabled")
ok(status_of(photo_ids[0])["caption_status"] == "off", "rows stay 'off' untouched")

# ---- 2. config save + masking + keep-old-key ----
r = client.post("/api/caption/config", json={
    "enabled": True, "base_url": MOCK_URL, "api_key": "sk-test-abcdef7890",
    "model": "mock-vlm", "allow_screenshots": False})
cfg = r.json()
ok(cfg["api_key"] == "********7890" and cfg["has_key"] is True,
   f"GET-side key masked: {cfg['api_key']}")
full = json.dumps(cfg)
ok("abcdef" not in full, "no config response leaks the real key")
r = client.post("/api/caption/config", json={"model": "mock-vlm-2"})  # no key field
ok(caption.load_config()["api_key"] == "sk-test-abcdef7890",
   "patch without api_key keeps stored key")
r = client.post("/api/caption/config", json={"api_key": ""})
ok(caption.load_config()["api_key"] == "sk-test-abcdef7890",
   "empty api_key patch also keeps stored key")
c2 = client.get("/api/caption/config").json()
ok(c2["enabled"] is True and c2["base_url"] == MOCK_URL, "config roundtrip via API")

# ---- 3. on-demand generate for a photo ----
ocr.save_result(photo_ids[0], "季度预算 meeting", True)
r = client.post(f"/api/caption/{photo_ids[0]}")
j = r.json()
ok(j.get("ok") is True and j.get("caption") == MOCK_REPLY,
   f"photo generate ok: {j.get('caption')}")
req = REQUESTS[-1]
ok(req["path"].endswith("/chat/completions")
   and req["auth"] == "Bearer sk-test-abcdef7890", "mock hit with Bearer auth")
content = req["body"]["messages"][0]["content"]
parts = {p["type"] for p in content}
img_part = next(p for p in content if p["type"] == "image_url")
ok("image_url" in parts and img_part["image_url"]["url"].startswith(
    "data:image/webp;base64,"), "only 512px webp data URL sent")
st = status_of(photo_ids[0])
ok(st["caption_status"] == "done" and st["caption"], "row: status done + caption stored")
d = client.get(f"/api/photo/{photo_ids[0]}").json()
ok(d["caption"] == st["caption"], "detail endpoint carries caption")

# ---- 4. screenshot policy ----
r = client.post(f"/api/caption/{shot_id}")
ok(r.json()["ok"] is False and "截图" in r.json()["reason"],
   f"screenshot rejected by default: {r.json()['reason']}")
n_before = len(REQUESTS)
r = client.post(f"/api/caption/{shot_id}", params={"force": "true"})
ok(r.json()["ok"] is True and len(REQUESTS) == n_before + 1,
   "force=true sends this one screenshot anyway")
ok(status_of(shot_id)["caption_status"] == "done", "forced screenshot caption stored")

# ---- 5. caption is searchable, source attributed ----
s = client.get("/api/search", params={"q": "橘猫", "engine": "text"}).json()
hit0 = [i for i in s["items"] if i["id"] == photo_ids[0]]
ok(hit0 and "caption" in hit0[0]["source"] and hit0[0]["caption"] == MOCK_REPLY,
   f"caption searchable, source={hit0[0]['source']}")
# 'meeting' is only in OCR text -> source stays ocr (no caption)
s = client.get("/api/search", params={"q": "meeting", "engine": "text"}).json()
ok(any(i["id"] == photo_ids[0] and i["source"] == "ocr" for i in s["items"]),
   "ocr-only hit not mislabeled with caption")
client.post("/api/photo/note", json={"id": photo_ids[0], "note": "窗台的猫"})
s = client.get("/api/search", params={"q": "猫", "engine": "text"}).json()
both = [i for i in s["items"] if i["id"] == photo_ids[0]]
ok(both and "note" in both[0]["source"] and "caption" in both[0]["source"],
   f"note+caption both attributed: source={both[0]['source']}")

# ---- 6. new photo via watcher -> auto pending -> auto done (no manual start) ----
put(LIB / "p9.jpg", 7)
row = until(lambda: db.get_conn().execute(
    "SELECT id FROM photos WHERE path=?", (str(LIB / "p9.jpg"),)).fetchone(),
    why="watcher ingests p9")
new_id = row["id"]
until(lambda: status_of(new_id)["caption_status"] in ("pending", "running", "done"),
      why="new photo marked pending")
until(lambda: status_of(new_id)["caption_status"] == "done",
      why="queue generated caption")
ok(status_of(new_id)["caption"], "auto caption stored for watcher-ingested photo")

# ---- 7. status/stats surface the lane ----
st = client.get("/api/status").json()
ok("caption" in st and st["caption"]["configured"] is True,
   "/api/status includes caption lane (configured=true)")
ok(st["db"]["captioned"] >= 3, f"stats.captioned >= 3 (got {st['db']['captioned']})")
p = client.get("/api/photos", params={"limit": 100}).json()
ok(any(i["id"] == photo_ids[0] and i["caption"] for i in p["items"]),
   "/api/photos items carry caption for badge")

# ---- 8. bad / unknown ids ----
ok(client.post("/api/caption/short").status_code == 400, "bad id -> 400")
ok(client.post("/api/caption/" + "0" * 64).status_code == 404, "unknown id -> 404")

mock.shutdown()
print("\ncaption check: ALL PASSED")
