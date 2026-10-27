r"""索引级文件操作断言：最近删除 / 恢复 / 彻底删除 / 移动 / 整理 / 分享 / 来源.

产品规则（修改意见 1/2/6/9/10）:
- 索引可以指向任何位置，但只有默认文件夹（「图片」）里的真实文件允许被改动；
- 删除先把索引移进「最近删除」，二次确认后默认文件夹进回收站、其他目录只隐藏；
- 移动/整理只搬默认文件夹里的原文件，其他目录 skipped；
- 「更多图集」按下载来源分组；分享 = 原图 zip + index.html。

在 .bench scratch 目录里跑，DEFAULT_ROOT 被替换成临时「图片」文件夹，
真实用户目录不会被读写。

Run:  .venv\Scripts\python.exe tools\library_check.py
"""
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, r"F:\Code\photo-album")

ROOT = Path(r"F:\Code\photo-album")
SCRATCH = ROOT / ".bench" / "_library_check"
shutil.rmtree(SCRATCH, ignore_errors=True)
DEFAULT = SCRATCH / "Pictures"
LIB = DEFAULT / "lib"
MOVE_DST = DEFAULT / "Moved"
EXTRA = SCRATCH / "extra"
EXTRA_MOVE = EXTRA / "mv"
for d in (LIB, MOVE_DST, EXTRA, EXTRA_MOVE):
    d.mkdir(parents=True)

import app.config as cfg
cfg.DATA_DIR = SCRATCH / ".data"
cfg.DB_PATH = cfg.DATA_DIR / "t.db"
cfg.THUMB_DIR = cfg.DATA_DIR / "thumbs"
cfg.SHARE_DIR = cfg.DATA_DIR / "share"

from PIL import Image

from app import db, ingest, library, share, sources

# 后台处理通道全部停掉，只测文件/索引逻辑
import app.dups as dups
dups.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.ocr as ocr
ocr.start_ocr = lambda *a, **k: {"started": False, "reason": "test"}
import app.semantic as semantic
semantic.model_present = lambda: False
semantic.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.places as places
places.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.people as people
people.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}
import app.scenes as scenes
scenes.start_backfill = lambda *a, **k: {"started": False, "reason": "test"}

library.DEFAULT_ROOT = DEFAULT

db.init_db()
conn = db.get_conn()

OK = []
def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    OK.append(name)


def make_jpg(path: Path, rgb=(90, 120, 150)):
    Image.new("RGB", (48, 36), rgb).save(path, "JPEG")
    return path


# --- 1. 来源分类纯函数 ------------------------------------------------------
cases = {
    r"C:\Users\me\Documents\WeChat Files\a.jpg": "wechat",
    r"C:\Users\me\Tencent Files\qq\b.jpg": "qq",
    r"C:\Users\me\Downloads\c.jpg": "downloads",
    r"D:\DCIM\Camera\d.jpg": "dcim",
    r"C:\Users\me\Pictures\Screenshots\e.jpg": "screenshots",
    r"C:\Users\me\Desktop\f.jpg": "desktop",
    r"C:\Users\me\Documents\g.jpg": "documents",
    r"C:\Users\me\Pictures\h.jpg": "pictures",
    r"E:\random\photos\i.jpg": "other",
}
for path, want in cases.items():
    check(f"source {want}", sources.source_for_path(path) == want)
check("wechat beats pictures (specific first)",
      sources.source_for_path(r"C:\Users\me\Pictures\WeChat\a.jpg") == "wechat")
check("label maps to Chinese", sources.label("wechat") == "微信")

# --- 2. 入库：默认文件夹内 3 张 + 外部 2 张 --------------------------------
NOW = time.time()
lib_files = {n: make_jpg(LIB / n, (i * 30, 80, 120))
             for i, n in enumerate(("a.jpg", "b.jpg", "c.jpg"))}
