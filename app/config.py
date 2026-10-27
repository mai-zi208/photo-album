"""Local storage paths and constants for the photo-album MVP core."""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 打包成 exe 后：只读资源（web/、assets/）随程序走，运行数据（索引库、缩略图、
# 模型）写到用户目录 %LOCALAPPDATA%\PhotoAlbum —— 程序目录可以只读，重装/升级
# 也不会丢数据，更不会把开发机的索引和照片信息打包发给别人。
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA")
                    or Path.home() / "AppData" / "Local") / "PhotoAlbum"
else:
    RESOURCE_ROOT = PROJECT_ROOT
    DATA_DIR = PROJECT_ROOT / ".data"
DB_PATH = DATA_DIR / "photoalbum.db"
THUMB_DIR = DATA_DIR / "thumbs"
SHARE_DIR = DATA_DIR / "share"

THUMB_SIZES = (512, 128)

# Chinese-CLIP ViT-B/16 ONNX (dual tower, int8) — see tools/semantic_spike.py
SEM_MODEL_DIR = DATA_DIR / "models" / "chinese_clip_onnx"

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".avif",
}

# Videos ride the same index (BLAKE3 id + poster thumbnail) but skip every
# image-only slow lane; playback goes through GET /media/{id} (HTTP Range).
# 注意：不收录 .ts —— 它同时也是 TypeScript 源码后缀（桌面/文档目录里常见），
# 误当视频会变成一堆读不出来的「坏图」。
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# 不索引的目录（大小写不敏感，按路径段匹配）：
# - 系统回收站
# - 微信/QQ 的表情包、表情图标、头像/推荐表情资源
# 这些目录动辄上千张、无浏览价值，检索时直接跳过。
IGNORED_SEGMENTS = {
    "$recycle.bin", "$recover.bin",
    "customemotion", "emotionicon", "emoji", "emoji-resource",
    "recommendface", "lnncustomface", "pokeface", "sticker", "表情",
}


def is_ignored_path(path: str) -> bool:
    parts = [p.casefold() for p in Path(path).parts]
    return any(p in IGNORED_SEGMENTS for p in parts)


def is_ignored_dir_name(name: str) -> bool:
    return name.casefold() in IGNORED_SEGMENTS


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)


# Windows 会把「图片/桌面/下载/文档」重定向到别的盘（例如 D:\图片）。
# 以注册表里的 User Shell Folders 为准，取不到再退回 %USERPROFILE%。
_SHELL_NAMES = {
    "pictures": "My Pictures",
    "desktop": "Desktop",
    "documents": "Personal",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}


def known_folders() -> dict[str, str]:
    """系统真实文件夹（已展开环境变量），只返回存在的那几个。"""
    try:
        import winreg
    except ImportError:
        return {}
    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            for field, reg_name in _SHELL_NAMES.items():
                try:
                    val, _ = winreg.QueryValueEx(k, reg_name)
                except OSError:
                    continue
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    out[field] = str(p)
    except OSError:
        return {}
    return out


def thumb_path(file_id: str, size: int) -> Path:
    return THUMB_DIR / file_id[:2] / f"{file_id}.w{size}.webp"
