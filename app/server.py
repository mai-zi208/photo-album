"""FastAPI server: scan control, status, timeline paging, thumbnail serving."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import RESOURCE_ROOT, SHARE_DIR, THUMB_SIZES, thumb_path
from . import (caption, db, dups, ingest, layout, library, memories, model_dl,
               ocr, people, places, rename, scenes, semantic, share, watch)
from . import albums as col
from . import sources as src_mod

app = FastAPI(title="photo-album core")
db.init_db()
dups.ensure_phash_schema()

# previously scanned folders resume watching on boot (daemon observers, so
# tooling that imports this module never hangs on exit)
for _r in db.get_conn().execute("SELECT root FROM scan_state").fetchall():
    watch.start(_r["root"])


@app.on_event("startup")
def autoscan_common_folders() -> None:
    """修改意见 1：自动检索系统常用文件夹（图片/桌面/下载/文档）并建立索引。
    首次启动全扫；之后增量重扫（2 小时内扫过就跳过，日常新增靠 watcher）。
    图片文件夹仍是默认文件夹，只有它允许改动真实文件。"""
    try:
        ingest.start_common_scan()
    except OSError:
        pass


class ScanRequest(BaseModel):
    root: str


@app.post("/api/scan")
def scan(req: ScanRequest) -> dict:
    return ingest.start_scan(req.root)


@app.get("/api/status")
def status() -> dict:
    return {"progress": ingest.progress_snapshot(), "db": db.stats(),
            "ocr": ocr.progress_snapshot(), "phash": dups.progress_snapshot(),
            "watch": watch.progress_snapshot(),
            "semantic": semantic.progress_snapshot(),
            "scenes": scenes.progress_snapshot(),
            "caption": caption.progress_snapshot()}


@app.post("/api/semantic/restart")
def semantic_restart() -> dict:
    return semantic.start_backfill()


@app.get("/api/models/status")
def models_status() -> dict:
    return model_dl.status()


@app.post("/api/models/install")
def models_install() -> dict:
    return model_dl.install()


class CaptionConfigRequest(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    allow_screenshots: bool | None = None
    prompt: str | None = None


@app.get("/api/caption/config")
def caption_config_get() -> dict:
    return caption.public_config()


@app.post("/api/caption/config")
def caption_config_set(req: CaptionConfigRequest) -> dict:
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    return caption.save_config(patch)


@app.post("/api/caption/{file_id}")
def caption_generate(file_id: str, force: bool = False) -> dict:
    if len(file_id) != 64:
        raise HTTPException(400, "bad id")
    r = caption.generate_now(file_id, force=force)
    if not r["ok"] and r["reason"] == "unknown photo":
        raise HTTPException(404, "unknown photo")
    return r


@app.post("/api/caption/restart")
def caption_restart() -> dict:
    return caption.start()


class WatchRequest(BaseModel):
    root: str


@app.get("/api/watch")
def watch_status() -> dict:
    return watch.progress_snapshot()


@app.post("/api/watch/start")
def watch_start(req: WatchRequest) -> dict:
    return watch.start(req.root)


@app.post("/api/watch/stop")
def watch_stop(req: WatchRequest | None = None) -> dict:
    return watch.stop(req.root if req else None)


@app.get("/api/dups")
def dups_list() -> dict:
    groups = db.dup_groups()
    return {"groups": groups, "count": len(groups),
            "dupes": sum(len(g["items"]) for g in groups)}


@app.post("/api/dups/restart")
def dups_restart() -> dict:
    return dups.start_backfill()


@app.post("/api/ocr/start")
def ocr_start() -> dict:
    return ocr.start_ocr()


@app.post("/api/ocr/stop")
def ocr_stop() -> dict:
    return ocr.stop_ocr()


def _base_item(r: dict) -> dict:
    return {
        "id": r["id"], "taken_at": r["taken_at"], "kind": r["kind"],
        "status": r["status"], "width": r["width"], "height": r["height"],
        "bytes": r["bytes"], "path": r["path"],
        "ext": r.get("ext"), "duration": r.get("duration"),
        "dup_group": r.get("dup_group"),
        "note": r.get("note"), "caption": r.get("caption"),
        "ocr_snippet": "", "source": "", "score": None,
    }


def _ymd_range(year: int | None = None, month: int | None = None,
               day: int | None = None) -> tuple[float, float]:
    """Local-time [start, end) epoch pair for a y/m/d prefix of any length."""
    now = datetime.now()
    y = year or now.year
    m = month or 1
    d = day or 1
    start = datetime(y, m, d)
    if day:
        end = start + timedelta(days=1)
    elif month:
        end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1)
    else:
        end = datetime(y + 1, 1, 1)
    return start.timestamp(), end.timestamp()


_TIME_RES: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"^(19\d{2}|20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?$"),
     ("year", "month", "day")),
    (re.compile(r"^(19\d{2}|20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*月?$"), ("year", "month")),
    (re.compile(r"^(19\d{2}|20\d{2})\s*年?$"), ("year",)),
    (re.compile(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?$"), ("month", "day")),
]


def _parse_time_query(q: str) -> tuple[float, float] | None:
    """'2026年7月2日' / '2026-7' / '2025年' / '7月2日' (this year) -> epoch range."""
    for rx, names in _TIME_RES:
        mt = rx.match(q.strip())
        if not mt:
            continue
        kw = dict(zip(names, (int(g) for g in mt.groups())))
        try:
            return _ymd_range(**kw)
        except (ValueError, OSError):  # 13月/32日; pre-1970 years on Windows
            return None
    return None


@app.get("/api/search")
def search(q: str, limit: int = 200, engine: str = "all") -> dict:
    """engine: text (OCR FTS + notes + filename + time) | semantic | all."""
    limit = max(1, min(limit, 500))
    items, seen = [], {}

    def _merge(r, source, extra=None):
        it = seen.get(r["id"])
        if it is not None:
            if source not in it["source"]:
                it["source"] += "+" + source
        else:
            it = _base_item(r)
            it["source"] = source
            seen[r["id"]] = it
            items.append(it)
        if extra:
            it.update(extra)
        return it

    if engine in ("text", "all"):
        match = ocr.build_query_match(q)
        if match:
            for r in db.search_photos(match, limit):
                _merge(r, "ocr", {"ocr_snippet": (r.get("ocr_raw") or "")[:160]})
        for r in db.search_notes(q, limit):  # notes/captions: plain substring
            matched = []
            if q.lower() in (r.get("note") or "").lower():
                matched.append("note")
            if q.lower() in (r.get("caption") or "").lower():
                matched.append("caption")
            if not matched:  # SQL LIKE hit but python differs (collation); attribute both
                matched = ["note", "caption"]
            it = _merge(r, matched[0])
            for m in matched[1:]:
                if m not in it["source"]:
                    it["source"] += "+" + m
            it["note"] = r.get("note")
            it["caption"] = r.get("caption")
        for r in db.search_files(q, limit):  # whole-path substring (messy names)
            _merge(r, "file")
        trange = _parse_time_query(q)  # "2026年7月2日" / "2026-07" / "3月21日" / "2025"
        if trange:
            for r in db.query_photos(ts_from=trange[0], ts_to=trange[1],
                                     limit=limit):
                _merge(r, "time")

    sem_err = None
    if engine in ("semantic", "all") and semantic.model_present():
        try:
            for fid, score in semantic.search_text(q, limit):
                it = seen.get(fid)
                if it is not None:
                    if "semantic" not in it["source"]:
                        it["source"] += "+semantic"
                    it["score"] = round(score, 4)
                else:
                    row = db.get_conn().execute(
                        "SELECT * FROM photos WHERE id=?", (fid,)).fetchone()
                    if row is None or row["status"] == "missing":
                        continue
                    it = _base_item(dict(row))
                    it["source"] = "semantic"
                    it["score"] = round(score, 4)
                    seen[fid] = it
                    items.append(it)
        except Exception as e:
            sem_err = f"{type(e).__name__}: {e}"

    if engine == "semantic":
        items.sort(key=lambda i: -(i["score"] or 0))
    else:
        items.sort(key=lambda i: -i["taken_at"])
    return {"items": items[:limit], "query": q, "engine": engine,
            "count": len(items), "semantic_error": sem_err}


@app.get("/api/similar/{file_id}")
def similar(file_id: str, limit: int = 24) -> dict:
    if len(file_id) != 64:
        raise HTTPException(400, "bad id")
    if not semantic.model_present():
        raise HTTPException(409, "semantic model not installed")
    row = db.get_conn().execute(
        "SELECT * FROM photos WHERE id=?", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "unknown photo")
    conn = db.get_conn()
    out = []
    for fid, score in semantic.similar_to(file_id, limit):
        r = conn.execute("SELECT * FROM photos WHERE id=?", (fid,)).fetchone()
        if r is None:
            continue
        it = _base_item(dict(r))
        it["score"] = round(score, 4)
        out.append(it)
    return {"items": out, "of": file_id, "count": len(out)}


@app.get("/api/onthisday")
def on_this_day(date: str | None = None) -> dict:
    """Photos from previous years taken on this month-day (or a given MM-DD).

    Current year is excluded — 'on this day' means memories from the past."""
    if date:
        if not re.fullmatch(r"\d{2}-\d{2}", date):
            raise HTTPException(400, "date must be MM-DD")
        mm_dd = date
    else:
        mm_dd = time.strftime("%m-%d", time.localtime())
    month, day = (int(x) for x in mm_dd.split("-"))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise HTTPException(400, "date out of range")
    this_year = time.localtime().tm_year
    groups: dict[int, list] = {}
    total = 0
    for r in db.on_this_day(mm_dd):
        y = time.localtime(r["taken_at"]).tm_year
        if y == this_year:
            continue
        it = _base_item(r)
        groups.setdefault(y, []).append(it)
        total += 1
    years = [{"year": y, "items": groups[y], "count": len(groups[y])}
             for y in sorted(groups, reverse=True)]
    return {"date": mm_dd, "years": years, "count": total,
            "year_span": (len(years) > 1 and f"{years[-1]['year']}–{years[0]['year']}") or ""}


@app.get("/api/grouped")
def grouped(level: str = "day", kind: str | None = None, ext: str | None = None,
            folder: str | None = None, status: str = "visible",
            year: int | None = None, month: int | None = None,
            date_from: float | None = None, date_to: float | None = None,
            limit: int = 500) -> dict:
    """Year/month/day cover cards for the gallery '照片' tab and reviews."""
    if level not in db._GROUP_FMT:
        raise HTTPException(400, "level must be year|month|day")
    if year or month:
        try:
            date_from, date_to = _ymd_range(year, month)
        except ValueError:
            raise HTTPException(400, "bad year/month")
    groups = db.grouped(level, limit=limit, kind=kind, ext=ext, folder=folder,
                        status=status, ts_from=date_from, ts_to=date_to)
    return {"level": level, "count": len(groups),
            "groups": [{"key": g["key"], "count": g["count"],
                        "cover": _base_item(g["row"])} for g in groups]}


@app.get("/api/albums")
def albums() -> dict:
    """图集页「性质」分区：手机相片/图片/截屏/视频/录屏；「来源」分区按图片
    所在目录对应的软件（微信/QQ/浏览器下载/手机相机/截图工具/桌面/文档/
    图片文件夹）分组。"""
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT id, kind, status, ext, source, path, taken_at, indexed_at
           FROM photos WHERE status!='broken' AND purged_at IS NULL
             AND trash_at IS NULL""").fetchall()
    buckets: dict[str, list] = {k: [] for k in
                                ("phone", "photo", "screenshot", "video",
                                 "record", "gif")}
    for r in rows:
        if r["status"] == "missing":
            continue
        if (r["ext"] or "").lower() == ".gif":
            buckets["gif"].append(r)
        if r["source"] == "dcim" and r["kind"] in ("photo", "screenshot"):
            buckets["phone"].append(r)      # 手机相机照片 + 手机截屏
        elif r["kind"] == "photo":
            buckets["photo"].append(r)
        elif r["kind"] == "screenshot":
            buckets["screenshot"].append(r)
        elif r["kind"] == "video":
            buckets["record" if db.is_record_path(r["path"]) else "video"].append(r)

    def card(lst: list, key: str, name: str) -> dict:
        pool = [r for r in lst if r["status"] == "ok"] or lst
        best = max(pool, key=lambda r: r["taken_at"]) if pool else None
        return {"key": key, "name": name, "count": len(lst),
                "cover": best["id"] if best else None}

    smart = [card(buckets["phone"], "phone", "手机相片"),
             card(buckets["photo"], "photo", "图片"),
             card(buckets["screenshot"], "screenshot", "截屏"),
             card(buckets["video"], "video", "视频"),
             card(buckets["record"], "record", "录屏")]
    source_list = [{"key": g["source"], "name": src_mod.label(g["source"]),
                    "count": g["count"], "cover": g["cover"]}
                   for g in db.source_groups()]
    user_list = [{"id": a["id"], "name": a["name"], "count": a["count"],
                  "cover": a["cover"], "created_at": a["created_at"]}
                 for a in col.albums_with_stats()]
    return {"smart": smart, "sources": source_list, "user": user_list,
            "gif": card(buckets["gif"], "gif", "动图")}


