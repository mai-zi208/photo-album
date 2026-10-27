"""Memories engine ("精彩回忆"): turn a set of photos into a playable moment.

Two lanes, matching the agreed "instant play + optional export" design:
  tracks()      - list the offline-synthesised music loops bundled in
                  assets/music/ (see tools/gen_music.py); the frontend player
                  animates the photos with CSS and streams one of these as the
                  soundtrack. Nothing leaves the machine.
  start_export()- render a REAL mp4 in the background: each photo becomes a
                  short Ken Burns clip (zoompan), clips join with a crossfade
                  (xfade), and the chosen loop is muxed over the top. The
                  render runs on a daemon thread and reports progress, exactly
                  like rename/layout apply, so the UI just polls.

Robustness rules (fully offline, never crashes a scan/request):
  - bundled ffmpeg only (media.ffmpeg_exe); if absent, export reports an error
    instead of raising.
  - only still images feed a slideshow; videos are skipped (kind=='video').
  - sources are the 512px thumbnails already on disk: they always decode, are
    already EXIF-oriented and downscaled, so ffmpeg never chokes on heic/avif.
  - the crossfade graph is finicky; if it fails we fall back to a hard-cut
    concat (bulletproof) and still deliver a valid, music-backed video.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import db, media
from .config import DATA_DIR, RESOURCE_ROOT, thumb_path

MUSIC_DIR = RESOURCE_ROOT / "assets" / "music"
EXPORT_DIR = DATA_DIR / "memories"
AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".ogg", ".wav"}

FPS = 30
CANVAS_W, CANVAS_H = 1280, 720
# fit then pad to the canvas: no stretch, letterbox instead.
_PREP = (f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease"
         f":flags=lanczos,"
         f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")

_SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")


# ---------------------------------------------------------------------------
# soundtrack
# ---------------------------------------------------------------------------

def tracks() -> list[dict]:
    """Bundled music loops. `url` is the streaming route; a browser <audio>
    or <video> can play it directly, and export resolves the same name."""
    if not MUSIC_DIR.is_dir():
        return []
    out = []
    for p in sorted(MUSIC_DIR.iterdir()):
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        name = p.stem
        pr = media.probe(str(p)) or {}
        out.append({"name": name,
                    "url": f"/api/memories/track/{name}",
                    "duration": pr.get("duration")})
    return out


def track_file(name: str) -> Path | None:
    """Resolve a whitelisted track name to its on-disk file (no traversal)."""
    if not _SAFE.match(name or ""):
        return None
    for p in MUSIC_DIR.glob(f"{name}.*"):
        if p.suffix.lower() in AUDIO_EXTS:
            return p
    return None


# ---------------------------------------------------------------------------
# export job table
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_JOBS: dict[str, dict] = {}


def _set(job_id: str, **kw) -> None:
    with _lock:
        _JOBS.setdefault(job_id, {}).update(kw)


def status(job_id: str) -> dict | None:
    with _lock:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def file_path(job_id: str) -> Path | None:
    with _lock:
        j = _JOBS.get(job_id)
    if not j or j.get("state") != "done":
        return None
    path = Path(j["file"])
    return path if path.is_file() else None


def _sources(ids: list[str]) -> list[dict]:
    """Still-image members, newest-first, that have a readable thumbnail.
    Returns [{'id','path'}] where path is the frame ffmpeg animates."""
    if not ids:
        return []
    conn = db.get_conn()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT id, path, kind FROM photos
            WHERE id IN ({marks}) AND status='ok' AND kind!='video'
            ORDER BY taken_at DESC""",
        ids).fetchall()
    by_id = {r["id"]: r for r in rows}
    frames = []
    for fid in ids:                    # preserve the caller's order
        r = by_id.get(fid)
        if r is None:
            continue
        tp = thumb_path(fid, 512)
        if not tp.exists():
            tp = thumb_path(fid, 128)
        if tp.exists():
            frames.append({"id": fid, "path": str(tp)})
    return frames


def _run_ffmpeg(args: list[str], timeout: float) -> tuple[bool, str]:
    exe = media.ffmpeg_exe()
    if not exe:
        return False, "ffmpeg unavailable"
    flags = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0
    try:
        p = subprocess.run([exe, "-hide_banner", "-loglevel", "error", *args],
                           capture_output=True, timeout=timeout, creationflags=flags)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return False, p.stderr.decode("utf-8", "replace")[-800:]
    return True, ""


def _clip_cmd(img: str, out: str, each: float) -> list[str]:
    """One still -> one Ken Burns mp4 segment (deterministic frame count)."""
    frames = max(1, round(each * FPS))
    vf = (f"{_PREP},zoompan="
          f"z='min(zoom+0.0006,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={CANVAS_W}x{CANVAS_H}:fps={FPS},format=yuv420p")
    return ["-y", "-i", img, "-vf", vf, "-r", str(FPS), "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-an", out]


