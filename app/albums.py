"""User collections (收藏集): named photo sets that live only in the database.

Deliberate design (docs/tech-stack.md 核心决策): directories stay organised by
stable dimensions (time/type), so a manual album must NOT touch files —
it is a bypass field like note, keyed by content-hash photo ids, so members
survive renames/moves and re-scans without any reconciliation.
"""
from __future__ import annotations

import time
import uuid

from .db import get_conn


def create(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "reason": "name required"}
    conn = get_conn()
    exists = conn.execute(
        "SELECT 1 FROM albums WHERE name=?", (name,)).fetchone()
    if exists:
        return {"ok": False, "reason": "name taken"}
    aid = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO albums(id, name, created_at) VALUES(?,?,?)",
        (aid, name, time.time()))
    conn.commit()
    return {"ok": True, "id": aid, "name": name}


def rename(album_id: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "reason": "name required"}
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM albums WHERE id=?", (album_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "no such album"}
    clash = conn.execute(
        "SELECT 1 FROM albums WHERE name=? AND id!=?", (name, album_id)).fetchone()
    if clash:
        return {"ok": False, "reason": "name taken"}
    conn.execute("UPDATE albums SET name=? WHERE id=?", (name, album_id))
    conn.commit()
    return {"ok": True, "id": album_id, "name": name}


def delete(album_id: str) -> dict:
    """Remove the collection only; photos are untouched."""
    conn = get_conn()
    cur = conn.execute("DELETE FROM albums WHERE id=?", (album_id,))
    conn.execute("DELETE FROM album_photos WHERE album_id=?", (album_id,))
    conn.commit()
    return {"ok": cur.rowcount > 0, "id": album_id}


def add(album_id: str, ids: list[str]) -> dict:
    conn = get_conn()
    album = conn.execute("SELECT 1 FROM albums WHERE id=?", (album_id,)).fetchone()
    if not album:
        return {"ok": False, "reason": "no such album"}
    before = conn.execute(
        "SELECT COUNT(*) c FROM album_photos WHERE album_id=?", (album_id,)).fetchone()["c"]
    conn.executemany(
        """INSERT OR IGNORE INTO album_photos(album_id, photo_id, added_at)
           SELECT ?, p.id, ? FROM photos p WHERE p.id=?""",
        [(album_id, time.time(), i) for i in set(ids)])
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) c FROM album_photos WHERE album_id=?", (album_id,)).fetchone()["c"]
    return {"ok": True, "added": after - before, "total": after}


def remove(album_id: str, ids: list[str]) -> dict:
    conn = get_conn()
    conn.executemany("DELETE FROM album_photos WHERE album_id=? AND photo_id=?",
                     [(album_id, i) for i in ids])
    conn.commit()
    return {"ok": True, "album_id": album_id}


def albums_with_stats() -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        """SELECT a.id, a.name, a.created_at,
                  (SELECT COUNT(*) FROM album_photos ap
                     JOIN photos p ON p.id=ap.photo_id
                    WHERE ap.album_id=a.id AND p.status!='missing'
                      AND p.trash_at IS NULL AND p.purged_at IS NULL) AS count,
                  (SELECT ap2.photo_id FROM album_photos ap2
                     JOIN photos p2 ON p2.id=ap2.photo_id
                    WHERE ap2.album_id=a.id AND p2.status='ok'
                      AND p2.trash_at IS NULL AND p2.purged_at IS NULL
                    ORDER BY p2.taken_at DESC LIMIT 1) AS cover
           FROM albums a ORDER BY a.created_at ASC, a.rowid""")]


def get_album(album_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT a.id, a.name, a.created_at,
                  (SELECT COUNT(*) FROM album_photos ap JOIN photos p
                     ON p.id=ap.photo_id AND p.status!='missing'
                    AND p.trash_at IS NULL AND p.purged_at IS NULL
                    WHERE ap.album_id=a.id) AS count
           FROM albums a WHERE a.id=?""", (album_id,)).fetchone()
    return dict(row) if row else None


def albums_for(photo_id: str) -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        """SELECT a.id, a.name FROM albums a
           JOIN album_photos ap ON ap.album_id=a.id
           WHERE ap.photo_id=? ORDER BY a.name""", (photo_id,)).fetchall()]
