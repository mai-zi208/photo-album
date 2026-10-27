"""Perceptual-hash dedup: DCT pHash (64-bit) + batched Hamming grouping.

pHash is computed in the same decode pass as the thumbnail (near-zero extra
cost for new scans); a background backfill covers rows indexed before this
existed. Grouping is global: numpy batched Hamming distances + union-find,
threshold 8/64 bits — robust to resize, recompress and mild edits, strict
enough that different photos don't merge.
"""
from __future__ import annotations

import os
import threading
import time

import numpy as np
from PIL import Image

from .db import get_conn

THRESHOLD = 8          # max differing bits out of 64
HASH_SIZE = 8
BIG = 32               # DCT working resolution

_progress_lock = threading.Lock()
PROGRESS = {
    "running": False,
    "backfilled": 0,
    "failed": 0,
    "groups": 0,
    "dupes": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
}

# ---------------------------------------------------------------------------
# hash computation
# ---------------------------------------------------------------------------

_DCT_CACHE: dict[int, np.ndarray] = {}


def _dct_matrix(n: int) -> np.ndarray:
    if n not in _DCT_CACHE:
        k = np.arange(n)[:, None]
        i = np.arange(n)[None, :]
        m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
        m *= np.sqrt(1.0 / n)
        m[1:] *= np.sqrt(2.0 / n)
        _DCT_CACHE[n] = m.astype(np.float32)
    return _DCT_CACHE[n]


def phash_from_pil(img: Image.Image) -> int | None:
    """64-bit DCT perceptual hash from an already-decoded PIL image."""
    try:
        g = img.convert("L").resize((BIG, BIG), Image.Resampling.BILINEAR)
        a = np.asarray(g, dtype=np.float32)
        C = _dct_matrix(BIG)
        dct = C @ a @ C.T
        low = dct[:HASH_SIZE, :HASH_SIZE].flatten()
        med = np.median(low[1:])          # ignore DC (overall brightness)
        bits = (low > med).astype(np.uint8)
        v = 0
        for b in bits:
            v = (v << 1) | int(b)
        return v
    except Exception:
        return None


def to_signed(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


def from_signed(v: int) -> int:
    return v & ((1 << 64) - 1)


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


def regroup(threshold: int = THRESHOLD) -> dict:
    """Recompute dup_group for every hashed photo. Returns summary."""
    try:
        return _regroup_inner(threshold)
    except Exception:
        get_conn().rollback()   # never strand a write txn (would lock out others)
        raise


def _regroup_inner(threshold: int) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, phash FROM photos WHERE phash IS NOT NULL AND status='ok'"
    ).fetchall()
    n = len(rows)
    conn.execute("UPDATE photos SET dup_group=NULL WHERE dup_group IS NOT NULL")
    if n < 2:
        conn.commit()
        return {"hashes": n, "groups": 0, "dupes": 0}

    ids = [r["id"] for r in rows]
    mat = np.fromiter(
        (from_signed(r["phash"]) for r in rows), dtype=np.uint64, count=n
    ).view(np.uint8).reshape(n, 8)

    # popcount lookup table: uint8 -> bit count (unpackbits flattens 1-D input)
    pop = np.unpackbits(np.arange(256, dtype=np.uint8)).reshape(256, 8).sum(1)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    CHUNK = 256
    for s in range(0, n, CHUNK):
        e = min(s + CHUNK, n)
        xor = mat[s:e, None, :] ^ mat[None, :, :]          # (c, n, 8)
        dist = pop[xor].sum(axis=2, dtype=np.int16)        # (c, n)
        gi = np.arange(s, e)[:, None]                      # global row index
        gj = np.arange(n)[None, :]
        dist[gi >= gj] = 99                                # upper triangle only
        ii, jj = np.where(dist <= threshold)
        for i, j in zip(ii, jj):
            union(s + int(i), int(j))

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        root = find(idx)
        if root != idx:
            groups.setdefault(root, []).append(idx)
    made = 0
    for root, members in groups.items():
        gid = ids[root]
        conn.execute(
            "UPDATE photos SET dup_group=? WHERE id=? OR id IN (%s)"
            % ",".join("?" * len(members)),
            (gid, ids[root], *([ids[m] for m in members])),
        )
        made += 1
    conn.commit()
    dupes = sum(len(m) + 1 for m in groups.values())
    return {"hashes": n, "groups": made, "dupes": dupes}


# ---------------------------------------------------------------------------
# backfill worker (compute phash for rows that predate the feature)
# ---------------------------------------------------------------------------


def _backfill_worker() -> None:
    conn = get_conn()
    try:
        todo = conn.execute(
            "SELECT id, path FROM photos WHERE phash IS NULL AND status='ok'"
            " AND kind!='video'"
        ).fetchall()
        with _progress_lock:
            PROGRESS["total"] = len(todo)
        for r in todo:
            try:
                with Image.open(r["path"]) as img:
                    img.draft("L", (BIG * 4, BIG * 4))
                    h = phash_from_pil(img)
                if h is None:
                    raise ValueError("no hash")
                conn.execute("UPDATE photos SET phash=? WHERE id=?", (to_signed(h), r["id"]))
                with _progress_lock:
                    PROGRESS["backfilled"] += 1
            except Exception:
                conn.execute(
                    "UPDATE photos SET phash=NULL, status='broken' WHERE id=?", (r["id"],))
                with _progress_lock:
                    PROGRESS["failed"] += 1
            if PROGRESS["backfilled"] % 300 == 0:
                conn.commit()
        conn.commit()
        summary = regroup()
        with _progress_lock:
            PROGRESS["groups"] = summary["groups"]
            PROGRESS["dupes"] = summary["dupes"]
    except Exception as e:
        with _progress_lock:
            PROGRESS["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _progress_lock:
            PROGRESS["running"] = False
            PROGRESS["finished_at"] = time.time()


def ensure_phash_schema() -> None:
    conn = get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    if "phash" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN phash INTEGER")
    if "dup_group" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN dup_group TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_dup ON photos(dup_group)")
    conn.commit()


def start_backfill() -> dict:
    ensure_phash_schema()
    conn = get_conn()
    missing = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE phash IS NULL AND status='ok'"
    ).fetchone()["c"]
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "backfill already running"}
        PROGRESS.update(running=True, backfilled=0, failed=0, groups=0, dupes=0,
                        total=missing, started_at=time.time(), finished_at=None,
                        error=None)
    if missing == 0:
        try:
            summary = regroup()
            with _progress_lock:
                PROGRESS.update(running=False, groups=summary["groups"],
                                dupes=summary["dupes"], finished_at=time.time())
            return {"started": False, "reason": "nothing missing; regrouped", **summary}
        except Exception as e:
            with _progress_lock:
                PROGRESS.update(running=False, error=f"{type(e).__name__}: {e}",
                                finished_at=time.time())
            raise
    threading.Thread(target=_backfill_worker, daemon=True, name="phash-backfill").start()
    return {"started": True, "missing": missing}


def progress_snapshot() -> dict:
    with _progress_lock:
        snap = dict(PROGRESS)
    return snap
