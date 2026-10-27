r"""Synthesise the offline memory soundtrack loops into assets/music/.

Pure numpy synthesis (plucked-string partials + arp), encoded with the ffmpeg
bundled in imageio-ffmpeg. Run once:
    .venv\Scripts\python.exe tools\gen_music.py
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, r"F:\Code\photo-album")
ROOT = Path(r"F:\Code\photo-album")
OUT = ROOT / "assets" / "music"
OUT.mkdir(parents=True, exist_ok=True)

SR = 44100


def freq(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def note(midi: float, dur: float, amp: float = 0.5) -> np.ndarray:
    n = max(1, int(SR * dur))
    t = np.arange(n) / SR
    f = freq(midi)
    body = np.zeros(n)
    for k, w in ((1, 1.0), (2, 0.42), (3, 0.16), (4, 0.06)):
        body += w * np.sin(2 * np.pi * f * k * t)
    env = np.exp(-t * 2.1) * (1 - np.exp(-t * 160))
    return (body * env * amp).astype(np.float32)


def place(buf: np.ndarray, start: float, sig: np.ndarray):
    i = int(start * SR)
    j = min(len(buf), i + len(sig))
    if j > i:
        buf[i:j] += sig[: j - i]


def render(name: str, chords, beat: float, pattern, tail: float) -> Path:
    bar = beat * 4
    total = bar * len(chords) + tail
    buf = np.zeros(int(SR * total), dtype=np.float32)
    for ci, chord in enumerate(chords):
        for step, semi in pattern:
            when = ci * bar + step * beat
            sig = note(chord + semi, total - when, 0.34)
            place(buf, when, sig)
            place(buf, when + 0.021, note(chord + semi - 12, total - when, 0.16))
    # decoupled comb of delayed taps: air without mud
    rev = np.zeros_like(buf)
    d = int(0.037 * SR)
    tap = buf
    for g in (0.42, 0.30, 0.20, 0.12):
        tap = np.concatenate([np.zeros(d, np.float32), tap])[: len(buf)] * (g / 0.42)
        rev += tap
    mix = np.tanh(buf + rev * 0.5) * 0.72
    fade = min(int(1.5 * SR), len(mix))
    mix[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    mix[: int(0.005 * SR)] *= np.linspace(0, 1, int(0.005 * SR))
    pcm = (mix * 32767).astype("<i2").tobytes()
    hdr = (b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVEfmt "
           + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
           + (1).to_bytes(2, "little") + (SR).to_bytes(4, "little")
           + (SR * 2).to_bytes(4, "little") + (2).to_bytes(2, "little")
           + (16).to_bytes(2, "little") + b"data" + (len(pcm)).to_bytes(4, "little"))
    wav = OUT / f"{name}.wav"
    wav.write_bytes(hdr + pcm)
    from imageio_ffmpeg import get_ffmpeg_exe
    m4a = OUT / f"{name}.m4a"
    subprocess.run([get_ffmpeg_exe(), "-y", "-loglevel", "error",
                    "-i", str(wav), "-c:a", "aac", "-b:a", "96k", str(m4a)],
                   check=True)
    wav.unlink()
    print(f"{m4a.name}: {total:.1f}s, {m4a.stat().st_size / 1e6:.2f} MB")
    return m4a


ARP8 = [(i / 2, s) for i, s in enumerate([0, 12, 19, 24, 31, 24, 19, 12])]
render("warm", [60, 55, 57, 53], 0.62, ARP8, 4.0)     # C G Am F
render("bright", [57, 53, 60, 55], 0.46, ARP8, 3.0)   # Am F C G, quicker
render("night", [50, 45, 41, 48], 0.80,               # Dm Bb F G, low & slow
       [(i / 2, s) for i, s in enumerate([0, 12, 19, 27, 19, 12])] + [(1.75, 31)],
       5.0)
print("done")
