"""SQLite storage for the photo index. Content hash is the primary key."""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from .config import DB_PATH, ensure_dirs
from .sources import source_for_path

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
  id        TEXT PRIMARY KEY,          -- BLAKE3(content)
  path      TEXT NOT NULL,
  kind      TEXT NOT NULL DEFAULT 'other',   -- screenshot | photo | other
  taken_at  REAL NOT NULL,             -- epoch seconds (EXIF > mtime)
  mtime     REAL NOT NULL,
  width     INTEGER,
  height    INTEGER,
  bytes     INTEGER,
  ext       TEXT,
  status    TEXT NOT NULL DEFAULT 'ok',      -- ok | broken | missing (file gone; row kept for re-reconcile)
  ocr_status TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed
  phash     INTEGER,                          -- 64-bit DCT perceptual hash (signed)
  dup_group TEXT,                             -- id of group leader (set only for dup sets)
  note      TEXT,                             -- user annotation (bypass field, survives moves)
  caption   TEXT,                             -- VLM summary (cloud lane, opt-in per item)
  caption_status TEXT NOT NULL DEFAULT 'off',  -- off | pending | running | done | failed
  sem_status TEXT NOT NULL DEFAULT 'pending', -- pending | running | done | failed
  face_status TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed (local face detection)
  face_count INTEGER,                          -- # faces seen (NULL until scanned; 0 = no people)
  gps_lat   REAL,                              -- EXIF GPS decimal degrees (NULL = no fix)
  gps_lon   REAL,
  place     TEXT,                              -- nearest offline city, or 其他地点
  gps_status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed (offline reverse lookup)
  trash_at  REAL,                              -- NULL = 在架; 有值 = 在「最近删除」里
  purged_at REAL,                              -- 非默认文件夹的索引隐藏标记（真实文件不动）
  source    TEXT,                              -- 下载来源: wechat/qq/downloads/dcim/...
  scene     TEXT,                              -- 智能场景主题（Chinese-CLIP 零样本）
  scene_status TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed
  indexed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at DESC);
