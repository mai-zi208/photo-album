"""Video media helpers: probe + poster frame via bundled ffmpeg.

Videos never decode through Pillow; we shell out to the ffmpeg binary that
ships with the imageio-ffmpeg wheel (fully local, no system install).
When ffmpeg is unavailable every helper degrades to None and the ingest
lane registers the file as broken instead of crashing the scan.
"""
from __future__ import annotations

import io
import re
import subprocess
from PIL import Image

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_STREAM_RE = re.compile(r"Video:.*?\b(\d{2,5})x(\d{2,5})")

_EXE: str | None | bool = False  # tri-state cache: unset / path / None


def ffmpeg_exe() -> str | None:
    global _EXE
    if _EXE is False:
        try:
            import imageio_ffmpeg
            _EXE = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            _EXE = None
    return _EXE  # type: ignore[return-value]


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    flags = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0  # no console flash
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout,
                              creationflags=flags)
    except Exception:
        return None


def probe(path: str) -> dict | None:
    """{'duration': float|None, 'width': int|None, 'height': int|None}
    parsed from ffmpeg's stderr banner; None when ffmpeg can't read it."""
    exe = ffmpeg_exe()
    if not exe:
        return None
    p = _run([exe, "-hide_banner", "-i", path], timeout=20)
    if p is None:
        return None
    err = p.stderr.decode("utf-8", "replace")
    duration = width = height = None
    m = _DURATION_RE.search(err)
    if m:
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = _VIDEO_STREAM_RE.search(err)
    if m:
        width, height = int(m[1]), int(m[2])
    if duration is None and width is None:
        return None
    return {"duration": duration, "width": width, "height": height}


def poster(path: str, ts: float = 0.0) -> Image.Image | None:
    """Grab one frame as PNG through a pipe (no temp files)."""
    exe = ffmpeg_exe()
    if not exe:
        return None
    p = _run([exe, "-hide_banner", "-loglevel", "error",
              "-ss", f"{max(ts, 0.0):.3f}", "-i", path,
              "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
             timeout=60)
    if p is None or not p.stdout:
        return None
    try:
        return Image.open(io.BytesIO(p.stdout)).convert("RGB")
    except Exception:
        return None


def poster_any(path: str, duration: float | None) -> Image.Image | None:
    """A frame slightly into the clip (black first frames are common),
    falling back to the very beginning."""
    img = poster(path, min(1.0, (duration or 2.0) * 0.1))
    return img or poster(path, 0.0)
