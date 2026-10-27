"""真实文件操作的执行层（删除/恢复/移动/整理）. 

产品规则:
- 删除 = 直接把原文件移进系统回收站，索引行同时移除（不再有「最近删除」中间态）；
- 移动 = 弹出文件夹选择器，任意图片都可以移动到任意目标文件夹；
- 整理 = 默认文件夹内按时间轴（或所选类别）建立子目录归档（前端已下线）。

所有函数都是同步执行的：一次操作最多几千个 id，失败逐条记录，不让一个
坏文件中断整批。
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import sources
from .config import known_folders
from .db import get_conn

# 默认文件夹 = 系统「图片」文件夹（可能已被用户重定向到 D:\图片 等）
DEFAULT_ROOT = Path(known_folders().get("pictures") or (Path.home() / "Pictures"))

KIND_LABELS = {"photo": "照片", "screenshot": "截图", "video": "视频",
               "other": "其他"}


def default_root() -> str:
    return str(DEFAULT_ROOT)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_default_path(path: str) -> bool:
    """文件是否落在默认文件夹里（解析 junction/OneDrive 重定向后比较）。"""
    try:
        p = _norm(path)
        r = _norm(str(DEFAULT_ROOT))
    except (OSError, ValueError):
        return False
    return p == r or p.startswith(r + os.sep)


def _rows(ids: list[str]) -> list[dict]:
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and len(i) == 64]
    if not ids:
        return []
    conn = get_conn()
    out = []
    CHUNK = 500
    for k in range(0, len(ids), CHUNK):
        part = ids[k:k + CHUNK]
        marks = ",".join("?" * len(part))
        out.extend(dict(r) for r in conn.execute(
            f"SELECT * FROM photos WHERE id IN ({marks})", part).fetchall())
    return out


def _drop_side_tables(conn, file_id: str) -> None:
    conn.execute("DELETE FROM album_photos WHERE photo_id=?", (file_id,))
    conn.execute("DELETE FROM sem_vec WHERE id=?", (file_id,))
    conn.execute("DELETE FROM ocr_fts WHERE id=?", (file_id,))


def _unique_dst(dst: Path) -> Path:
    """Never overwrite an existing file in the target folder."""
    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    n = 1
    while True:
        cand = dst.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def _with_retry(fn, attempts: int = 3):
    """文件被缩略图/杀毒/索引器短暂占用时，重试几次再判定失败。"""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.3 * (i + 1))


# ---------------------------------------------------------------------------
# 删除：原文件直接进系统回收站，索引行同时删除
# ---------------------------------------------------------------------------


def purge(ids: list[str]) -> dict:
    """删除：原文件进系统回收站，索引行同时删除。

    不再区分默认文件夹——任何索引里的真实文件都按「进回收站」处理
    （仍可从系统回收站恢复）。文件早已不存在时只删索引。
    """
    from send2trash import send2trash

    rows = [r for r in _rows(ids) if r["purged_at"] is None]
    conn = get_conn()
    deleted = failed = 0
    errors: list[str] = []
    for r in rows:
        p = r["path"]
        try:
            if os.path.exists(p):
                _with_retry(lambda: send2trash(p))
            conn.execute("DELETE FROM photos WHERE id=?", (r["id"],))
            _drop_side_tables(conn, r["id"])
            conn.commit()
            deleted += 1
        except Exception as e:
            failed += 1
            errors.append(f"{p}: {type(e).__name__}: {e}")
    return {"ok": True, "deleted": deleted, "hidden": 0,
            "failed": failed, "errors": errors[:5],
            "default_root": str(DEFAULT_ROOT)}


# ---------------------------------------------------------------------------
# 移动：任意位置 -> 任意目标文件夹
# ---------------------------------------------------------------------------


def move(ids: list[str], target_dir: str) -> dict:
    """把任意索引图片移动到任意目标文件夹（不再限制默认文件夹）。"""
    target = Path(str(target_dir or "").strip()).expanduser()
    if not str(target):
        return {"ok": False, "reason": "请选择目标文件夹"}
    if not target.is_dir():
        return {"ok": False, "reason": f"目标文件夹不存在：{target}"}
    target = target.resolve()

    rows = _rows(ids)
    conn = get_conn()
    moved = skipped = failed = 0
    samples: list[str] = []
    errors: list[str] = []
    for r in rows:
        src = r["path"]
        if r["status"] != "ok" or not os.path.exists(src):
            failed += 1
            continue
        try:
            dst = _unique_dst(target / Path(src).name)
            if _norm(str(dst)) == _norm(src):
                skipped += 1
                continue
            _with_retry(lambda: shutil.move(src, str(dst)))
            conn.execute(
                "UPDATE photos SET path=?, source=?, status='ok' WHERE id=?",
                (str(dst), sources.source_for_path(str(dst)), r["id"]))
            moved += 1
            if len(samples) < 5:
                samples.append(str(dst))
        except Exception as e:
            failed += 1
            errors.append(f"{src}: {type(e).__name__}: {e}")
    conn.commit()
    return {"ok": True, "moved": moved, "skipped": skipped, "failed": failed,
            "sample": samples, "errors": errors[:5],
            "target": str(target), "default_root": str(DEFAULT_ROOT)}


# ---------------------------------------------------------------------------
# 整理：默认文件夹内建子目录（时间轴 / 种类 / 来源 / 主题）
# ---------------------------------------------------------------------------


def _organize_subdir(row: dict, mode: str) -> str:
    dt = datetime.fromtimestamp(row["taken_at"])
    if mode == "kind":
        return KIND_LABELS.get(row["kind"] or "other", KIND_LABELS["other"])
    if mode == "source":
        return sources.label(row.get("source"))
    if mode == "scene":
        return row.get("scene") or "其他"
    return os.path.join(dt.strftime("%Y"), dt.strftime("%Y-%m"))  # timeline


def organize(ids: list[str], mode: str = "timeline") -> dict:
    if mode not in ("timeline", "kind", "source", "scene"):
        return {"ok": False, "reason": "mode must be timeline|kind|source|scene"}
    rows = _rows(ids)
    conn = get_conn()
    moved = skipped = failed = 0
    samples: list[str] = []
    errors: list[str] = []
    for r in rows:
        src = r["path"]
        if r["status"] != "ok" or not os.path.exists(src):
            failed += 1
            continue
        if not is_default_path(src):
            skipped += 1
            continue
        try:
            sub = _organize_subdir(r, mode)
            folder = DEFAULT_ROOT / sub
            folder.mkdir(parents=True, exist_ok=True)
            dst = _unique_dst(folder / Path(src).name)
            if _norm(str(dst)) == _norm(src):
                skipped += 1
                continue
            shutil.move(src, str(dst))
            conn.execute(
                "UPDATE photos SET path=?, source=?, status='ok' WHERE id=?",
                (str(dst), sources.source_for_path(str(dst)), r["id"]))
            moved += 1
            if len(samples) < 5:
                samples.append(os.path.relpath(str(dst), str(DEFAULT_ROOT)))
        except Exception as e:
            failed += 1
            errors.append(f"{src}: {type(e).__name__}: {e}")
    conn.commit()
    return {"ok": True, "moved": moved, "skipped": skipped, "failed": failed,
            "sample": samples, "errors": errors[:5], "mode": mode,
            "default_root": str(DEFAULT_ROOT)}


# ---------------------------------------------------------------------------
# 文件夹选择器（只列目录，不暴露文件）
# ---------------------------------------------------------------------------


def list_dirs(path: str | None) -> dict:
    start = Path(str(path).strip()).expanduser() if path else DEFAULT_ROOT
    try:
        cur = start.resolve()
    except (OSError, ValueError):
        return {"ok": False, "reason": "路径无效"}
    if not cur.is_dir():
        cur = DEFAULT_ROOT
    dirs = []
    try:
        for entry in sorted(cur.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith((".", "$")):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except OSError as e:
        return {"ok": False, "reason": f"无法读取目录：{e}"}
    parent = str(cur.parent) if cur.parent != cur else None
    return {"ok": True, "path": str(cur), "parent": parent,
            "dirs": dirs, "default_root": str(DEFAULT_ROOT),
            "is_default": is_default_path(str(cur))}