def _compose_final(clips: list[str], seg: float, xfade: float,
                   audio: str | None, total: float, out: str) -> bool:
    """Crossfade all clips, mux music, fade audio out. True on success."""
    n = len(clips)
    args: list[str] = []
    for c in clips:
        args += ["-i", c]
    if audio:
        args += ["-stream_loop", "-1", "-i", audio]
    parts: list[str] = []
    if n == 1:
        vlabel = "0:v"
    else:
        prev = "0:v"
        acc = seg                       # length composited so far
        for i in range(1, n):
            offset = acc - xfade
            nxt = f"x{i}"
            parts.append(f"[{prev}][{i}:v]xfade=transition=fade:"
                         f"duration={xfade}:offset={offset:.3f}[{nxt}]")
            prev = nxt
            acc += seg - xfade
        vlabel = prev
    filters = list(parts)
    amap = []
    if audio:
        fade_start = max(0.0, total - 1.2)
        filters.append(f"[{n}:a]afade=t=in:st=0:d=0.6,"
                       f"afade=t=out:st={fade_start:.3f}:d=1.2,aresample=44100[aout]")
        amap = ["-map", "[aout]"]
    filters_sql = ";".join(filters)
    args += ["-filter_complex", filters_sql] if filters_sql else []
    args += ["-map", f"[{vlabel}]" if vlabel != "0:v" else "0:v", *amap,
             "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "23", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-b:a", "128k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", "-y", out]
    ok, _ = _run_ffmpeg(args, timeout=600)
    return ok


def _concat_final(clips: list[str], audio: str | None, out: str) -> bool:
    """Fallback: hard-cut concat list (bulletproof), then mux music."""
    lst = Path(out).with_suffix(".txt")
    lst.write_text("".join(f"file '{os.path.abspath(c)}'\n" for c in clips),
                   encoding="utf-8")
    joined = Path(out).with_name(Path(out).stem + "_joined.mp4")
    ok, _ = _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst),
                         "-c", "copy", "-y", str(joined)], timeout=300)
    lst.unlink(missing_ok=True)
    if not ok:
        return False
    args = ["-i", str(joined)]
    if audio:
        args += ["-stream_loop", "-1", "-i", audio]
    args += ["-map", "0:v", "-c:v", "copy"]
    if audio:
        args += ["-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", "-y", out]
    ok2, _ = _run_ffmpeg(args, timeout=300)
    joined.unlink(missing_ok=True)
    return ok2


def _export_worker(job_id: str, frames: list[dict], track: str | None,
                   each: float, xfade: float) -> None:
    tmp = EXPORT_DIR / f"job_{job_id}"
    clips_dir = tmp / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    out = EXPORT_DIR / f"memory_{job_id}.mp4"
    try:
        clips = []
        for k, fr in enumerate(frames):
            _set(job_id, state="rendering", done=k, total=len(frames))
            cp = clips_dir / f"c{k}.mp4"
            ok, err = _run_ffmpeg(_clip_cmd(fr["path"], str(cp), each), timeout=120)
            if not ok or not cp.is_file():
                raise RuntimeError(f"clip {k}: {err}")
            clips.append(str(cp))
        n = len(clips)
        total = each if n == 1 else n * each - (n - 1) * xfade
        audio = None
        if track:
            tf = track_file(track)
            audio = str(tf) if tf else None
        _set(job_id, state="compositing", done=n, total=n)
        ok = _compose_final(clips, each, xfade, audio, total, str(out))
        if not ok:
            ok = _concat_final(clips, audio, str(out))
        if not ok or not out.is_file():
            raise RuntimeError("final render failed")
        pr = media.probe(str(out)) or {}
        _set(job_id, state="done", file=str(out), seconds=total,
             duration=pr.get("duration"), size=out.stat().st_size)
    except Exception as e:
        _set(job_id, state="error", error=f"{type(e).__name__}: {e}")
    finally:
        import shutil
        shutil.rmtree(clips_dir, ignore_errors=True)


class ExportError(ValueError):
    pass


def start_export(ids: list[str], track: str | None = None,
                 each: float = 3.5, xfade: float = 0.8,
                 max_photos: int = 30) -> dict:
    frames = _sources(ids)
    if not frames:
        raise ExportError("no viewable photos in this selection")
    frames = frames[:max_photos]
    each = max(1.5, min(each, 8.0))
    xfade = max(0.0, min(xfade, each - 0.5))
    if track is not None and track != "" and track_file(track) is None:
        raise ExportError("unknown track")
    job_id = uuid.uuid4().hex[:12]
    n = len(frames)
    total = each if n == 1 else n * each - (n - 1) * xfade
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    _set(job_id, state="starting", photos=n,
         track=track, done=0, total=n, seconds=round(total, 2), error=None)
    threading.Thread(target=_export_worker, daemon=True, name="memory-export",
                     args=(job_id, frames, track, each, xfade)).start()
    return {"job_id": job_id, "photos": n, "seconds": round(total, 2)}


def cleanup_old(keep_minutes: int = 180) -> int:
    """Drop render artifacts older than the window; scratch dir hygiene."""
    cutoff = time.time() - keep_minutes * 60
    removed = 0
    if EXPORT_DIR.is_dir():
        for p in EXPORT_DIR.glob("memory_*.mp4"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        for p in EXPORT_DIR.glob("job_*"):
            if p.is_dir() and p.stat().st_mtime < cutoff:
                import shutil
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    return removed