class AlbumCreate(BaseModel):
    name: str


class AlbumRename(BaseModel):
    id: str
    name: str


class AlbumMembers(BaseModel):
    id: str
    ids: list[str]


@app.post("/api/albums")
def album_create(req: AlbumCreate) -> dict:
    out = col.create(req.name)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "failed"))
    return out


@app.post("/api/albums/rename")
def album_rename(req: AlbumRename) -> dict:
    out = col.rename(req.id, req.name)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "failed"))
    return out


@app.post("/api/albums/add")
def album_add(req: AlbumMembers) -> dict:
    if any(len(i) != 64 for i in req.ids):
        raise HTTPException(400, "bad id")
    out = col.add(req.id, req.ids)
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason", "failed"))
    return out


@app.post("/api/albums/remove")
def album_remove(req: AlbumMembers) -> dict:
    out = col.remove(req.id, req.ids)
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason", "failed"))
    return out


@app.get("/api/albums/{album_id}")
def album_get(album_id: str) -> dict:
    a = col.get_album(album_id)
    if a is None:
        raise HTTPException(404, "no such album")
    return a


@app.delete("/api/albums/{album_id}")
def album_delete(album_id: str) -> dict:
    out = col.delete(album_id)
    if not out.get("ok"):
        raise HTTPException(404, "no such album")
    return out


