"""Minimal dependency-free WebSocket upstream for the image smoke test."""

from __future__ import annotations

import base64
import hashlib
import json
import socketserver
import struct
import time
from http import HTTPStatus
from typing import Final


# The proxy runs in Docker and reaches the runner through
# host.docker.internal, so the fixture must listen on the runner's bridge
# interface as well as loopback.
HOST: Final = "0.0.0.0"
PORT: Final = 18765
EXPECTED_AUTH: Final = "Bearer provider-test-key"
WEBSOCKET_GUID: Final = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _text_frame(payload: str) -> bytes:
    encoded = payload.encode("utf-8")
    length = len(encoded)
    if length < 126:
        header = bytes((0x81, length))
    elif length <= 0xFFFF:
        header = bytes((0x81, 126)) + struct.pack("!H", length)
    else:
        header = bytes((0x81, 127)) + struct.pack("!Q", length)
    return header + encoded


class RealtimeHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_line = self.rfile.readline().decode("latin-1").strip()
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline().decode("latin-1")
            if line in ("\r\n", "\n", ""):
                break
            name, value = line.split(":", 1)
            headers[name.lower()] = value.strip()

        if not request_line.startswith("GET /v1/realtime?"):
            self._reject(HTTPStatus.NOT_FOUND)
            return
        if "model=gpt-realtime-2.1" not in request_line:
            self._reject(HTTPStatus.BAD_REQUEST)
            return
        if headers.get("authorization") != EXPECTED_AUTH:
            self._reject(HTTPStatus.UNAUTHORIZED)
            return

        client_key = headers.get("sec-websocket-key")
        if not client_key:
            self._reject(HTTPStatus.BAD_REQUEST)
            return
        accept = base64.b64encode(hashlib.sha1(f"{client_key}{WEBSOCKET_GUID}".encode()).digest()).decode()
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        event = {
            "type": "session.created",
            "event_id": "evt_smoke",
            "session": {"id": "sess_smoke", "model": "gpt-realtime-2.1"},
        }
        self.wfile.write(_text_frame(json.dumps(event)))
        self.wfile.flush()
        time.sleep(2)

    def _reject(self, status: HTTPStatus) -> None:
        self.wfile.write(
            f"HTTP/1.1 {status.value} {status.phrase}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode(
                "ascii"
            )
        )


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with ReusableTCPServer((HOST, PORT), RealtimeHandler) as server:
        server.serve_forever()
