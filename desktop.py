"""桌面启动器：本地起 FastAPI 内核，再用 pywebview 开一个原生窗口。

打包后的用户双击 exe 看到的就是这个窗口（不是浏览器页面）。窗口内部是
WebView2（Edge 内核），页面仍然由本机 127.0.0.1 上的服务提供，因此所有接口、
缩略图和原图读取逻辑与浏览器版完全一致。

要点：
- 端口随机挑一个空闲的，避免和别的程序撞 8765；
- 先用 /api/status 探活再开窗，避免用户看到白屏；
- 关窗即退出（服务线程是 daemon）；
- 打包后没有控制台，所有过程与异常都写进
  %LOCALAPPDATA%\\PhotoAlbum\\desktop.log，出问题可直接看日志。
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
TITLE = "相册索引"

DATA_DIR = (Path(os.environ.get("LOCALAPPDATA")
                 or Path.home() / "AppData" / "Local") / "PhotoAlbum")
LOG_PATH = DATA_DIR / "desktop.log"
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

try:
    logging.basicConfig(filename=str(LOG_PATH), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
except OSError:
    # 日志落不了盘也不能挡住程序启动（logging 会退回标准错误输出）
    pass
log = logging.getLogger("desktop")


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind((HOST, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _serve(port: int) -> None:
    try:
        import uvicorn
        log.info("uvicorn starting on %s:%s", HOST, port)
        uvicorn.run("app.server:app", host=HOST, port=port, log_level="warning")
        log.info("uvicorn stopped")
    except Exception:
        log.exception("uvicorn failed")
        _message("相册索引内核启动失败。\n\n详见 "
                 f"{LOG_PATH}")


def _wait_ready(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://{HOST}:{port}/api/status"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _message(text: str) -> None:
    """没有窗口时的兜底提示（打包后没有控制台可看）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)
    except Exception:
        log.info("message: %s", text)


def main() -> None:
    # 窗口程序没有控制台：sys.stdout/stderr 是 None，uvicorn 的日志格式化会直接炸
    # （DefaultFormatter 里要拿 stderr.isatty()）。这里统一指到日志文件，顺带把
    # 第三方库的原始输出也留在日志里，方便排查。
    if sys.stdout is None or sys.stderr is None:
        try:
            stream = open(LOG_PATH, "a", encoding="utf-8", errors="replace",
                          buffering=1)
        except OSError:
            stream = open(os.devnull, "w")
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    log.info("=== start === frozen=%s pid=%s", getattr(sys, "frozen", False),
             os.getpid())

    port = _free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    if not _wait_ready(port):
        log.error("server did not answer on %s", port)
        _message("相册索引内核启动失败，请重试。\n\n详见 " f"{LOG_PATH}")
        return
    log.info("server ready on %s", port)

    try:
        import webview
    except Exception:
        log.exception("webview import failed")
        _message("缺少桌面组件，无法打开程序窗口。\n\n详见 " f"{LOG_PATH}")
        return

    webview.create_window(
        TITLE,
        f"http://{HOST}:{port}/",
        width=1280, height=880, min_size=(900, 620),
        text_select=True,
    )
    try:
        log.info("opening window")
        webview.start(gui="edgechromium", private_mode=False,
                      storage_path=str(DATA_DIR / "webview"))
        log.info("window closed")
    except Exception as e:                       # WebView2 未安装等
        log.exception("webview.start failed")
        _message("无法打开程序窗口（需要 Microsoft Edge WebView2 运行时）。\n\n"
                 f"{type(e).__name__}: {e}\n\n详见 {LOG_PATH}")


if __name__ == "__main__":
    sys.exit(main())