@app.get("/api/albums_of/{file_id}")
def albums_of(file_id: str) -> dict:
    if len(file_id) != 64:
        raise HTTPException(400, "bad id")
    return {"albums": col.albums_for(file_id)}


@app.get("/api/photo/{file_id}")
def photo_detail(file_id: str) -> dict:
    if len(file_id) != 64:
        raise HTTPException(400, "bad id")
    detail = db.photo_detail(file_id)
    if detail is None:
        raise HTTPException(404, "unknown photo")
    return detail


class NoteRequest(BaseModel):
    id: str
    note: str


@app.post("/api/photo/note")
def set_note(req: NoteRequest) -> dict:
    if len(req.id) != 64:
        raise HTTPException(400, "bad id")
    if not db.set_note(req.id, req.note):
        raise HTTPException(404, "unknown photo")
    return {"ok": True, "id": req.id, "note": req.note.strip() or None}


class ShareRequest(BaseModel):
    ids: list[str]
    title: str | None = None


@app.post("/api/share")
def share_pack(req: ShareRequest) -> dict:
    return share.build(req.ids, req.title)


@app.get("/share/{name}")
def share_get(name: str) -> FileResponse:
    # name is server-generated (s<ts>-<hex>.html / p<ts>-<hex>.zip); keep
    # traversal out anyway
    path = (SHARE_DIR / name).resolve()
    if not path.is_relative_to(SHARE_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "no such share pack")
    if path.suffix.lower() == ".zip":
        return FileResponse(path, media_type="application/zip", filename=name,
                            headers={"Cache-Control": "no-cache"})
    return FileResponse(path, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# 选择模式（全选 / 类选）与索引级操作
# ---------------------------------------------------------------------------


def _view_filters(level_key, kind, ext, folder, status, date_from, date_to,
                  indexed_from, album, has_face, place, source, scene,
                  scene_in=None):
    if level_key:  # "2026" | "2026-07" | "2026-07-02"
        try:
            parts = [int(x) for x in level_key.split("-")]
            date_from, date_to = _ymd_range(*parts)
        except (ValueError, TypeError):
            raise HTTPException(400, "bad level_key")
    scenes = [s for s in (scene_in or "").split(",") if s] or None
    return dict(kind=kind, ext=ext, folder=folder, status=status,
                ts_from=date_from, ts_to=date_to, indexed_from=indexed_from,
                album=album, has_face=has_face, place=place,
                source=source, scene=scene, scenes=scenes)


@app.get("/api/photo_ids")
def photo_ids(limit: int = 20000, level_key: str | None = None,
              kind: str | None = None, ext: str | None = None,
              folder: str | None = None, status: str = "visible",
              date_from: float | None = None, date_to: float | None = None,
              indexed_from: float | None = None, album: str | None = None,
              has_face: bool | None = None, place: str | None = None,
              source: str | None = None, scene: str | None = None,
              scene_in: str | None = None,
              order: str = "taken") -> dict:
    """当前视图的全部 id（全选/类选用，不是分页的一屏）。"""
    limit = max(1, min(limit, 50000))
    kw = _view_filters(level_key, kind, ext, folder, status, date_from, date_to,
                       indexed_from, album, has_face, place, source, scene,
                       scene_in)
    ids = db.query_ids(limit=limit, order=order, **kw)
    return {"ids": ids, "count": len(ids), "order": order}


def _drives() -> list[str]:
    """所有磁盘根（Windows）；取不到就返回空。"""
    try:
        drives = list(os.listdrives())
    except (AttributeError, OSError):
        drives = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                  if Path(f"{c}:\\").is_dir()]
    return [d for d in drives if Path(d).is_dir()]


