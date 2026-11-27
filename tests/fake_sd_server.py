"""A stand-in for stable-diffusion.cpp's sd-server, for tests and screenshots.

It accepts sd-server's command line, serves ``/v1/models`` and the OpenAI-style
``/v1/images/generations`` route, and answers with a real PNG (a smooth
gradient of the requested size). Each request body is appended to the file in
``FAKE_SD_LOG`` when set, so a test can check what Kova sent.
"""
from __future__ import annotations

import base64
import json
import math
import os
import struct
import sys
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer


def png(width: int, height: int, seed: int) -> bytes:
    hue = (seed % 360) / 360.0
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            t = (x + y) / max(1, width + height - 2)
            r = int(127 + 127 * math.sin(6.28 * (hue + t)))
            g = int(127 + 127 * math.sin(6.28 * (hue + t + 0.33)))
            b = int(127 + 127 * math.sin(6.28 * (hue + t + 0.66)))
            rows += bytes((r, g, b))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self.reply(200, {"data": [{"id": "sd-cpp-local", "object": "model"}]})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/images/generations":
            self.reply(404, {"error": "not found"})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if os.environ.get("FAKE_SD_LOG"):
            with open(os.environ["FAKE_SD_LOG"], "a", encoding="utf-8") as log:
                log.write(json.dumps({"body": body, "argv": sys.argv[1:]}) + "\n")
        width, height = (int(v) for v in str(body.get("size") or "64x64").split("x"))
        # Keep test pictures small; the size Kova asked for is still in the log.
        scale = max(1, max(width, height) // 128)
        seed = len(body.get("prompt", ""))
        image = png(width // scale, height // scale, seed)
        self.reply(200, {"created": 0, "data": [{"b64_json": base64.b64encode(image).decode()}]})


def main() -> None:
    args = sys.argv[1:]
    port = int(args[args.index("--listen-port") + 1]) if "--listen-port" in args else 1234
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
