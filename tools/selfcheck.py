"""Self-check / benchmark harness (no browser needed).

Usage:
  .venv/Scripts/python.exe tools/selfcheck.py [scan_root]

1. Boots the FastAPI app in-process (httpx/TestClient-style via ASGI transport).
2. Starts a scan, waits for completion, reports throughput.
3. Asserts: DB rows, thumbnail files on disk, paging API, thumb API, broken handling.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, ingest  # noqa: E402
from app.config import THUMB_DIR, thumb_path  # noqa: E402


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / ".bench" / "library")
    db.init_db()
    st0 = db.stats()  # rows may predate this run; assert deltas, not absolutes
    t0 = time.time()
    result = ingest.start_scan(root)
    print("scan request:", result)
    assert result.get("started"), result
    while ingest.PROGRESS["running"]:
        time.sleep(0.5)
    p = ingest.progress_snapshot()
    wall = time.time() - t0
    print(json.dumps(p, indent=2, ensure_ascii=False))
    st = db.stats()
    print("db stats:", st)

    n_files = sum(1 for _ in ingest.iter_images(root))
    rate = n_files / wall
    print(f"throughput: {n_files} files in {wall:.1f}s = {rate:.1f} files/s"
          f"  (30k extrapolation: {30000 / rate / 60:.1f} min)")

    problems = []
    if p["error"]:
        problems.append(f"scan error: {p['error']}")
    if p["seen"] != n_files:
        problems.append(f"seen {p['seen']} != files on disk {n_files}")
    if p["broken"] == 0 and st["broken"] == 0:
        problems.append("expected some broken files in the bench library")
    if st["total"] - st0["total"] != p["indexed"] + (st["broken"] - st0["broken"]):
        problems.append(
            f"db rows +{st['total'] - st0['total']} != indexed {p['indexed']} "
            f"+ broken +{st['broken'] - st0['broken']}")
    if st["hashed"] < st["total"] - st["broken"]:
        problems.append(f"phash missing: hashed {st['hashed']} of "
                        f"{st['total'] - st['broken']} ok rows")
    # thumbnails exist for a sample of ok rows
    conn = db.get_conn()
    sample = conn.execute(
        "SELECT id FROM photos WHERE status='ok' ORDER BY random() LIMIT 20"
    ).fetchall()
    missing = [r["id"] for r in sample if not thumb_path(r["id"], 128).exists()]
    if missing:
        problems.append(f"missing thumbs for {len(missing)} sampled ids")
    thumb_mb = sum(f.stat().st_size for f in THUMB_DIR.rglob("*.webp")) / 1e6
    print(f"thumbnails: {len(list(THUMB_DIR.rglob('*.webp')))} files, {thumb_mb:.1f} MB")

    # API-level checks via the ASGI app (no socket needed)
    from fastapi.testclient import TestClient
    from app.server import app as fastapi_app
    client = TestClient(fastapi_app)
    page1 = client.get("/api/photos", params={"limit": 100}).json()
    assert len(page1["items"]) == 100, len(page1["items"])
    c = page1["next_cursor"]
    page2 = client.get("/api/photos", params={
        "limit": 100, "taken_at": c["taken_at"], "id_cursor": c["id"]}).json()
    ids1 = {i["id"] for i in page1["items"]}
    overlap = ids1 & {i["id"] for i in page2["items"]}
    if overlap:
        problems.append(f"paging overlap: {len(overlap)} ids")
    some_id = next(i["id"] for i in page1["items"] if i["status"] == "ok")
    r = client.get(f"/thumb/{some_id}.webp", params={"size": 128})
    if r.status_code != 200 or r.headers["content-type"] != "image/webp":
        problems.append(f"thumb api bad: {r.status_code} {r.headers.get('content-type')}")
    s = client.get("/api/status").json()
    print("status api ok, kinds:", s["db"]["kinds"])
    # second scan of same root: everything should dedup to 'existing'
    while ingest.PROGRESS["running"]:
        time.sleep(0.2)
    r2 = ingest.start_scan(root)
    while ingest.PROGRESS["running"]:
        time.sleep(0.2)
    p2 = ingest.progress_snapshot()
    print(f"rescan: seen={p2['seen']} duplicated={p2['duplicated']} indexed={p2['indexed']}")
    if p2["indexed"] != 0:
        problems.append("rescan inserted new rows; content dedup broken")

    if problems:
        print("\nFAILURES:")
        for x in problems:
            print(" -", x)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