@app.get("/api/fs/list")
def fs_list(path: str | None = None) -> dict:
    """文件夹选择器数据源（只列目录）。默认从「图片」文件夹起步。"""
    out = library.list_dirs(path)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "无法读取目录"))
    quick = [{"name": Path(r).name, "path": r} for r in ingest.common_roots()]
    for drive in _drives():       # 磁盘根，便于选到图片文件夹之外的位置
        quick.append({"name": drive, "path": drive})
    out["quick"] = quick
    return out


class IdsRequest(BaseModel):
    ids: list[str]


class MoveRequest(BaseModel):
    ids: list[str]
    target: str


class OrganizeRequest(BaseModel):
    ids: list[str]
    mode: str = "timeline"


class ZipRequest(BaseModel):
    ids: list[str]
    title: str | None = None
    dest: str | None = None


@app.post("/api/photos/purge")
def photos_purge(req: IdsRequest) -> dict:
    """删除：原文件移进系统回收站，索引同时移除。"""
    return library.purge(req.ids)


@app.post("/api/photos/move")
def photos_move(req: MoveRequest) -> dict:
    out = library.move(req.ids, req.target)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "移动失败"))
    return out


@app.post("/api/photos/organize")
def photos_organize(req: OrganizeRequest) -> dict:
    out = library.organize(req.ids, req.mode)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "整理失败"))
    return out


