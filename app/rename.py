"""Content-rename engine: give messy files a name derived from what they show.

Name source priority (the agreed content chain):
    note (the users own words win outright)
  > caption (cloud VLM description, only if the user enabled it per-item)
  > OCR first line (local: screenshots/memes/contracts)
  > type + date fallback ("截图_2026-03-05", opt-in; off by default so the
    engine stays honest - it only renames what it actually understands)

Safety model is inherited from layout.py: plan (touch nothing) -> preview ->
apply (journal each step) -> undo (reverse journal). Files are renamed IN
PLACE (same directory); the BLAKE3 row id never changes, so notes/OCR/vectors
all survive a rename untouched.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime

from .config import DATA_DIR
from .db import get_conn

PLAN_PATH = DATA_DIR / "rename_plan.json"
_lock = threading.Lock()

RULE_VERSION = "content-v1"
STEM_MAX = 24
# keep CJK, letters, digits, spaces; drop emoji/brackets/punctuation noise
_KEEPER = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff ]+")
_MEANINGFUL = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
# all-digits, or letter-led like wx_image_0001 / photo_979: ends in digits
_ROBOTISH = re.compile(r"^(?:[0-9]+|[a-z][a-z_0-9]*[0-9][_0-9]*)$", re.IGNORECASE)

TYPE_PREFIX = {"screenshot": "截图_", "video": "视频_", "other": "动图_"}


def _clean_stem(text: str) -> str:
    text = _KEEPER.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:STEM_MAX].strip().rstrip(". ")   # windows hates trailing dot
    return text


def _first_line(raw: str) -> str:
    for line in (raw or "").splitlines():
        line = line.strip()
        if len(line) >= 2:      # single glyphs are usually noise
            return line
    return ""


def _already_named(stem: str) -> bool:
    """Camera/bot names (IMG_20240102, 1719000000, wx_image...) are noise;
    anything with real characters and some length is user intent - keep it."""
    if len(stem) < 4 or not _MEANINGFUL.search(stem):
        return False
    return not _ROBOTISH.match(stem.replace(' ', '_'))


def _content_stem(row: dict, ocr_raw: str | None) -> tuple[str, str]:
    """Returns (stem, source) where source in note|caption|ocr|time."""
    if (row.get("note") or "").strip():
        stem = _clean_stem(row["note"])
        if stem:
            return stem, "note"
    if (row.get("caption") or "").strip():
        stem = _clean_stem(row["caption"])
        if stem:
            return stem, "caption"
    stem = _clean_stem(_first_line(ocr_raw))
    if stem:
        return stem, "ocr"
    return "", "time"


def _fallback_stem(row: dict) -> str:
    d = datetime.fromtimestamp(row["taken_at"]).strftime("%Y-%m-%d")
    prefix = TYPE_PREFIX.get(row["kind"], "照片_")
    return f"{prefix}{d}_{row['id'][:8]}"


def plan(with_fallback: bool = False, limit: int | None = None) -> dict:
    """Compute (but do not run) a rename plan. Touches nothing on disk."""
    conn = get_conn()
    sql = """SELECT p.id, p.path, p.kind, p.ext, p.taken_at, p.note, p.caption,
                    o.raw AS ocr_raw
             FROM photos p LEFT JOIN ocr_fts o ON o.id = p.id
             WHERE p.status='ok' ORDER BY p.taken_at DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    steps, skipped_named, skipped_same = [], 0, 0
    seen_dst: set[str] = set()
    for r in rows:
        row = dict(r)
        path = row["path"]
        directory, name = os.path.split(path)
        old_stem, ext = os.path.splitext(name)
        stem, source = _content_stem(row, row.get("ocr_raw"))
        if _already_named(old_stem):
            skipped_named += 1   # whatever we might say, it has a keeper name
            continue
        if source == "time":
            if not with_fallback:
                continue         # nothing honest to say about this file yet
            stem = _fallback_stem(row)
        if not stem:
            continue
        ext = ext.lower() or ".jpg"
        dst = os.path.join(directory, stem + ext)
        if os.path.normcase(dst) == os.path.normcase(path):
            skipped_same += 1
            continue
        n = 1
        base, e = os.path.splitext(dst)
        while dst in seen_dst:
            n += 1
            dst = f"{base}-{n}{e}"
        seen_dst.add(dst)
        steps.append({"id": row["id"], "src": path, "dst": dst,
                      "source": source})

    data = {
        "rule_version": RULE_VERSION,
        "with_fallback": with_fallback,
        "created_at": time.time(),
        "state": "planned",            # planned -> applied -> undone
        "steps": steps,
        "results": [],
        "skipped_already_named": skipped_named,
        "skipped_same_name": skipped_same,
    }
    with _lock:
        PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PLAN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PLAN_PATH)
    return summarize(data)


