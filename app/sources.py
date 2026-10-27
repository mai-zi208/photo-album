"""图片来源 (下载来源) derived purely from the on-disk path.

图集页「来源」和选择后的「类选 → 来源」都按这个分类（显示为软件/位置名）：
微信、QQ、浏览器下载、手机相机、截图工具、桌面、文档、图片文件夹、其他。
规则优先匹配更具体的目录名
（如 Pictures/WeChat 先算微信，而不是图片），最后回落到「其他」。

source 在入库时算一次并写进 photos.source；文件被移动/整理后由调用方
重新计算，保证来源跟着真实位置走。
"""
from __future__ import annotations

SOURCE_LABELS = {
    "wechat": "微信",
    "qq": "QQ",
    "downloads": "浏览器下载",
    "dcim": "手机相机",
    "screenshots": "截图工具",
    "desktop": "桌面",
    "documents": "文档",
    "pictures": "图片文件夹",
    "other": "其他",
}

# 顺序即优先级：越具体越靠前
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("wechat", ("\\wechat\\", "\\weixin\\", "\\微信\\", "\\wechat files\\")),
    ("qq", ("\\qq\\", "\\tencent files\\", "\\qq files\\", "\\qq图片\\")),
    ("downloads", ("\\downloads\\", "\\下载\\", "\\浏览器下载\\")),
    ("screenshots", ("\\screenshots\\", "\\screenshot\\", "\\截图\\",
                     "\\屏幕截图\\", "\\屏幕快照\\")),
    ("dcim", ("\\dcim\\", "\\camera roll\\", "\\camera\\", "\\相机\\",
              "\\照片\\")),
    ("desktop", ("\\desktop\\", "\\桌面\\")),
    ("documents", ("\\documents\\", "\\文档\\")),
    ("pictures", ("\\pictures\\", "\\图片\\")),
]


def source_for_path(path: str) -> str:
    p = (path or "").replace("/", "\\").lower()
    if not p:
        return "other"
    padded = "\\" + p.strip("\\") + "\\"
    for key, tokens in _RULES:
        if any(t in padded for t in tokens):
            return key
    return "other"


def label(key: str | None) -> str:
    return SOURCE_LABELS.get(key or "other", SOURCE_LABELS["other"])