@app.post("/api/share/zip")
def share_zip(req: ZipRequest) -> dict:
    out = share.build_zip(req.ids, req.title, dest=req.dest)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "打包失败"))
    return out


@app.get("/api/scenes")
def scenes_summary() -> dict:
    """发现页「智能场景」：本地零样本主题分类结果。"""
    return scenes.summary()


@app.post("/api/scenes/scan")
def scenes_scan() -> dict:
    return scenes.start_backfill()


@app.get("/api/sources")
def sources_summary() -> dict:
    """按下载来源分组（类选·来源 / 更多图集）。"""
    return {"sources": [{"key": g["source"], "name": src_mod.label(g["source"]),
                         "count": g["count"], "cover": g["cover"]}
                        for g in db.source_groups()]}


@app.get("/api/photos")
def photos(taken_at: float | None = None, id_cursor: str | None = None,
           limit: int = 240, level_key: str | None = None,
           kind: str | None = None, ext: str | None = None,
           folder: str | None = None, status: str = "visible",
           date_from: float | None = None, date_to: float | None = None,
           indexed_from: float | None = None, album: str | None = None,
           has_face: bool | None = None, place: str | None = None,
           source: str | None = None, scene: str | None = None,
           scene_in: str | None = None, bucket: str | None = None,
           order: str = "taken") -> dict:
    """Timeline paging (no filters) or a filtered album/day/month listing."""
    limit = max(1, min(limit, 1000))
    if (level_key or kind or ext or folder or date_from or date_to
            or indexed_from or album or has_face is not None
            or place or source or scene or scene_in or bucket or order != "taken"
            or status != "visible"):
        if level_key:  # "2026" | "2026-07" | "2026-07-02" from /api/grouped
            try:
                parts = [int(x) for x in level_key.split("-")]
                date_from, date_to = _ymd_range(*parts)
            except (ValueError, TypeError):
                raise HTTPException(400, "bad level_key")
        rows = db.query_photos(limit=limit, order=order, kind=kind, ext=ext,
                               folder=folder, status=status,
                               ts_from=date_from, ts_to=date_to,
                               indexed_from=indexed_from, album=album,
                               has_face=has_face, place=place,
                               source=source, scene=scene, bucket=bucket,
                               scenes=[s for s in (scene_in or "").split(",") if s] or None)
        items = [{
            "id": r["id"], "taken_at": r["taken_at"], "kind": r["kind"],
            "status": r["status"], "width": r["width"], "height": r["height"],
            "bytes": r["bytes"], "path": r["path"],
            "ext": r.get("ext"), "duration": r.get("duration"),
            "dup_group": r.get("dup_group"),
            "note": r.get("note"),
            "caption": r.get("caption"),
            "face_count": r.get("face_count"),
            "source": r.get("source"), "scene": r.get("scene"),
            "trash_at": r.get("trash_at"),
        } for r in rows]
        return {"items": items, "next_cursor": None, "has_more": False}
    rows = db.page_photos(taken_at, id_cursor, limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": r["id"], "taken_at": r["taken_at"], "kind": r["kind"],
            "status": r["status"], "width": r["width"], "height": r["height"],
            "bytes": r["bytes"], "path": r["path"],
            "ext": r.get("ext"), "duration": r.get("duration"),
            "dup_group": r.get("dup_group"),
            "note": r.get("note"),
            "caption": r.get("caption"),
        }
        for r in rows
    ]
    cursor = {"taken_at": rows[-1]["taken_at"], "id": rows[-1]["id"]} if rows else None
    return {"items": items, "next_cursor": cursor, "has_more": has_more}