CREATE TABLE IF NOT EXISTS scan_state(
  root TEXT PRIMARY KEY,
  last_scan REAL
);
CREATE TABLE IF NOT EXISTS sem_vec(
  id  TEXT PRIMARY KEY,             -- photos.id (content hash)
  dim INTEGER NOT NULL,
  vec BLOB NOT NULL                 -- float32 little-endian, L2-normalised
);
CREATE TABLE IF NOT EXISTS albums(
  id TEXT PRIMARY KEY,              -- random 12-hex, stable like photo ids
  name TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS album_photos(
  album_id TEXT NOT NULL,
  photo_id TEXT NOT NULL,           -- photos.id (content hash: survives rename/move)
  added_at REAL NOT NULL,
  PRIMARY KEY(album_id, photo_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS ocr_fts USING fts5(
  tokens,               -- pre-tokenised text: CJK bigrams + ascii words
  raw UNINDEXED,        -- recognised text for display
  id UNINDEXED
);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        ensure_dirs()
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        # journal_mode 是数据库级持久属性：只在 init_db 里设一次。
        # 每个新连接都去切 WAL 会抢排它锁，扫描线程池建连接时正好撞上
        # 后台 lane 的写事务，就报「database is locked」。
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=8000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass          # 已有别的连接在写，模式下已是 WAL 就不影响
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    if "ocr_status" not in cols:  # migrate pre-OCR databases
        conn.execute(
            "ALTER TABLE photos ADD COLUMN ocr_status TEXT NOT NULL DEFAULT 'pending'")
    if "phash" not in cols:       # migrate pre-dedup databases
        conn.execute("ALTER TABLE photos ADD COLUMN phash INTEGER")
    if "dup_group" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN dup_group TEXT")
    if "note" not in cols:         # migrate pre-notes databases
        conn.execute("ALTER TABLE photos ADD COLUMN note TEXT")
    if "sem_status" not in cols:   # migrate pre-semantic databases
        conn.execute(
            "ALTER TABLE photos ADD COLUMN sem_status TEXT NOT NULL DEFAULT 'pending'")
    if "caption" not in cols:      # migrate pre-caption databases
        conn.execute("ALTER TABLE photos ADD COLUMN caption TEXT")
    if "caption_status" not in cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN caption_status TEXT NOT NULL DEFAULT 'off'")
    if "duration" not in cols:     # videos only (seconds); images stay NULL
        conn.execute("ALTER TABLE photos ADD COLUMN duration REAL")
    if "face_status" not in cols:  # migrate pre-people databases
        conn.execute(
            "ALTER TABLE photos ADD COLUMN face_status TEXT NOT NULL DEFAULT 'pending'")
    if "face_count" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN face_count INTEGER")
    if "gps_lat" not in cols:      # migrate pre-places databases
        conn.execute("ALTER TABLE photos ADD COLUMN gps_lat REAL")
    if "gps_lon" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN gps_lon REAL")
    if "place" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN place TEXT")
    if "gps_status" not in cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN gps_status TEXT NOT NULL DEFAULT 'pending'")
    if "trash_at" not in cols:     # migrate pre-trash databases
        conn.execute("ALTER TABLE photos ADD COLUMN trash_at REAL")
    if "purged_at" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN purged_at REAL")
    if "source" not in cols:       # migrate pre-source databases
        conn.execute("ALTER TABLE photos ADD COLUMN source TEXT")
    if "scene" not in cols:        # migrate pre-scene databases
        conn.execute("ALTER TABLE photos ADD COLUMN scene TEXT")
    if "scene_status" not in cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN scene_status TEXT NOT NULL DEFAULT 'pending'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_ocr ON photos(ocr_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_dup ON photos(dup_group)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_sem ON photos(sem_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_face ON photos(face_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_gps ON photos(gps_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_place ON photos(place)")
    # 「最近删除」已下线：把历史遗留的 trash_at 清空，让这些照片回到正常浏览里
    # （它们对应的真实文件从未被移动过）。列本身保留，避免改表结构影响其它查询。
    conn.execute("UPDATE photos SET trash_at=NULL WHERE trash_at IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_source ON photos(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_scene ON photos(scene)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_scene_status ON photos(scene_status)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sem_vec(
             id TEXT PRIMARY KEY, dim INTEGER NOT NULL, vec BLOB NOT NULL)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS albums(
             id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
             created_at REAL NOT NULL)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS album_photos(
             album_id TEXT NOT NULL, photo_id TEXT NOT NULL,
             added_at REAL NOT NULL, PRIMARY KEY(album_id, photo_id))""")
    # 老库补 source：来源完全是路径的纯字符串推导，一次补齐即可
    legacy = conn.execute(
        "SELECT id, path FROM photos WHERE source IS NULL").fetchall()
    if legacy:
        conn.executemany(
            "UPDATE photos SET source=? WHERE id=?",
            [(source_for_path(r["path"]), r["id"]) for r in legacy])
    conn.commit()


def retry_locked(fn, tries: int = 4):
    """Retry a short write when SQLite answers 'database is locked'.

    扫描线程池 + OCR/向量等后台 lane 会同时写库；WAL 下偶发的 BUSY
    不该把一张好图记成 broken，短退避几次即可。"""
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == tries - 1:
                raise
            time.sleep(0.15 * (2 ** i))


def upsert_photo(row: dict) -> str:
    """Insert or keep existing photo (content dedup). Returns 'inserted'|'existing'.
    Lanes a row should never enter are pre-marked via ocr_status/sem_status
    (videos pass 'done': nothing to OCR, nothing to embed)."""
    conn = get_conn()

    def _write():
        cur = conn.execute(
            """INSERT INTO photos(id, path, kind, taken_at, mtime, width, height,
                                  bytes, ext, status, indexed_at, phash, duration,
                                  ocr_status, sem_status, face_status,
                                  gps_lat, gps_lon, place, gps_status,
                                  source, scene, scene_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (
                row["id"], row["path"], row["kind"], row["taken_at"], row["mtime"],
                row.get("width"), row.get("height"), row.get("bytes"), row.get("ext"),
                row.get("status", "ok"), time.time(), row.get("phash"),
                row.get("duration"),
                row.get("ocr_status", "pending"), row.get("sem_status", "pending"),
                row.get("face_status", "pending"),
                row.get("gps_lat"), row.get("gps_lon"), row.get("place"),
                row.get("gps_status", "pending"),
                row.get("source") or source_for_path(row["path"]),
                row.get("scene"), row.get("scene_status", "pending"),
            ),
        )
        conn.commit()
        return cur.rowcount

    rowcount = retry_locked(_write)
    return "inserted" if rowcount else "existing"


def reconcile_existing(file_id: str, new_path: str, mtime: float) -> bool:
    """A known hash showed up at a different/restored path (watch & rescan).
    Re-attach the row when the old path is gone or the row was marked missing.
    Returns True if the row moved or was revived."""
    conn = get_conn()
    row = conn.execute(
        "SELECT path, status, purged_at FROM photos WHERE id=?", (file_id,)).fetchone()
    if row is None or row["purged_at"] is not None:
        return False  # 用户已彻底移出索引，重新扫到也不复活
    old = row["path"]
    if row["status"] == "missing" or (old != new_path and not os.path.exists(old)):
        # old location was a lie (marked missing, or simply gone): follow the
        # content to wherever it lives now
        conn.execute("UPDATE photos SET path=?, status='ok', mtime=? WHERE id=?",
                     (new_path, mtime, file_id))
        conn.commit()
        return True
    return False  # same live path, or genuine duplicate at two live paths


def demote_previous_at_path(path: str, keep_id: str) -> int:
    """New content took over this path (file edited/replaced): the old photo
    no longer exists on disk, so drop its index row (disk wins)."""
    conn = get_conn()
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM photos WHERE path=? AND id!=?", (path, keep_id)).fetchall()]
    return _forget_ids(conn, ids)


def _forget_ids(conn: sqlite3.Connection, ids: list[str]) -> int:
    if not ids:
        return 0
    def _write():
        conn.executemany("DELETE FROM album_photos WHERE photo_id=?",
                         [(i,) for i in ids])
        conn.executemany("DELETE FROM sem_vec WHERE id=?", [(i,) for i in ids])
        conn.executemany("DELETE FROM ocr_fts WHERE id=?", [(i,) for i in ids])
        conn.executemany("DELETE FROM photos WHERE id=?", [(i,) for i in ids])
        conn.commit()
    retry_locked(_write)
    return len(ids)


def forget_path(conn: sqlite3.Connection, path: str) -> int:
    """文件在工具外被删除（含移进系统回收站）：索引行一并删掉。"""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM photos WHERE path=?", (path,)).fetchall()]
    return _forget_ids(conn, ids)


def forget_prefix(conn: sqlite3.Connection, dirpath: str) -> int:
    prefix = dirpath.rstrip("\\/") + os.sep
    n = len(prefix)
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM photos WHERE substr(path,1,?)=?", (n, prefix)).fetchall()]
    return _forget_ids(conn, ids)


def sweep_missing(root: str) -> int:
    """After a full scan: rows under root whose file vanished while the
    watcher was down. 删除索引（而不是只标记 missing），保证图库与磁盘同步。"""
    conn = get_conn()
    prefix = root.rstrip("\\/") + os.sep
    n = len(prefix)
    rows = conn.execute(
        "SELECT id, path FROM photos WHERE status IN ('ok','missing')"
        " AND substr(path,1,?)=?", (n, prefix)).fetchall()
    gone = [r["id"] for r in rows if not os.path.exists(r["path"])]
    return _forget_ids(conn, gone)


def stats() -> dict:
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE purged_at IS NULL").fetchone()["c"]
    broken = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE status='broken' AND purged_at IS NULL"
    ).fetchone()["c"]
    missing = conn.execute(
        "SELECT COUNT(*) c FROM photos"
        " WHERE status='missing' AND purged_at IS NULL").fetchone()["c"]
    kinds = {
        r["kind"]: r["c"]
        for r in conn.execute("SELECT kind, COUNT(*) c FROM photos GROUP BY kind")
    }
    ocr = {
        r["ocr_status"]: r["c"]
        for r in conn.execute("SELECT ocr_status, COUNT(*) c FROM photos GROUP BY ocr_status")
    }
    dupes = conn.execute(
        "SELECT COUNT(*) c, COUNT(DISTINCT dup_group) g FROM photos"
        " WHERE dup_group IS NOT NULL AND status!='missing'"
        " AND trash_at IS NULL AND purged_at IS NULL").fetchone()
    hashed = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE phash IS NOT NULL AND status='ok'"
        " AND purged_at IS NULL"
    ).fetchone()["c"]
    notes = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE note IS NOT NULL"
        " AND purged_at IS NULL").fetchone()["c"]
    embedded = conn.execute("SELECT COUNT(*) c FROM sem_vec").fetchone()["c"]
    captioned = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE caption IS NOT NULL"
        " AND purged_at IS NULL").fetchone()["c"]
    return {"total": total, "broken": broken, "missing": missing,
            "kinds": kinds, "ocr": ocr,
            "dupes": dupes["c"], "dup_groups": dupes["g"], "hashed": hashed,
            "notes": notes, "embedded": embedded, "captioned": captioned}


def page_photos(cursor_taken: float | None, cursor_id: str | None, limit: int) -> list[dict]:
    conn = get_conn()
    if cursor_taken is None:
        rows = conn.execute(
            "SELECT * FROM photos"
            " WHERE status!='missing' AND trash_at IS NULL AND purged_at IS NULL"
            " ORDER BY taken_at DESC, id LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM photos
               WHERE status!='missing' AND trash_at IS NULL AND purged_at IS NULL
                 AND (taken_at < ? OR (taken_at = ? AND id > ?))
               ORDER BY taken_at DESC, id LIMIT ?""",
            (cursor_taken, cursor_taken, cursor_id or "", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def dup_groups() -> list[dict]:
    """Near-duplicate groups, leader first, biggest group earliest taken."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT dup_group, id, path, taken_at, kind, status, width, height, bytes,
                  (id = dup_group) AS is_leader
           FROM photos
           WHERE dup_group IS NOT NULL AND status!='missing'
             AND trash_at IS NULL AND purged_at IS NULL
           ORDER BY dup_group, is_leader DESC, taken_at DESC"""
    ).fetchall()
    by_group: dict[str, dict] = {}
    for r in rows:
        g = by_group.setdefault(r["dup_group"], {"group_id": r["dup_group"], "items": []})
        g["items"].append({k: r[k] for k in
                           ("id", "path", "taken_at", "kind", "status",
                            "width", "height", "bytes")})
    groups = list(by_group.values())
    groups.sort(key=lambda g: (-len(g["items"]), min(
        i["taken_at"] for i in g["items"])))
    return groups


def place_groups() -> list[dict]:
    """Photos grouped by offline reverse-geocoded place, biggest first.
    Cover prefers a viewable row (status='ok'), same rule as album cards."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT place, COUNT(*) c FROM photos
           WHERE place IS NOT NULL AND status!='missing'
             AND trash_at IS NULL AND purged_at IS NULL
           GROUP BY place ORDER BY c DESC, place""").fetchall()
    out = []
    for r in rows:
        cover = conn.execute(
            """SELECT id FROM photos WHERE place=? AND status='ok'
                 AND trash_at IS NULL AND purged_at IS NULL
               ORDER BY taken_at DESC LIMIT 1""", (r["place"],)).fetchone()
        out.append({"place": r["place"], "count": r["c"],
                    "cover": cover["id"] if cover else None})
    return out


def search_photos(match_expr: str, limit: int = 200) -> list[dict]:
    """Join FTS hits with photo rows; newest first."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.*, ocr_fts.raw AS ocr_raw
           FROM ocr_fts JOIN photos p ON p.id = ocr_fts.id
           WHERE ocr_fts MATCH ? AND p.status!='missing'
             AND p.trash_at IS NULL AND p.purged_at IS NULL
           ORDER BY p.taken_at DESC
           LIMIT ?""",
        (match_expr, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def photo_detail(file_id: str) -> dict | None:
    """One photo: row + full OCR text + near-dup siblings. None if unknown."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM photos WHERE id=?", (file_id,)).fetchone()
    if row is None:
        return None
    detail = dict(row)
    ocr = conn.execute("SELECT raw FROM ocr_fts WHERE id=?", (file_id,)).fetchone()
    detail["ocr_raw"] = ocr["raw"] if ocr else None
    detail["exists"] = os.path.exists(row["path"])
    if row["dup_group"]:
        detail["duplicates"] = [
            dict(r) for r in conn.execute(
                """SELECT id, path, taken_at, status, width, height, bytes
                   FROM photos WHERE dup_group=? AND id!=? AND status!='missing'
                   ORDER BY taken_at DESC""",
                (row["dup_group"], file_id))
        ]
    else:
        detail["duplicates"] = []
    return detail


def set_note(file_id: str, note: str) -> bool:
    """User annotation lives only in the DB (bypass field, never touches files)."""
    conn = get_conn()
    cur = conn.execute("UPDATE photos SET note=? WHERE id=?",
                       (note.strip() or None, file_id))
    conn.commit()
    return cur.rowcount > 0


def search_notes(q: str, limit: int = 200) -> list[dict]:
    """Substring search over user notes and VLM captions; complements OCR FTS.
    Both are bypass fields, so plain LIKE beats FTS at this scale."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM photos
           WHERE (note IS NOT NULL OR caption IS NOT NULL)
             AND (note LIKE ? OR caption LIKE ?) AND status!='missing'
             AND trash_at IS NULL AND purged_at IS NULL
           ORDER BY taken_at DESC LIMIT ?""",
        (f"%{q}%", f"%{q}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def on_this_day(mm_dd: str) -> list[dict]:
    """Photos whose local month-day equals mm_dd ('10-25'), any year.
    Broken/missing rows are excluded — a memory strip should only hold
    viewable pictures. Year filtering (drop current year) is the caller's."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM photos
           WHERE status='ok' AND trash_at IS NULL AND purged_at IS NULL
             AND strftime('%m-%d', taken_at,'unixepoch','localtime')=?
           ORDER BY taken_at DESC""",
        (mm_dd,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- gallery shell queries: grouping, filtered listing, filename search ----

_GROUP_FMT = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}


RECORD_KEYWORDS = ("录屏", "屏幕录制", "屏幕录像", "screen record",
                   "screenrecord", "screencast")
_RECORD_LIKES = tuple(f"%{k}%" for k in RECORD_KEYWORDS)


def is_record_path(path: str | None) -> bool:
    """录屏：文件名/目录名带录屏关键词的普通视频。"""
    p = (path or "").lower()
    return any(k in p for k in RECORD_KEYWORDS)


def _filter_sql(kind=None, ext=None, folder=None, status="visible",
                ts_from=None, ts_to=None, indexed_from=None, album=None,
                has_face=None, place=None, source=None, scene=None,
                scenes=None, bucket=None):
    r"""Shared WHERE builder. folder is a prefix (album dir incl. trailing
    sep); substr beats LIKE on Windows paths because \ % _ all appear there.

    bucket: 图集页「性质」的五个固定分类 —— phone（手机来的照片，含手机
    相机拍摄与手机截屏）/ photo（其余照片）/ screenshot（非手机来源的截屏）
    / video（普通视频）/ record（录屏）。

    status: visible (在架) | trashed (最近删除) | ok / missing / broken / all."""
    where, args = [], []
    if status == "trashed":
        where.append("trash_at IS NOT NULL AND purged_at IS NULL")
    elif status in ("ok", "missing", "broken"):
        where.append("status=? AND trash_at IS NULL AND purged_at IS NULL")
        args.append(status)
    elif status == "all":
        where.append("purged_at IS NULL")
    else:  # visible
        where.append("status!='missing' AND trash_at IS NULL AND purged_at IS NULL")
    if album:
        where.append("id IN (SELECT photo_id FROM album_photos WHERE album_id=?)")
        args.append(album)
    if has_face is not None:
        where.append("face_count>0" if has_face else "face_count=0")
    if place:
        where.append("place=?")
        args.append(place)
    if source:
        where.append("source=?")
        args.append(source)
    if scene:
        where.append("scene=?")
        args.append(scene)
    if scenes:
        where.append("scene IN (" + ",".join("?" * len(scenes)) + ")")
        args.extend(scenes)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if bucket == "phone":
        where.append("kind IN ('photo','screenshot') AND source='dcim'")
    elif bucket == "photo":
        where.append("kind='photo' AND (source IS NULL OR source!='dcim')")
    elif bucket == "screenshot":
        where.append("kind='screenshot' AND (source IS NULL OR source!='dcim')")
    elif bucket in ("video", "record"):
        marks = " OR ".join("lower(path) LIKE ?" for _ in _RECORD_LIKES)
        where.append("kind='video' AND "
                     + (f"({marks})" if bucket == "record" else f"NOT ({marks})"))
        args.extend(_RECORD_LIKES)
    if ext:
        where.append("lower(ext)=?")
        args.append(ext.lower())
    if folder:
        where.append("substr(path,1,?)=?")
        args.extend([len(folder), folder])
    if ts_from is not None:
        where.append("taken_at>=?")
        args.append(ts_from)
    if ts_to is not None:
        where.append("taken_at<?")
        args.append(ts_to)
    if indexed_from is not None:
        where.append("indexed_at>=?")
        args.append(indexed_from)
    return " AND ".join(where) or "1", args


def grouped(level: str, limit: int = 500, **filters) -> list[dict]:
    """Per year/month/day group: count + the newest viewable row as cover
    (broken files have no thumbnail, so status='ok' wins), newest group
    first. Same-second ties are deduped here on the group key."""
    fmt = _GROUP_FMT[level]  # internal constant, never user text
    where, args = _filter_sql(**filters)
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT g.k AS gkey, g.c AS gcount, p.* FROM (
              SELECT strftime('{fmt}', taken_at,'unixepoch','localtime') k,
                     COUNT(*) c,
                     COALESCE(MAX(CASE WHEN status='ok' THEN taken_at END),
                              MAX(taken_at)) m
              FROM photos WHERE {where} GROUP BY k ORDER BY k DESC LIMIT ?
            ) g
            JOIN photos p
              ON strftime('{fmt}', p.taken_at,'unixepoch','localtime')=g.k
             AND p.taken_at=g.m""",
        (*args, limit)).fetchall()
    out, by_key = [], {}
    for r in rows:
        d = dict(r)
        k, c = d.pop("gkey"), d.pop("gcount")
        if k not in by_key:
            entry = {"key": k, "count": c, "row": d}
            by_key[k] = entry
            out.append(entry)
        elif by_key[k]["row"]["status"] != "ok" and d["status"] == "ok":
            by_key[k]["row"] = d  # same-second tie: prefer the viewable one
    return out


def query_photos(limit: int = 1000, order: str = "taken", **filters) -> list[dict]:
    where, args = _filter_sql(**filters)
    order_sql = ("indexed_at DESC, id" if order == "indexed"
                 else "taken_at DESC, id")
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM photos WHERE {where} ORDER BY {order_sql} LIMIT ?",
        (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def search_files(q: str, limit: int = 200) -> list[dict]:
    """Filename search: user filenames are messy by design, and matching the
    whole path lets '微信'/'合同'/'photo_979' hit folders too. instr() avoids
    LIKE's %-_ escaping trap on Windows paths."""
    where, args = _filter_sql()
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM photos WHERE {where} AND instr(lower(path), ?)>0"
        " ORDER BY taken_at DESC LIMIT ?",
        (*args, q.lower(), limit)).fetchall()
    return [dict(r) for r in rows]


def query_ids(limit: int = 20000, order: str = "taken", **filters) -> list[str]:
    """Just the ids of a filtered view (全选 / 类选 needs every id, not a page)."""
    where, args = _filter_sql(**filters)
    order_sql = ("indexed_at DESC, id" if order == "indexed"
                 else "taken_at DESC, id")
    conn = get_conn()
    rows = conn.execute(
        f"SELECT id FROM photos WHERE {where} ORDER BY {order_sql} LIMIT ?",
        (*args, limit)).fetchall()
    return [r["id"] for r in rows]


def scene_groups() -> list[dict]:
    """智能场景主题分组（scene 由 Chinese-CLIP 零样本打标）。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT scene, COUNT(*) c FROM photos
           WHERE scene IS NOT NULL AND status!='missing'
             AND trash_at IS NULL AND purged_at IS NULL
           GROUP BY scene ORDER BY c DESC, scene""").fetchall()
    out = []
    for r in rows:
        cover = conn.execute(
            """SELECT id FROM photos WHERE scene=? AND status='ok'
                 AND trash_at IS NULL AND purged_at IS NULL
               ORDER BY taken_at DESC LIMIT 1""", (r["scene"],)).fetchone()
        out.append({"scene": r["scene"], "count": r["c"],
                    "cover": cover["id"] if cover else None})
    return out


def source_groups() -> list[dict]:
    """按下载来源分组（更多图集 / 类选·来源）。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT source, COUNT(*) c FROM photos
           WHERE source IS NOT NULL AND status!='missing'
             AND trash_at IS NULL AND purged_at IS NULL
           GROUP BY source ORDER BY c DESC, source""").fetchall()
    out = []
    for r in rows:
        cover = conn.execute(
            """SELECT id FROM photos WHERE source=? AND status='ok'
                 AND trash_at IS NULL AND purged_at IS NULL
               ORDER BY taken_at DESC LIMIT 1""", (r["source"],)).fetchone()
        out.append({"source": r["source"], "count": r["c"],
                    "cover": cover["id"] if cover else None})
    return out
