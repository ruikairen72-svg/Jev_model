#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用的假上游：记录收到的 model，再回一段 SSE。"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.jsonl")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        with open(OUT, "a") as fh:
            fh.write(json.dumps({
                "path": self.path,
                "model": body.get("model"),
                "reasoning": body.get("reasoning"),
                "auth_seen": bool(self.headers.get("Authorization")),
                "bytes": len(raw),
            }, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in (b'data: {"type":"response.created"}\n\n',
                      b'data: {"type":"response.completed"}\n\n'):
            self.wfile.write(frame)
            self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
