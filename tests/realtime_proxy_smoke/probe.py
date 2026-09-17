"""Open a direct proxy WebSocket and require a relayed session.created event."""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
from typing import Final


HOST: Final = "127.0.0.1"
PORT: Final = 14000


def _read_headers(sock: socket.socket) -> bytes:
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("proxy closed during WebSocket handshake")
        response += chunk
    return response


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("proxy closed before a complete WebSocket frame arrived")
        data += chunk
    return data


def _read_text_frame(sock: socket.socket) -> str:
    first, second = _read_exact(sock, 2)
    opcode = first & 0x0F
    if opcode == 0x8:
        raise RuntimeError("proxy closed before session.created")
    if opcode != 0x1:
        raise RuntimeError(f"unexpected WebSocket opcode: {opcode}")
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if masked else b""
    payload = _read_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return payload.decode("utf-8")


def main() -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /v1/realtime?model=gpt-realtime-2.1 HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "OpenAI-Beta: realtime=v1\r\n"
        "Authorization: Bearer sk-test-master\r\n"
        "\r\n"
    )
    with socket.create_connection((HOST, PORT), timeout=10) as sock:
        sock.settimeout(15)
        sock.sendall(request.encode("ascii"))
        response = _read_headers(sock)
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError(f"WebSocket handshake failed: {status_line.decode('latin-1')}")
        event = json.loads(_read_text_frame(sock))
    if event.get("type") != "session.created":
        raise RuntimeError(f"expected session.created, received {event.get('type')!r}")
    print("direct Realtime WebSocket smoke test passed")


if __name__ == "__main__":
    main()

