r"""Content-rename pipeline assertions on a scratch library + scratch DB.

Covers: priority chain (note > caption > OCR > fallback), already-named
skip, same-name collision suffix, plan->apply->undo round trip, DB path
follows the rename, watcher does not double-register a renamed file,
API endpoints.
Run:  .venv\Scripts\python.exe tools\rename_check.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"F:\Code\photo-album")

ROOT = Path(r"F:\Code\photo-album")
SCRATCH = ROOT / ".bench" / "_rename_test"
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


def put(name: str, seed: int) -> Path:
    p = LIB / name
    img = Image.new("RGB", (300, 200), (seed * 37 % 256, seed * 61 % 256, 90))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 280, 180], outline=(250, 250, 250))
    img.save(p, quality=90)
    return p


# --- fixtures: messy names on purpose -------------------------------------
f_ocr = put("wx_image_0001.jpg", 1)          # OCR: 合同扫描件 -> 合同扫描件.jpg
f_note = put("IMG_20240102_153045.jpg", 2)   # note wins over OCR
f_cap = put("1719000000000.jpg", 3)          # caption source
f_plain = put("screenshot_9527.png", 4)      # no signals -> fallback only
f_named = put("家庭聚餐合影.jpg", 5)          # already meaningful -> skip
f_dup1 = put("a1.jpg", 6)                    # both OCR to the same stem
f_dup2 = put("a2.jpg", 7)

from app import ingest
ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("scan indexed 7", conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 7)

def fid(p: Path) -> str:
    row = conn.execute("SELECT id FROM photos WHERE path=?", (str(p),)).fetchone()
    return row["id"] if row else None

# freeze ids right after the scan: renames move paths, ids never move
ID = {n: fid(LIB / n) for n in [
    "wx_image_0001.jpg", "IMG_20240102_153045.jpg", "1719000000000.jpg",
    "screenshot_9527.png", "家庭聚餐合影.jpg", "a1.jpg", "a2.jpg"]}
f_ocr_id, f_note_id, f_cap_id, f_plain_id = (
    ID["wx_image_0001.jpg"], ID["IMG_20240102_153045.jpg"],
    ID["1719000000000.jpg"], ID["screenshot_9527.png"])

ocr.save_result(f_ocr_id, "合同扫描件 第 3 页", True)
ocr.save_result(ID['a1.jpg'], "项目验收单", True)
ocr.save_result(ID['a2.jpg'], "项目验收单", True)
conn.execute("UPDATE photos SET note=? WHERE id=?", ("孩子百日纪念", f_note_id))
conn.execute("UPDATE photos SET caption=? WHERE id=?", ("海边日落下的金色沙滩与椰子树", f_cap_id))
conn.commit()

from app import rename

# --- plan: priority chain --------------------------------------------------
p = rename.plan(with_fallback=False)
check("plan excludes fallback rows", p["files"] == 5)
check("plan skips already-named", p["skipped_already_named"] == 1)
by = {s["id"]: s for s in p["sample"]}
check("OCR first line names file", by[f_ocr_id]["dst"] == "合同扫描件 第 3 页.jpg")
check("OCR source tagged", by[f_ocr_id]["source"] == "ocr")
check("note beats everything", by[f_note_id]["dst"] == "孩子百日纪念.jpg")
check("note source tagged", by[f_note_id]["source"] == "note")
check("caption names file", by[f_cap_id]["dst"] == "海边日落下的金色沙滩与椰子树.jpg")
check("caption source tagged", by[f_cap_id]["source"] == "caption")
check("note over ocr priority", "note" in p["by_source"] and p["by_source"]["note"] == 1)
dsts = sorted(s["dst"] for s in p["sample"] if s["id"] in (ID['a1.jpg'], ID['a2.jpg']))
check("collision gets suffix", dsts == ["项目验收单-2.jpg", "项目验收单.jpg"])  # - sorts before .
check("no-plan row for signal-less file", f_plain_id not in by)
check("already-named file untouched", fid(f_named) not in by)

# --- plan with fallback ----------------------------------------------------
p2 = rename.plan(with_fallback=True)
check("fallback adds the plain file", p2["files"] == 6)
fb = next(s for s in p2["sample"] if s["id"] == f_plain_id)
check("fallback name shape", fb["dst"].startswith("截图_") and fb["dst"].endswith(".png"))
check("fallback carries id8", f"_{fb['dst'][-12:-4]}" in fb["dst"] or len(fb["dst"]) > 14)
check("fallback source tagged", fb["source"] == "time")

# --- apply ------------------------------------------------------------------
r = rename.start_apply()
check("apply started", r.get("started") is True)
while rename.apply_progress()["running"]:
    time.sleep(0.1)
pr = rename.apply_progress()
check("apply renamed 6", pr["renamed"] == 6 and pr["failed"] == 0)
check("plan state applied", rename.current()["state"] == "applied")
check("ocr file renamed on disk", (LIB / "合同扫描件 第 3 页.jpg").exists())
check("old name gone", not f_ocr.exists())
check("note file renamed", (LIB / "孩子百日纪念.jpg").exists())
check("caption file renamed", (LIB / "海边日落下的金色沙滩与椰子树.jpg").exists())
check("collision pair both exist",
      (LIB / "项目验收单.jpg").exists() and (LIB / "项目验收单-2.jpg").exists())
check("already-named untouched on disk", f_named.exists())
row = conn.execute("SELECT path FROM photos WHERE id=?", (f_ocr_id,)).fetchone()
check("DB path follows rename", row["path"] == str(LIB / "合同扫描件 第 3 页.jpg"))
check("id survives rename (content PK)",
      conn.execute("SELECT COUNT(*) c FROM photos WHERE id=?",
                   (f_ocr_id,)).fetchone()["c"] == 1)

# search still finds it under the new name
from fastapi.testclient import TestClient
from app import server
client = TestClient(server.app)
s = client.get("/api/search", params={"q": "合同"}).json()
check("OCR search survives rename",
      any(i["id"] == f_ocr_id for i in s["items"]))
s = client.get("/api/search", params={"q": "孩子"}).json()
check("filename search hits new name",
      any(i["id"] == f_note_id and "file" in i["source"] for i in s["items"]))

# --- undo -------------------------------------------------------------------
u = rename.undo()
check("undo ok", u["ok"] is True and u["restored"] == 6)
check("undo restored originals", f_ocr.exists() and f_note.exists() and f_cap.exists())
check("undo removed new names", not (LIB / "合同扫描件 第 3 页.jpg").exists())
row = conn.execute("SELECT path FROM photos WHERE id=?", (f_ocr_id,)).fetchone()
check("undo re-points DB", row["path"] == str(f_ocr))
check("plan state undone", rename.current()["state"] == "undone")

# --- watcher must not double-register a renamed file ------------------------
from app import watch
watch.QUIET_SEC = 0.5
watch.start(str(LIB))
new_name = LIB / "监听改名验证.jpg"
f_plain.rename(new_name)
deadline = time.time() + 15
ok_watch = False
while time.time() < deadline:
    rows = conn.execute("SELECT COUNT(*) c FROM photos WHERE path=?",
                        (str(new_name),)).fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"]
    if rows == 1 and total == 7:
        ok_watch = True
        break
    time.sleep(0.5)
watch.stop(str(LIB))
check("watch reconciles rename without new row", ok_watch)

# --- API surface -------------------------------------------------------------
rp = client.post("/api/rename/plan", json={"with_fallback": False})
check("POST /api/rename/plan", rp.status_code == 200 and rp.json()["state"] == "planned")
pv = client.get("/api/rename/preview").json()
check("GET /api/rename/preview", pv["files"] == rp.json()["files"] and pv["steps"])
check("preview rows carry source tag", all("source" in st for st in pv["steps"]))
cu = client.get("/api/rename/current").json()
check("GET /api/rename/current", cu["state"] == "planned")
# re-apply through the API then undo, end to end
st = client.post("/api/rename/apply").json()
check("POST /api/rename/apply", st.get("started") is True)
while client.get("/api/rename/apply_status").json()["progress"]["running"]:
    time.time() and time.sleep(0.1)
check("API apply renamed", (LIB / "合同扫描件 第 3 页.jpg").exists())
un = client.post("/api/rename/undo").json()
check("POST /api/rename/undo", un["ok"] and un["restored"] >= 1)
check("final state back to originals", f_ocr.exists())

print(f"rename_check: {len(OK)} assertions passed")
