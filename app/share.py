"""Share pack: build a self-contained single-file HTML from a set of photo ids.

Design (product rule: 分享包 = 可离线打开、可直接发给别人的最小投影):
- one HTML file, no external assets, no network — images are 512px WebP
  thumbnails embedded as base64 data URLs;
- privacy by omission: the pack carries ONLY the thumbnail bytes, the taken
  date and the kind label (照片/截图/其他). It never includes the original
  file, the on-disk path, OCR text, user notes or cloud captions — so a pack
  can be forwarded without leaking anything the index happens to know;
- newest-first day grouping + click-to-zoom lightbox, mirroring the main app.

The output is written under .data/share/ and served read-only at /share/<name>.
The visual shell lives in web/share_template.html (a __DATA__ JSON placeholder).
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .config import RESOURCE_ROOT, SHARE_DIR, thumb_path
from .db import get_conn

MAX_ITEMS = 2000                 # hard cap on images per pack
MAX_ZIP_ITEMS = 500              # 图片包带上原图，数量/体积都要收着点
MAX_ZIP_BYTES = 4 * 1024 ** 3
TEMPLATE_PATH = RESOURCE_ROOT / "web" / "share_template.html"
KIND_LABEL = {"photo": "照片", "screenshot": "截图", "other": "其他"}

_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def build(ids: list[str], title: str | None = None) -> dict:
    """Assemble the pack for the given ids. Returns {ok, ...}."""
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and len(i) == 64]
    if not ids:
        return {"ok": False, "reason": "没有可打包的图片"}
    if len(ids) > MAX_ITEMS:
        return {"ok": False, "reason": f"单次最多 {MAX_ITEMS} 张（当前 {len(ids)}）"}

    conn = get_conn()
    items, skipped = [], 0
    for fid in ids:
        row = conn.execute(
            "SELECT id, taken_at, kind, status FROM photos WHERE id=?",
            (fid,)).fetchone()
        if row is None or row["status"] != "ok":
            skipped += 1
            continue
        tp = thumb_path(fid, 512)
        if not tp.exists():
            tp = thumb_path(fid, 128)
            if not tp.exists():
                skipped += 1
                continue
        data_url = "data:image/webp;base64," + base64.b64encode(
            tp.read_bytes()).decode()
        items.append({
            "id": fid, "src": data_url, "ts": row["taken_at"],
            "kind": KIND_LABEL.get(row["kind"], row["kind"]),
        })
    if not items:
        return {"ok": False, "reason": "选中的图片都没有可用缩略图"}
    items.sort(key=lambda i: -i["ts"])

    payload = {
        "title": (title or "图片分享").strip()[:80] or "图片分享",
        "made": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
        "items": items,
    }
    html = _render(payload)
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"s{int(time.time())}-{secrets.token_hex(4)}.html"
    (SHARE_DIR / name).write_text(html, encoding="utf-8")
    size = (SHARE_DIR / name).stat().st_size
    return {"ok": True, "name": name, "url": f"/share/{name}",
            "count": len(items), "skipped": skipped, "bytes": size}


def _render(payload: dict) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    # embed as JSON inside a <script type=application/json>; guard breakout
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (template
            .replace("__TITLE__", _esc(payload["title"]))
            .replace("__DATA__", blob))


# ---------------------------------------------------------------------------
# 图片包：zip（只放原图，资源管理器预览栏即可查看）
# ---------------------------------------------------------------------------


def _safe_name(path: Path) -> str:
    stem = _BAD_NAME.sub("_", path.stem).strip(" ._") or "image"
    suffix = _BAD_NAME.sub("", path.suffix).lower()
    return stem[:60] + suffix


def build_zip(ids: list[str], title: str | None = None,
              dest: str | None = None) -> dict:
    """把选中的原图打包成 zip，只含图片文件（不再生成 index.html）。

    dest 为空时落在 .data/share/（站内可直接下载）；用户选了目录则写到该目录，
    只返回本地 path（不再给 /share/ URL）。"""
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and len(i) == 64]
    if not ids:
        return {"ok": False, "reason": "没有可打包的图片"}
    conn = get_conn()
    picked, skipped, total = [], 0, 0
    for fid in ids:
        row = conn.execute(
            "SELECT id, taken_at, kind, status, path FROM photos WHERE id=?",
            (fid,)).fetchone()
        if row is None or row["status"] != "ok":
            skipped += 1
            continue
        p = Path(row["path"])
        try:
            if not p.is_file():
                skipped += 1
                continue
            size = p.stat().st_size
        except OSError:
            skipped += 1
            continue
        if len(picked) >= MAX_ZIP_ITEMS or total + size > MAX_ZIP_BYTES:
            skipped += 1
            continue
        total += size
        picked.append({"id": fid, "path": p, "ts": row["taken_at"],
                       "kind": KIND_LABEL.get(row["kind"] or "", "其他")})
    if not picked:
        return {"ok": False, "reason": "选中的图片都没有可打包的原文件"}

    picked.sort(key=lambda i: -i["ts"])
    used: set[str] = set()
    for it in picked:
        dt = datetime.fromtimestamp(it["ts"])
        base = f'{dt.strftime("%Y%m%d-%H%M")}_{_safe_name(it["path"])}'
        name = base
        n = 1
        while name.lower() in used:
            name = f"{Path(base).stem} ({n}){Path(base).suffix}"
            n += 1
        used.add(name.lower())
        it["arc"] = name

    if dest:
        try:
            target = Path(dest).expanduser().resolve()
        except (OSError, ValueError):
            return {"ok": False, "reason": "导出目录无效"}
        if not target.is_dir():
            return {"ok": False, "reason": "导出目录不存在"}
    else:
        target = SHARE_DIR
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"p{int(time.time())}-{secrets.token_hex(4)}.zip"
    out = target / name
    for attempt in range(3):
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for it in picked:
                    zf.write(it["path"], it["arc"])
            break
        except PermissionError as e:
            # 目标目录被占用/杀毒扫描时重试，仍失败则明确告知目录不可写
            if attempt == 2:
                return {"ok": False, "reason": f"目标文件夹不可写：{e}"}
            time.sleep(0.3 * (attempt + 1))
        except OSError as e:
            return {"ok": False, "reason": f"写入失败：{e}"}
    res = {"ok": True, "name": name, "path": str(out),
           "count": len(picked), "skipped": skipped,
           "bytes": out.stat().st_size}
    if not dest:
        res["url"] = f"/share/{name}"
    return res
