"""Tiny localhost HTTP server shared by Silkworm's local integrations:
tool-approval requests (approval_hook.py) and session lifecycle events
(session_hook.py). POST-only, JSON in/out, 127.0.0.1 only.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("silkworm.server")


class LocalServer:
    def __init__(self, port: int):
        self.port = port
        self._routes = {}

    def route(self, path: str, fn) -> None:
        """fn(payload: dict) -> dict; runs on the request thread and may block."""
        self._routes[path] = fn

    def start(self) -> None:
        routes = self._routes

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                fn = routes.get(self.path)
                if fn is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length)) if length else {}
                except Exception:
                    payload = {}
                try:
                    answer = fn(payload) or {}
                except Exception:
                    log.exception("handler for %s failed", self.path)
                    answer = {}
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="silkworm-http").start()
        log.info("local server listening on 127.0.0.1:%d (%s)",
                 self.port, ", ".join(self._routes) or "no routes")
