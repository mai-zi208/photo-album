# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Windows 文件夹版（onedir）桌面程序。

在项目根目录执行：
    .venv\\Scripts\\pyinstaller.exe packaging\\photoalbum.spec --noconfirm --clean
产物：
    dist\\相册索引\\相册索引.exe   （双击即开原生窗口，不弹浏览器）

打包进去的只有程序本身：web/ 与 assets/ 作为只读资源，运行数据（索引库、
缩略图、下载的模型）落在 %LOCALAPPDATA%\\PhotoAlbum，因此不会带上开发机的
索引与照片信息。.data、.venv、tools、docs 一律排除。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "assets"), "assets"),
]
datas += collect_data_files("cv2")            # Haar 级联 xml
datas += collect_dynamic_libs("pillow_heif")  # libheif 运行时

binaries = []
hiddenimports = [
    # 应用本体：desktop.py 用字符串 "app.server:app" 交给 uvicorn，静态分析看不到
    "app", "app.server",
    # uvicorn 按字符串动态加载实现
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    # pywebview（Edge/WebView2 + pythonnet 桥）
    "webview.platforms.edgechromium", "webview.platforms.winforms",
    "clr", "clr_loader",
    # watchdog 的 Windows 观察器按平台字符串导入
    "watchdog.observers.read_directory_changes", "watchdog.observers.winapi",
]

# transformers 是懒加载、rapidocr 的模型随包分发，静态分析看不全，整体收集
for pkg in ("transformers", "rapidocr", "imageio_ffmpeg"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "torch", "tensorflow", "matplotlib", "pandas", "scipy", "IPython",
        "notebook", "pytest", "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="相册索引",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="相册索引",
)
