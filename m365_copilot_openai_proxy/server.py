"""A small threaded HTTP server built on the standard library.

Replaces uvicorn. Supports buffered JSON responses and streamed Server-Sent
Events. ``Server`` exposes ``started`` / ``should_exit`` so the CLI can run it
in a thread and stop it on a keypress, the same way it drove uvicorn before.
"""

from __future__ import annotations

import http.server
import socketserver

from .app import App


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(app: App):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                response = app.handle(method, self.path, headers, body)
            except Exception as exc:  # never leak a traceback to the socket
                self._send_buffered(500, b'{"detail":"internal error"}', "application/json")
                self.log_error("handler error: %s", exc)
                return

            if response.stream is not None:
                self._send_stream(response)
            else:
                self._send_buffered(response.status, response.body, response.content_type)

        def _send_buffered(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_stream(self, response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in response.stream:
                    data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                    self.wfile.write(data)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, *args) -> None:  # quiet by default
            pass

    return Handler


class Server:
    def __init__(self, app: App, host: str, port: int, poll_interval: float = 0.25):
        self._httpd = _ThreadingHTTPServer((host, port), _make_handler(app))
        self._httpd.timeout = poll_interval
        self.host = host
        self.port = port
        self.started = False
        self.should_exit = False

    def run(self) -> None:
        self.started = True
        try:
            while not self.should_exit:
                self._httpd.handle_request()  # returns after a request or poll_interval
        finally:
            self._httpd.server_close()