@app.get("/thumb/{file_id}.webp")
def thumb(file_id: str, size: int = 512) -> FileResponse:
    if size not in THUMB_SIZES or len(file_id) != 64:
        raise HTTPException(400, "bad thumbnail request")
    path = thumb_path(file_id, size)
    if not path.exists():
        path = thumb_path(file_id, 128)
        if not path.exists():
            raise HTTPException(404, "no thumbnail")
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# Containers browsers can play inline in <video>; others open the original.
INLINE_VIDEO_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mov": "video/mp4",  # fine while the track is h264; else fallback link
}


@app.get("/media/{file_id}")
def media(file_id: str) -> FileResponse:
    """Original-file通路 for playback: never transcodes, streams the file as
    is. FileResponse honours HTTP Range, so seeking works out of the box."""
    if len(file_id) != 64:
        raise HTTPException(400, "bad id")
    row = db.get_conn().execute(
        "SELECT path, ext, kind FROM photos WHERE id=?", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "unknown file")
    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(404, "file gone")
    ext = (row["ext"] or path.suffix).lower()
    media_type = INLINE_VIDEO_TYPES.get(ext) or (
        "image/gif" if ext == ".gif" else None)
    if media_type is None:
        import mimetypes
        media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    # no long-lived caching: the path may later hold a different file
    return FileResponse(path, media_type=media_type,
                        headers={"Cache-Control": "no-cache"})


class PlanRequest(BaseModel):
    root_out: str
    limit: int | None = None


class ApplyRequest(BaseModel):
    mode: str = "copy"          # copy | move


@app.post("/api/layout/plan")
def layout_plan(req: PlanRequest) -> dict:
    return layout.plan(req.root_out, req.limit)


