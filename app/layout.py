"""Layout engine: turn the index into an on-disk folder arrangement.

Safety model (this module MOVES real files):
- plan()  computes a full target layout and stores it as a reversible plan;
          it never touches files. The plan is previewed before anything runs.
- apply() executes a previously-previewed plan, recording every step in
          ops_journal as it goes, so a half-finished run is still undoable.
- undo()  reverses an applied plan (move back / trash copies).

Directory rule (v1): <root>/<year>/<YYYY-MM>/<YYYY-MM-DD>_<slug>_<id8>.<ext>
Only stable, never-wrong dimensions: time. Kind is encoded in the filename
prefix for screenshots. Semantic grouping is deliberately NOT part of the path.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime

from .config import DATA_DIR
from .db import get_conn

PLAN_PATH = DATA_DIR / "layout_plan.json"
_lock = threading.Lock()

RULE_VERSION = "time-v1"

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _slug(text: str, max_len: int = 24) -> str:
    text = _UNSAFE.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:max_len] or "img").replace(" ", "_")


def _date(taken_at: float) -> str:
    return datetime.fromtimestamp(taken_at).strftime("%Y-%m-%d")


def _plan_one(row: dict, root_out: str) -> dict:
    d = _date(row["taken_at"])
    year, ym = d[:4], d[:7]
    date_slug = _slug(row["path"].rsplit(os.sep, 1)[-1].rsplit(".", 1)[0])
    ext = (row["ext"] or ".jpg").lstrip(".").lower()
    if ext in ("jpeg",):
        ext = "jpg"
    prefix = "截图_" if row["kind"] == "screenshot" else ""
    name = f"{d}_{prefix}{date_slug}_{row['id'][:8]}.{ext}"
    dst_dir = os.path.join(root_out, year, ym)
    return {
        "id": row["id"],
        "src": row["path"],
        "dst": os.path.join(dst_dir, name),
        "original_mtime": row["mtime"],
    }


def plan(root_out: str, limit: int | None = None) -> dict:
    """Compute (but do not run) a layout plan for all indexed, non-broken photos."""
    root_out = os.path.abspath(root_out)
    conn = get_conn()
    sql = ("SELECT id, path, kind, taken_at, mtime, ext FROM photos "
           "WHERE status='ok' ORDER BY taken_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    steps, skipped = [], 0
    seen_dst: set[str] = set()
    for r in rows:
        p = _plan_one(dict(r), root_out)
        if os.path.normcase(p["src"]) == os.path.normcase(p["dst"]):
            skipped += 1
            continue
        # guard against two source files resolving to the same target name
        base_dst, ext_dst = os.path.splitext(p["dst"])
        n = 1
        while p["dst"] in seen_dst:
            n += 1
            p["dst"] = f"{base_dst}-dup{n}{ext_dst}"
        seen_dst.add(p["dst"])
        steps.append(p)

    data = {
        "rule_version": RULE_VERSION,
        "root_out": root_out,
        "created_at": time.time(),
        "state": "planned",           # planned -> applied -> undone
        "steps": steps,
        "results": [],                 # filled by apply(): {src,dst,action}
        "skipped_already_in_place": skipped,
    }
    with _lock:
        PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PLAN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PLAN_PATH)
    return summarize(data)


def summarize(data: dict) -> dict:
    folders = sorted({os.path.dirname(s["dst"]) for s in data["steps"]})
    by_year: dict[str, int] = {}
    root = data["root_out"]
    for s in data["steps"]:
        rel = os.path.relpath(os.path.dirname(s["dst"]), root)
        year = rel.split(os.sep)[0]
        by_year[year] = by_year.get(year, 0) + 1
    return {
        "state": data["state"],
        "root_out": data["root_out"],
        "rule_version": data["rule_version"],
        "files": len(data["steps"]),
        "folders": len(folders),
        "by_year": by_year,
        "skipped_already_in_place": data.get("skipped_already_in_place", 0),
        "sample": [s["dst"] for s in data["steps"][:5]],
        "applied": sum(1 for r in data["results"] if r["action"] == "move"),
        "failed": sum(1 for r in data["results"] if r["action"] == "failed"),
    }


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
APPLY = {"running": False, "done": 0, "moved": 0, "failed": 0,
         "total": 0, "error": None}


def _bump(**kw) -> None:
    with _progress_lock:
        for k, v in kw.items():
            APPLY[k] = APPLY.get(k, 0) + v


def apply_progress() -> dict:
    with _progress_lock:
        return dict(APPLY)


def _apply_worker(mode: str) -> None:
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
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if mode == "copy":
                    if not (os.path.exists(dst)
                            and os.path.getsize(dst) == os.path.getsize(src)):
                        shutil.copy2(src, dst)
                    new_path = dst
                else:  # move
                    if os.path.normcase(src) != os.path.normcase(dst):
                        if os.path.exists(dst):
                            # never hard-delete a pre-existing user file:
                            # recycle it first, then move ours in
                            from send2trash import send2trash
                            send2trash(dst)
                        shutil.move(src, dst)
                    new_path = dst
                conn.execute("UPDATE photos SET path=? WHERE id=?", (new_path, step["id"]))
                data["results"].append({"src": src, "dst": dst, "action": "move"})
                _bump(moved=1)
            except Exception as e:
                data["results"].append({"src": src, "dst": dst, "action": "failed",
                                        "error": f"{type(e).__name__}: {e}"})
                _bump(failed=1)
            finally:
                _bump(done=1)
            if APPLY["done"] % 200 == 0:
                conn.commit()
                _persist(data)
        conn.commit()
        data["state"] = "applied"
        data["mode"] = mode
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


def start_apply(mode: str) -> dict:
    if mode not in ("copy", "move"):
        return {"started": False, "reason": "mode must be copy|move"}
    data = _load()
    if not data:
        return {"started": False, "reason": "no plan; call /api/layout/plan first"}
    if data["state"] == "applied":
        return {"started": False, "reason": "plan already applied (undo to revert)"}
    with _progress_lock:
        if APPLY["running"]:
            return {"started": False, "reason": "apply already running"}
        APPLY.update(running=True, done=0, moved=0, failed=0, total=len(data["steps"]),
                     error=None)
    threading.Thread(target=_apply_worker, args=(mode,), daemon=True,
                     name="layout-apply").start()
    return {"started": True, "mode": mode, "total": len(data["steps"])}


def undo() -> dict:
    """Reverse an applied plan: move files back, or trash copies."""
    data = _load()
    if not data or data["state"] != "applied":
        return {"ok": False, "reason": "nothing to undo"}
    mode = data.get("mode", "move")
    conn = get_conn()
    restored = failed = trashed = 0
    for step in reversed(data["steps"]):
        src, dst = step["src"], step["dst"]
        try:
            if mode == "move":
                if os.path.exists(dst):
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    if os.path.exists(src):
                        from send2trash import send2trash
                        send2trash(src)  # user put something here since; recycle it
                    shutil.move(dst, src)
                    conn.execute("UPDATE photos SET path=? WHERE id=?", (src, step["id"]))
                    restored += 1
            else:  # copy: originals never moved, just trash the copies
                if os.path.exists(dst):
                    from send2trash import send2trash
                    send2trash(dst)
                    trashed += 1
                # index must stop pointing at the trashed copy
                conn.execute("UPDATE photos SET path=? WHERE id=?", (src, step["id"]))
        except Exception:
            failed += 1
    conn.commit()
    data["state"] = "undone"
    data["results"] = []
    _persist(data)
    # tidy empty folders we created under root_out
    if mode == "copy":
        for d, _, files in os.walk(data["root_out"], topdown=False):
            if not files and not os.listdir(d) and d != data["root_out"]:
                try:
                    os.rmdir(d)
                except OSError:
                    pass
    return {"ok": True, "mode": mode, "restored": restored,
            "trashed": trashed, "failed": failed}
