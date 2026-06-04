"""Round-trip test for the standard-library WebSocket client against a tiny
raw-socket server that performs the RFC 6455 handshake and echoes one frame.

This exercises the handshake (Sec-WebSocket-Accept), client-side masking,
server-frame parsing, and a fragmented message.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading

from m365_copilot_openai_proxy.wsclient import WebSocket, _proxy_from_env

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _server_handshake(conn: socket.socket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(1024)
        if not chunk:
            return
        data += chunk
    key = ""
    for line in data.decode("latin-1").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "sec-websocket-key":
            key = value.strip()
    accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
    )


def _server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    # Server frames are unmasked.
    b0 = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([b0, length])
    elif length < 65536:
        header = bytes([b0, 126]) + struct.pack(">H", length)
    else:
        header = bytes([b0, 127]) + struct.pack(">Q", length)
    return header + payload


def _read_client_frame(conn: socket.socket) -> bytes:
    def read(n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise AssertionError("client closed early")
            buf += chunk
        return buf

    b0, b1 = read(2)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read(8))[0]
    assert b1 & 0x80, "client frames must be masked"
    mask = read(4)
    payload = read(length)
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def test_wsclient_handshake_send_and_recv() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received = {}

    def serve():
        conn, _ = srv.accept()
        with conn:
            _server_handshake(conn)
            received["msg"] = _read_client_frame(conn).decode("utf-8")
            # echo back as two fragments to test reassembly
            conn.sendall(_server_frame(b"ping-", opcode=0x1, fin=False))
            conn.sendall(_server_frame(b"pong", opcode=0x0, fin=True))

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    with WebSocket.connect(f"ws://127.0.0.1:{port}/chat") as ws:
        ws.send("hello-server")
        reply = ws.recv(timeout=5)

    t.join(timeout=5)
    assert received["msg"] == "hello-server"
    assert reply == "ping-pong"


def _start_echo_server() -> tuple[int, dict]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received: dict = {}

    def serve():
        conn, _ = srv.accept()
        with conn:
            _server_handshake(conn)
            received["msg"] = _read_client_frame(conn).decode("utf-8")
            conn.sendall(_server_frame(b"echo:" + received["msg"].encode()))

    threading.Thread(target=serve, daemon=True).start()
    return port, received


def _start_connect_proxy(target_port: int) -> tuple[int, dict]:
    """A minimal HTTP CONNECT proxy that tunnels to 127.0.0.1:<target_port>."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen: dict = {}

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def serve():
        client, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = client.recv(1024)
            if not chunk:
                return
            req += chunk
        request_line = req.split(b"\r\n", 1)[0].decode("latin-1")
        seen["connect"] = request_line  # e.g. "CONNECT 127.0.0.1:<port> HTTP/1.1"
        upstream = socket.create_connection(("127.0.0.1", target_port))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return port, seen


def test_wsclient_connects_through_http_proxy() -> None:
    echo_port, received = _start_echo_server()
    proxy_port, seen = _start_connect_proxy(echo_port)

    with WebSocket.connect(
        f"ws://127.0.0.1:{echo_port}/chat",
        proxy=f"http://127.0.0.1:{proxy_port}",
    ) as ws:
        ws.send("through-proxy")
        reply = ws.recv(timeout=5)

    assert received["msg"] == "through-proxy"
    assert reply == "echo:through-proxy"
    # confirm the connection actually went via the proxy's CONNECT tunnel
    assert seen["connect"] == f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1"


def test_proxy_from_env_reads_standard_vars(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://corp-proxy:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert _proxy_from_env("wss", "substrate.office.com") == "http://corp-proxy:8080"

    # NO_PROXY should suppress proxying for matching hosts
    monkeypatch.setenv("NO_PROXY", "substrate.office.com")
    assert _proxy_from_env("wss", "substrate.office.com") is None