@app.get("/api/layout/current")
def layout_current() -> dict:
    cur = layout.current()
    return cur or {"state": "none", "files": 0}


@app.post("/api/layout/apply")
def layout_apply(req: ApplyRequest) -> dict:
    return layout.start_apply(req.mode)


@app.get("/api/layout/apply_status")
def layout_apply_status() -> dict:
    return {"progress": layout.apply_progress(), "plan": layout.current()}


@app.post("/api/layout/undo")
def layout_undo() -> dict:
    return layout.undo()


# ---------------------------------------------------------------------------
# content rename (messy name -> what the picture actually shows)
# ---------------------------------------------------------------------------

class RenamePlanRequest(BaseModel):
    with_fallback: bool = False      # also name content-less files by type+date
    limit: int | None = None


@app.post("/api/rename/plan")
def rename_plan(req: RenamePlanRequest) -> dict:
    return rename.plan(req.with_fallback, req.limit)


@app.get("/api/rename/current")
def rename_current() -> dict:
    cur = rename.current()
    return cur or {"state": "none", "files": 0}


@app.get("/api/rename/preview")
def rename_preview(limit: int = 200) -> dict:
    return rename.preview(max(1, min(limit, 1000)))


@app.post("/api/rename/apply")
def rename_apply() -> dict:
    return rename.start_apply()


@app.get("/api/rename/apply_status")
def rename_apply_status() -> dict:
    return {"progress": rename.apply_progress(), "plan": rename.current()}


@app.post("/api/rename/undo")
def rename_undo() -> dict:
    return rename.undo()


# ---------------------------------------------------------------------------
# memories ("精彩回忆"): instant soundtrack + optional ffmpeg mp4 export
# ---------------------------------------------------------------------------

AUDIO_TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac",
               ".ogg": "audio/ogg", ".wav": "audio/wav"}


@app.get("/api/memories/tracks")
def memories_tracks() -> dict:
    return {"tracks": memories.tracks()}


@app.get("/api/memories/track/{name}")
def memories_track(name: str) -> FileResponse:
    path = memories.track_file(name)
    if path is None:
        raise HTTPException(404, "unknown track")
    mt = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mt,
                        headers={"Cache-Control": "public, max-age=86400"})


class MemoryExport(BaseModel):
    ids: list[str]
    track: str | None = None
    each: float = 3.5
    xfade: float = 0.8


@app.post("/api/memories/export")
def memories_export(req: MemoryExport) -> dict:
    ids = [i for i in req.ids if len(i) == 64]
    if not ids:
        raise HTTPException(400, "no valid ids")
    try:
        return memories.start_export(ids, track=req.track or None,
                                     each=req.each, xfade=req.xfade)
    except memories.ExportError as e:
        raise HTTPException(400, str(e))


@app.get("/api/memories/status/{job_id}")
def memories_status(job_id: str) -> dict:
    st = memories.status(job_id)
    if st is None:
        raise HTTPException(404, "unknown job")
    st.pop("ids", None)
    return st


@app.get("/api/memories/file/{job_id}")
def memories_file(job_id: str) -> FileResponse:
    path = memories.file_path(job_id)
    if path is None:
        raise HTTPException(404, "not ready")
    return FileResponse(path, media_type="video/mp4",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/people")
def people_summary() -> dict:
    """Count of photos containing people + backfill progress, for the 发现页
    人物 card. Detection is local/offline (Haar cascades bundled in opencv)."""
    return people.summary()


@app.post("/api/people/scan")
def people_scan() -> dict:
    """Kick the face-detection slow lane over pending photos (no-op when idle
    or the detector is unavailable)."""
    return people.start_backfill()


@app.get("/api/places")
def places_summary() -> dict:
    """City album from EXIF GPS, reverse-geocoded against a built-in offline
    city table (nearest city within ~80 km, else 其他地点). No network calls."""
    return places.summary()


@app.post("/api/places/scan")
def places_scan() -> dict:
    """Kick the GPS backfill over rows indexed before this lane existed."""
    return places.start_backfill()


app.mount("/", StaticFiles(directory=RESOURCE_ROOT / "web", html=True), name="web")
