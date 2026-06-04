"""A minimal, dependency-free WebSocket client (RFC 6455).

Python's standard library ships a WebSocket server (since 3.x via asyncio in
third-party land, not stdlib) but no client, so this module implements just
enough of the client side to replace the ``websockets`` package:

  * ``ws://`` and ``wss://`` (TLS via the stdlib ``ssl`` module),
  * the HTTP Upgrade handshake with ``Sec-WebSocket-Accept`` verification,
  * client-masked outbound frames (required by the spec),
  * inbound frame reassembly, with automatic ping/pong and close handling,
  * a blocking ``recv()`` with an optional per-call timeout for polling loops.

It is synchronous and uses only the standard library.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.request
from urllib.parse import unquote, urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WebSocketError(Exception):
    """Raised for handshake failures, protocol errors, or a closed connection."""


class WebSocketTimeout(WebSocketError):
    """Raised by ``recv(timeout=...)`` when no message arrives in time."""


class WebSocket:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""
        self.closed = False

    # -- construction -------------------------------------------------------

    @classmethod
    def connect(
        cls,
        url: str,
        headers: dict | None = None,
        timeout: float = 30.0,
        proxy: str | None = None,
    ) -> "WebSocket":
        """Open a WebSocket.

        ``proxy`` is an HTTP proxy URL (e.g. ``http://user:pass@host:8080``) to
        tunnel through via HTTP CONNECT. If ``None``, the proxy is taken from
        the standard ``HTTPS_PROXY``/``HTTP_PROXY`` environment (respecting
        ``NO_PROXY``); pass ``""`` to force a direct connection.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme: {parts.scheme!r}")
        secure = parts.scheme == "wss"
        host = parts.hostname
        if not host:
            raise WebSocketError("URL is missing a host")
        port = parts.port or (443 if secure else 80)
        resource = parts.path or "/"
        if parts.query:
            resource += "?" + parts.query

        if proxy is None:
            proxy = _proxy_from_env(parts.scheme, host)

        if proxy:
            sock = _open_via_http_proxy(proxy, host, port, timeout)
        else:
            sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)

            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = host if port in (80, 443) else f"{host}:{port}"
            lines = [
                f"GET {resource} HTTP/1.1",
                f"Host: {host_header}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            for name, value in (headers or {}).items():
                lines.append(f"{name}: {value}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

            ws = cls(sock)
            ws._read_handshake(key)
            sock.settimeout(None)  # blocking reads by default afterwards
            return ws
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _read_handshake(self, key: str) -> None:
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        text = head.decode("latin-1")
        status_line, *header_lines = text.split("\r\n")
        if "101" not in status_line:
            raise WebSocketError(f"unexpected handshake response: {status_line!r}")
        accept = ""
        for line in header_lines:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept = value.strip()
                break
        expected = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            raise WebSocketError("invalid Sec-WebSocket-Accept in handshake")

    # -- low-level byte reads ----------------------------------------------

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                self.closed = True
                raise WebSocketError("connection closed by peer")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    # -- frames -------------------------------------------------------------

    def _read_frame(self, first_byte_timeout: float | None):
        # Only the wait for the *first* byte is subject to the poll timeout, so
        # a timeout never leaves us with a half-consumed frame in the buffer.
        self._sock.settimeout(first_byte_timeout)
        try:
            b0 = self._read(1)[0]
        except (socket.timeout, TimeoutError):
            raise WebSocketTimeout("recv timed out")
        finally:
            self._sock.settimeout(None)

        b1 = self._read(1)[0]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else b""
        payload = self._read(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    # -- public API ---------------------------------------------------------

    def send(self, data) -> None:
        if isinstance(data, str):
            self._send_frame(_OP_TEXT, data.encode("utf-8"))
        else:
            self._send_frame(_OP_BINARY, bytes(data))

    def recv(self, timeout: float | None = None):
        """Return the next data message (str for text, bytes for binary).

        Control frames (ping/pong/close) are handled internally. ``timeout``
        bounds the wait for the start of a message; on expiry it raises
        ``WebSocketTimeout`` without disturbing the stream.
        """
        fragments: list[bytes] = []
        frag_opcode = None
        while True:
            fin, opcode, payload = self._read_frame(timeout)
            if opcode == _OP_CLOSE:
                self.closed = True
                raise WebSocketError("connection closed by peer")
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CONT:
                fragments.append(payload)
                if fin:
                    return self._decode(frag_opcode, b"".join(fragments))
                continue
            # text or binary
            if not fin:
                frag_opcode = opcode
                fragments = [payload]
                continue
            return self._decode(opcode, payload)

    @staticmethod
    def _decode(opcode, payload: bytes):
        if opcode == _OP_TEXT:
            return payload.decode("utf-8", "replace")
        return payload

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return self.recv()
        except WebSocketError:
            raise StopIteration

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(_OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --- HTTP proxy support (CONNECT tunnelling) -------------------------------

def _proxy_from_env(scheme: str, host: str) -> str | None:
    """Return the proxy URL for ``scheme`` from the environment, honouring
    NO_PROXY. ``wss`` maps to the HTTPS proxy, ``ws`` to the HTTP proxy."""
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except (ValueError, OSError):
        pass
    proxies = urllib.request.getproxies()
    return proxies.get("https" if scheme == "wss" else "http")


def _open_via_http_proxy(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    """Open a TCP socket to ``host:port`` tunnelled through an HTTP proxy using
    the CONNECT method. The returned socket carries raw bytes to the target, so
    the caller can wrap it in TLS for ``wss``."""
    if "://" not in proxy:
        proxy = "http://" + proxy
    p = urlsplit(proxy)
    proxy_host = p.hostname
    proxy_port = p.port or 80
    if not proxy_host:
        raise WebSocketError(f"invalid proxy URL: {proxy!r}")

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        lines = [
            f"CONNECT {host}:{port} HTTP/1.1",
            f"Host: {host}:{port}",
            "Proxy-Connection: keep-alive",
        ]
        if p.username:
            raw = f"{unquote(p.username)}:{unquote(p.password or '')}".encode("utf-8")
            lines.append("Proxy-Authorization: Basic " + base64.b64encode(raw).decode("ascii"))
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

        # Read the CONNECT response one byte at a time so we never consume any
        # bytes that belong to the tunnel itself (e.g. the start of the TLS
        # handshake the caller is about to perform).
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(1)
            if not chunk:
                raise WebSocketError("proxy closed the connection during CONNECT")
            data += chunk
        status_line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        fields = status_line.split(maxsplit=2)
        if len(fields) < 2 or fields[1] != "200":
            raise WebSocketError(f"proxy CONNECT failed: {status_line!r}")
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
