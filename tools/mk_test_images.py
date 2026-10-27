"""Generate a synthetic test library to benchmark the first scan.

Usage:
  python tools/mk_test_images.py [count] [--dir PATH]

Creates ~count images spread over two years (random mtimes), mixed JPEG/PNG,
plus a handful of duplicate pairs and broken files.
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Chinese + latin phrases planted into text screenshots so the OCR slow
# channel has real text to recognise and /api/search something to hit.
PHRASES = [
    "合同金额总计 8600 元",
    "会议纪要 2026 第一季度",
    "请查收附件中的报价单",
    "航班 CA1836 登机口 27",
    "生日蛋糕食谱 奶油用量",
    "服务器错误 500 请重试",
    "快递单号 SF1234567890",
    "明天下午三点开会提醒",
    "房产证复印件已上传",
    "微信支付 收款到账 128.50",
    "Python 正则表达式教程",
    "体检报告 血压偏高 注意饮食",
]


def _load_font(size: int):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def text_screenshot(phrase: str, w: int = 900, h: int = 460) -> Image.Image:
    """A readable screenshot-like image with a planted phrase on light bg."""
    img = Image.new("RGB", (w, h), (248, 248, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 46], fill=(76, 110, 245))          # app bar
    d.rectangle([0, h - 40, w, h], fill=(230, 230, 234))     # status bar
    font = _load_font(40)
    small = _load_font(22)
    d.text((30, 120), phrase, fill=(20, 20, 20), font=font)
    d.text((30, 200), "—— 截图正文 ——" + phrase[:4], fill=(90, 90, 96), font=small)
    d.text((30, 300), "Ref: " + phrase[-8:], fill=(120, 120, 126), font=small)
    return img


def rand_image(w: int, h: int) -> Image.Image:
    """Noise-ish gradient image, unique content, cheap to generate."""
    img = Image.new("RGB", (min(w, 64), min(h, 64)))
    px = []
    for y in range(img.height):
        base = random.randint(0, 200)
        for x in range(img.width):
            px.append(((base + x * 4) % 256, (base * y) % 256, random.randint(0, 255)))
    img.putdata(px)
    return img.resize((w, h), Image.Resampling.BILINEAR)


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 2000
    out = Path(sys.argv[next(i for i, a in enumerate(sys.argv) if a == "--dir") + 1]) \
        if "--dir" in sys.argv else Path(__file__).resolve().parent.parent / ".bench" / "library"
    out.mkdir(parents=True, exist_ok=True)

    random.seed(42)
    now = time.time()
    two_years = 2 * 365 * 86400
    made = 0

    # text-bearing screenshots (planted phrases) so OCR + search have targets
    text_dir = out / "text_screenshots"
    text_dir.mkdir(parents=True, exist_ok=True)
    for k, phrase in enumerate(PHRASES):
        p = text_dir / f"text_{k:02d}.png"
        text_screenshot(phrase).save(p, "PNG")
        ts = now - two_years * (k / len(PHRASES))
        os.utime(p, (ts, ts))
        made += 1

    for i in range(count):
        day = random.random()
        ts = now - two_years * day
        struct = time.localtime(ts)
        year = time.strftime("%Y", struct)
        folder = out / year / time.strftime("%Y-%m", struct)
        folder.mkdir(parents=True, exist_ok=True)

        if i % 5 == 0:  # screenshots: PNG, screen-ish aspect
            w, h = (1920, 1080) if i % 10 == 0 else (1170, 2532)
            p = folder / f"screenshot_{i}.png"
            rand_image(w, h).save(p, "PNG")
        else:
            w, h = random.choice([(1600, 1200), (2048, 1536), (3024, 4032), (800, 600)])
            p = folder / f"photo_{i}.jpg"
            rand_image(w, h).save(p, "JPEG", quality=random.randint(70, 92))
        os.utime(p, (ts, ts))
        made += 1

    # duplicate pairs: identical content under different names/folders
    src = sorted(out.rglob("*.jpg"))[0]
    data = src.read_bytes()
    for k in range(20):
        dup = out / f"dup_folder_{k % 3}" / f"copy_{k}.jpg"
        dup.parent.mkdir(parents=True, exist_ok=True)
        dup.write_bytes(data)
        os.utime(dup, (now, now))
        made += 1

    # broken files (unique content each)
    for k in range(5):
        bad = out / "broken" / f"corrupt_{k}.jpg"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff\xd8\xff\xe0 garbage not a jpeg " + str(k).encode() + os.urandom(16))
        made += 1

    total_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    print(f"created {made} files ({total_mb:.0f} MB) in {out}")


if __name__ == "__main__":
    main()