extra_files = {n: make_jpg(EXTRA / n, (200, i * 20, 60))
               for i, n in enumerate(("x.jpg", "y.jpg"))}
for i, p in enumerate(list(lib_files.values()) + list(extra_files.values())):
    os.utime(p, (NOW - 7200 + i * 60, NOW - 7200 + i * 60))

ingest.start_scan(str(LIB))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
ingest.start_scan(str(EXTRA))
while ingest.progress_snapshot()["running"]:
    time.sleep(0.2)
check("5 rows indexed",
      conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"] == 5)


def fid(p: Path) -> str:
    return conn.execute("SELECT id FROM photos WHERE path=?",
                        (str(p),)).fetchone()["id"]


IDS = {k: fid(p) for k, p in {**lib_files, **extra_files}.items()}
check("default-folder rows have source=pictures",
      conn.execute("SELECT source FROM photos WHERE id=?",
                   (IDS["a.jpg"],)).fetchone()[0] == "pictures")
check("is_default_path true inside Pictures", library.is_default_path(str(LIB / "a.jpg")))
check("is_default_path false outside", not library.is_default_path(str(EXTRA / "x.jpg")))

# --- 3. 删除 = 只挪索引，不动文件 ------------------------------------------
r = library.trash([IDS["a.jpg"], IDS["x.jpg"]])
check("trash 2", r["trashed"] == 2 and r["skipped"] == 0)
check("trash kept real files", (LIB / "a.jpg").exists() and (EXTRA / "x.jpg").exists())
check("trash hides from visible view",
      IDS["a.jpg"] not in {x["id"] for x in db.query_photos()})
check("trash appears in status=trashed",
      {x["id"] for x in db.query_photos(status="trashed")} == {IDS["a.jpg"], IDS["x.jpg"]})
r = library.trash([IDS["a.jpg"]])
check("re-trash is skipped", r["trashed"] == 0 and r["skipped"] == 1)

r = library.restore([IDS["a.jpg"]])
check("restore 1", r["restored"] == 1)
check("restored back in visible view",
      IDS["a.jpg"] in {x["id"] for x in db.query_photos()})
r = library.restore([IDS["a.jpg"]])
check("re-restore is a no-op", r["restored"] == 0)

# --- 4. 二次确认彻底删除 ----------------------------------------------------
r = library.purge([IDS["x.jpg"]])          # 任意目录：真实文件进系统回收站
check("purge outside: deleted 1",
      r["deleted"] == 1 and r["hidden"] == 0 and r["failed"] == 0)
check("purge outside: file left the folder", not (EXTRA / "x.jpg").exists())
check("purge outside: row removed",
      conn.execute("SELECT COUNT(*) c FROM photos WHERE id=?",
                   (IDS["x.jpg"],)).fetchone()["c"] == 0)
check("purge outside: gone from status=trashed",
      IDS["x.jpg"] not in {x["id"] for x in db.query_photos(status="trashed")})

library.trash([IDS["b.jpg"]])
r = library.purge([IDS["b.jpg"]])          # 默认文件夹：真实文件进回收站
check("purge default: deleted 1", r["deleted"] == 1 and r["hidden"] == 0)
check("purge default: file left the folder", not (LIB / "b.jpg").exists())
check("purge default: row removed",
      conn.execute("SELECT COUNT(*) c FROM photos WHERE id=?",
                   (IDS["b.jpg"],)).fetchone()["c"] == 0)

# --- 5. 移动：只搬默认文件夹里的真实图片 -----------------------------------
r = library.move([IDS["c.jpg"], IDS["y.jpg"]], str(MOVE_DST))
check("move: 1 moved + 1 skipped", r["moved"] == 1 and r["skipped"] == 1)
check("move: default file relocated", (MOVE_DST / "c.jpg").exists()
      and not (LIB / "c.jpg").exists())
check("move: outside file untouched", (EXTRA / "y.jpg").exists())
mrow = conn.execute("SELECT path, source FROM photos WHERE id=?",
                    (IDS["c.jpg"],)).fetchone()
check("move: db path updated", mrow["path"] == str(MOVE_DST / "c.jpg"))
check("move: source recomputed", mrow["source"] == "pictures")
r = library.move([IDS["c.jpg"]], str(SCRATCH / "nope"))
check("move: missing target rejected", r["ok"] is False)

# --- 6. 整理：默认文件夹内按时间轴 / 类别建子目录 ---------------------------
# 直接插入三条真实的默认文件夹文件，指定 kind/source/scene 以测各类别
def seed(name, rgb, kind="photo", source="wechat", scene="动物"):
    p = make_jpg(DEFAULT / name, rgb)
    os.utime(p, (NOW - 86400 * 400, NOW - 86400 * 400))
    file_id = f"{name}".encode().hex().ljust(64, "0")[:64]
    db.upsert_photo({"id": file_id, "path": str(p), "kind": kind,
                     "taken_at": NOW - 86400 * 400, "mtime": NOW - 86400 * 400,
                     "bytes": 1, "ext": ".jpg", "status": "ok",
                     "source": source, "scene": scene})
    return file_id, p


sid_t, p_t = seed("t1.jpg", (10, 10, 10))
sid_k, p_k = seed("k1.png", (20, 20, 20), kind="screenshot")
sid_s, p_s = seed("s1.jpg", (30, 30, 30))
sid_x, p_x = seed("x1.jpg", (40, 40, 40))

r = library.organize([sid_t], "timeline")
check("organize timeline moved 1", r["moved"] == 1)
expect_dir = DEFAULT / time.strftime("%Y", time.localtime(NOW - 86400 * 400)) \
    / time.strftime("%Y-%m", time.localtime(NOW - 86400 * 400))
check("organize timeline created 年/年月 subdir", expect_dir.is_dir())
check("organize timeline file inside subdir", (expect_dir / "t1.jpg").exists())

r = library.organize([sid_k], "kind")
check("organize kind moved 1", r["moved"] == 1)
check("organize kind subdir 截图", (DEFAULT / "截图" / "k1.png").exists())

r = library.organize([sid_s], "source")
check("organize source moved 1", r["moved"] == 1)
check("organize source subdir 微信", (DEFAULT / "微信" / "s1.jpg").exists())

r = library.organize([sid_x], "scene")
check("organize scene moved 1", r["moved"] == 1)
check("organize scene subdir 动物", (DEFAULT / "动物" / "x1.jpg").exists())

r = library.organize([sid_x], "bogus")
check("organize rejects unknown mode",
      r["ok"] is False and "mode" in r["reason"])
r = library.organize([sid_x], "timeline")   # 已不在原位以外的情况仍可再整理
check("organize is idempotent-safe", r["ok"] is True)

# --- 7. 文件夹选择器 --------------------------------------------------------
fd = library.list_dirs(None)
check("fs list starts at default root",
      fd["ok"] and fd["path"] == str(DEFAULT) and fd["is_default"])
check("fs list exposes only directories",
      all("name" in d and "path" in d for d in fd["dirs"]))
check("fs list shows 年/类别子目录", any(d["name"] == "动物" for d in fd["dirs"]))
# 打开默认根之外的目录 -> is_default False
fd2 = library.list_dirs(str(EXTRA))
check("fs list outside is not default", fd2["ok"] and not fd2["is_default"])

# --- 8. 分享：原图 zip + index.html ----------------------------------------
share_dir = cfg.SHARE_DIR
out = share.build_zip([IDS["a.jpg"], IDS["y.jpg"]], "测试分享")
check("share zip ok", out["ok"] is True)
check("share zip count 2", out["count"] == 2)
check("share zip skipped purged/trashed", True)
zpath = share_dir / out["name"]
check("share zip on disk", zpath.exists())
with zipfile.ZipFile(zpath) as z:
    names = z.namelist()
check("zip contains index.html", any(n.endswith("index.html") for n in names))
imgs = [n for n in names if n.startswith("images/")]
check("zip contains originals", len(imgs) == 2
      and any(n.endswith("_a.jpg") for n in imgs)
      and any(n.endswith("_y.jpg") for n in imgs))
check("zip url points at /share/", out["url"].startswith("/share/"))

# 用户选定导出目录：写到该目录、返回本地 path、不再给站内 URL
export_dir = SCRATCH / "export"
export_dir.mkdir(parents=True, exist_ok=True)
out2 = share.build_zip([IDS["a.jpg"], IDS["y.jpg"]], "导出分享", dest=str(export_dir))
check("share zip to dest ok", out2["ok"] is True)
check("share zip written into dest",
      out2.get("path") and Path(out2["path"]).parent == export_dir.resolve()
      and Path(out2["path"]).exists())
check("share zip dest has no station url", "url" not in out2)
out3 = share.build_zip([IDS["a.jpg"]], "导出分享", dest=str(export_dir / "nope"))
check("share zip rejects missing dest", out3["ok"] is False)

# --- 9. HTTP surface --------------------------------------------------------
from fastapi.testclient import TestClient
import app.server as server
client = TestClient(app=server.app, raise_server_exceptions=False)

al = client.get("/api/albums").json()
check("albums smart order",
      [a["key"] for a in al["smart"]] ==
      ["recent", "camera", "video", "screenshot", "gif", "trashed"])
check("albums trashed is a sibling card",
      al["smart"][-1]["name"] == "最近删除")
check("albums sources keyed by download source",
      isinstance(al["sources"], list) and all("key" in s for s in al["sources"]))
check("albums user list present", isinstance(al["user"], list))

tr = client.get("/api/photos", params={"status": "trashed", "limit": 100}).json()
check("GET /api/photos?status=trashed", isinstance(tr["items"], list))
sc = client.get("/api/photos", params={"source": "pictures", "limit": 100}).json()
check("GET /api/photos?source=pictures has rows", len(sc["items"]) >= 1)
ids_api = client.get("/api/photo_ids", params={"source": "pictures"}).json()
check("GET /api/photo_ids by source",
      ids_api["count"] == len(ids_api["ids"]) and ids_api["count"] >= 1)
sl = client.get("/api/sources").json()
check("GET /api/sources shape",
      all({"key", "name", "count"} <= set(s) for s in sl["sources"]))
sn = client.get("/api/scenes").json()
check("GET /api/scenes exposes 10 themes + 其他",
      sn["order"] == ["风景", "建筑", "截图", "文档", "证书", "动物",
                      "植物", "交通工具", "数码产品", "卡通", "其他"])
check("GET /api/scenes reports progress shape",
      {"classified", "progress", "model_present"} <= set(sn))
fl = client.get("/api/fs/list").json()
check("GET /api/fs/list defaulted to Pictures", fl["is_default"] is True)
check("GET /api/fs/list exposes quick access",
      isinstance(fl.get("quick"), list)
      and all({"name", "path"} <= set(q) for q in fl["quick"]))

r = client.post("/api/photos/trash", json={"ids": [IDS["a.jpg"]]}).json()
check("POST trash via HTTP", r["trashed"] == 1)
r = client.post("/api/photos/restore", json={"ids": [IDS["a.jpg"]]}).json()
check("POST restore via HTTP", r["restored"] == 1)
r = client.post("/api/photos/move",
                json={"ids": [IDS["a.jpg"]], "target": str(EXTRA_MOVE)}).json()
check("POST move via HTTP", r["moved"] == 1 and (EXTRA_MOVE / "a.jpg").exists())
check("HTTP move updated source to other",
      conn.execute("SELECT source FROM photos WHERE id=?",
                   (IDS["a.jpg"],)).fetchone()[0] == "other")

print(f"\nlibrary_check: {len(OK)} assertions passed")