def summarize(data: dict) -> dict:
    by_source: dict[str, int] = {}
    for s in data["steps"]:
        by_source[s["source"]] = by_source.get(s["source"], 0) + 1
    return {
        "state": data["state"],
        "rule_version": data["rule_version"],
        "with_fallback": data.get("with_fallback", False),
        "files": len(data["steps"]),
        "by_source": by_source,
        "skipped_already_named": data.get("skipped_already_named", 0),
        "skipped_same_name": data.get("skipped_same_name", 0),
        "sample": [{"id": s["id"], "src": os.path.basename(s["src"]),
                    "dst": os.path.basename(s["dst"]), "source": s["source"]}
                   for s in data["steps"][:200]],
        "applied": sum(1 for r in data["results"] if r["action"] == "rename"),
        "failed": sum(1 for r in data["results"] if r["action"] == "failed"),
    }


def preview(limit: int = 200) -> dict:
    """Old->new list for the confirm dialog (plan must exist)."""
    data = _load()
    if not data:
        return {"steps": [], "files": 0, "state": None, "by_source": {}}
    done = {r["dst"] for r in data["results"] if r["action"] == "rename"}
    steps = [{"id": s["id"], "src": os.path.basename(s["src"]),
              "dst": os.path.basename(s["dst"]), "source": s["source"],
              "done": s["dst"] in done}
             for s in data["steps"][:limit]]
    return {"steps": steps, "files": len(data["steps"]),
            "state": data["state"], "by_source": summarize(data)["by_source"]}


def _load() -> dict | None:
    if not PLAN_PATH.exists():
        return None
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def current() -> dict | None:
    data = _load()
    return summarize(data) if data else None


# ---------------------------------------------------------------------------
# apply / undo
# ---------------------------------------------------------------------------

_progress_lock = threading.Lock()
APPLY = {"running": False, "done": 0, "renamed": 0, "failed": 0,
         "total": 0, "error": None}


def _bump(**kw) -> None:
    with _progress_lock:
        for k, v in kw.items():
            APPLY[k] = APPLY.get(k, 0) + v


def apply_progress() -> dict:
    with _progress_lock:
        return dict(APPLY)


def _apply_worker() -> None:
    conn = get_conn()
    try:
        data = _load()
        with _progress_lock:
            APPLY["total"] = len(data["steps"])
        for step in data["steps"]:
            src, dst = step["src"], step["dst"]
            try:
                if not os.path.exists(src):
                    raise FileNotFoundError(src)
                if os.path.exists(dst):
                    # never hard-delete a pre-existing user file: recycle first
                    from send2trash import send2trash
                    send2trash(dst)
                os.rename(src, dst)
                conn.execute("UPDATE photos SET path=? WHERE id=?",
                             (dst, step["id"]))
                data["results"].append({"src": src, "dst": dst,
                                        "action": "rename"})
                _bump(renamed=1)
            except Exception as e:
                data["results"].append({"src": src, "dst": dst,
                                        "action": "failed",
                                        "error": f"{type(e).__name__}: {e}"})
                _bump(failed=1)
            finally:
                _bump(done=1)
            if APPLY["done"] % 200 == 0:
                conn.commit()
                _persist(data)
        conn.commit()
        data["state"] = "applied"
        _persist(data)
    except Exception as e:
        with _progress_lock:
            APPLY["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _progress_lock:
            APPLY["running"] = False


def _persist(data: dict) -> None:
    tmp = PLAN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PLAN_PATH)


def start_apply() -> dict:
    data = _load()
    if not data:
        return {"started": False, "reason": "no plan; call /api/rename/plan first"}
    if data["state"] == "applied":
        return {"started": False, "reason": "plan already applied (undo to revert)"}
    with _progress_lock:
        if APPLY["running"]:
            return {"started": False, "reason": "apply already running"}
        APPLY.update(running=True, done=0, renamed=0, failed=0,
                     total=len(data["steps"]), error=None)
    threading.Thread(target=_apply_worker, daemon=True,
                     name="rename-apply").start()
    return {"started": True, "total": len(data["steps"])}


def undo() -> dict:
    """Reverse an applied plan: every renamed file goes back to its old name."""
    data = _load()
    if not data or data["state"] != "applied":
        return {"ok": False, "reason": "nothing to undo"}
    by_dst = {s["dst"]: s["id"] for s in data["steps"]}
    conn = get_conn()
    restored = failed = 0
    for r in reversed(data["results"]):
        if r["action"] != "rename":
            continue
        src, dst = r["src"], r["dst"]
        try:
            if os.path.exists(dst):
                if os.path.exists(src):
                    from send2trash import send2trash
                    send2trash(src)   # something appeared at the old name since
                os.rename(dst, src)
                fid = by_dst.get(dst)
                if fid:
                    conn.execute("UPDATE photos SET path=? WHERE id=?",
                                 (src, fid))
                restored += 1
        except Exception:
            failed += 1
    conn.commit()
    data["state"] = "undone"
    data["results"] = []
    _persist(data)
    return {"ok": True, "restored": restored, "failed": failed}
