"""Semantic search verification — runs ONLY in a throwaway scratch dir.

Usage:  .venv/Scripts/python.exe -u tools/semantic_check.py

Exercises the Chinese-CLIP lane end to end (real model, no torch):
  scan -> auto backfill -> semantic top-1 ranking for Chinese queries;
  text engine stays OCR/notes-only; engine=all merges;
  /api/similar returns the visual variant first;
  watcher ingests a new file AND the slow lane embeds it without a rescan.
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

SCRATCH = ROOT / ".bench" / "_semantic_test"
LIB = SCRATCH / "library"
DATA = SCRATCH / "data"
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
LIB.mkdir(parents=True)
DATA.mkdir(parents=True)
config.DATA_DIR = DATA
config.DB_PATH = DATA / "test.db"
config.THUMB_DIR = DATA / "thumbs"
config.SEM_MODEL_DIR = ROOT / ".data" / "models" / "chinese_clip_onnx"

from PIL import Image, ImageDraw  # noqa: E402

from app import db, ingest, ocr, semantic, watch  # noqa: E402

assert semantic.model_present(), "run tools/semantic_spike.py download steps first"

ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}

from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

client = TestClient(server.app)


def scene(name, bg, shape, color, box=(52, 52, 172, 172)):
    img = Image.new("RGB", (224, 224), bg)
    d = ImageDraw.Draw(img)
    if shape == "circle":
        d.ellipse(box, fill=color)
    elif shape == "triangle":
        d.polygon([(112, 40), (40, 184), (184, 184)], fill=color)
    else:
        d.rectangle(box, fill=color)
    p = LIB / name
    img.save(p, "PNG")
    return p


def until(pred, timeout=120.0, why=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.5)
    raise AssertionError(f"timeout waiting: {why}")


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS {msg}")


def sem_done():
    conn = db.get_conn()
    return conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE sem_status='done'").fetchone()["c"]


# ---- build library: 3 query targets + 1 visual variant + 3 distractors ----
scene("red_square.png", "white", "rect", "red")
scene("blue_circle.png", "black", "circle", "blue")
scene("green_square.png", "white", "rect", "green")
scene("red_square_small.png", "white", "rect", "red", box=(80, 80, 144, 144))
scene("noisy1.png", "gray", "circle", "purple")
scene("noisy2.png", "yellow", "rect", "cyan")
scene("noisy3.png", "white", "triangle", "black")

ingest.start_scan(str(LIB))
until(lambda: not ingest.PROGRESS["running"], why="scan finish")
until(lambda: sem_done() == 7 and not semantic.PROGRESS["running"],
      why="semantic backfill of 7 images")
st = db.stats()
ok(st["embedded"] == 7, f"all 7 images embedded (rate ok)")


def id_of(name):
    r = db.get_conn().execute(
        "SELECT id FROM photos WHERE path=?", (str(LIB / name),)).fetchone()
    return r["id"] if r else None


# ---- 1. semantic text ranking ----
for q, want in [("红色的正方形", "red_square.png"),
                ("蓝色的圆形", "blue_circle.png"),
                ("绿色的方块", "green_square.png")]:
    s = client.get("/api/search", params={"q": q, "engine": "semantic"}).json()
    top = s["items"][0]
    ok(top["id"] == id_of(want) and top["score"] > 0.35,
       f"semantic top-1 for {q!r} = {want} ({top['score']:.3f})")

# ---- 2. text engine must NOT do semantic matching ----
s = client.get("/api/search", params={"q": "红色的正方形", "engine": "text"}).json()
ok(s["count"] == 0, "text engine ignores pure semantic query")
s = client.get("/api/search", params={"q": "红色的正方形", "engine": "all"}).json()
ok(s["count"] >= 3 and any("semantic" in i["source"] for i in s["items"]),
   "all engine merges semantic hits in")

# ---- 3. image -> image: the shifted variant must rank first ----
rid = id_of("red_square.png")
sim = client.get(f"/api/similar/{rid}?limit=3").json()
ok(sim["items"][0]["id"] == id_of("red_square_small.png"),
   f"similar top-1 is the visual variant (score {sim['items'][0]['score']:.3f})")
ok(rid not in [i["id"] for i in sim["items"]], "similar excludes the query image")

# ---- 4. watcher path embeds new files WITHOUT a manual rescan ----
watch.QUIET_SEC = 0.5
scene("yellow_triangle.png", "white", "triangle", "orange")
until(lambda: sem_done() == 8, why="watcher + slow lane on new file")
s = client.get("/api/search", params={"q": "黄色三角形", "engine": "semantic"}).json()
ok(s["items"][0]["id"] == id_of("yellow_triangle.png"),
   "newly watched file is queryable semantically without rescan")

# ---- 5. status plumbing ----
snap = client.get("/api/status").json()["semantic"]
ok(snap["model_present"] and snap["pending"] == 0,
   "status.semantic reports model + zero pending")

print("\nsemantic check: ALL PASSED")
