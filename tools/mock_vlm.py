"""Tiny fake OpenAI-compatible VLM endpoint for UI/verification runs.

Usage: .venv/Scripts/python.exe -u tools/mock_vlm.py [port]
Listens on 127.0.0.1:8798 by default; returns one canned Chinese caption for
any /chat/completions request. Dev-only — never points at a real provider.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8798
REPLY = "一张日常场景的照片：桌面上摊着笔记本和杯子，窗外光线柔和。"


class H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        print(f"[mock-vlm] {self.path} {len(body)}B auth={bool(self.headers.get('Authorization'))}")
        out = json.dumps({"choices": [{"message": {"content": REPLY}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


print(f"mock vlm on 127.0.0.1:{PORT}")
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
