"""Minimal stdlib HTTP JSON API."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlparse

from .gateway import Gateway
from .runtime import local_stack


def _json(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    raw = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class NadiHandler(BaseHTTPRequestHandler):
    gateway: ClassVar[Gateway]

    def log_message(self, format: str, *args: Any) -> None:  # quiet in tests/demos
        return

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            return _json(self, 200, {"ok": True})
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "events":
            return _json(self, 200, {"events": self.gateway.get_session_events(parts[1])})
        _json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        if path == "/sessions":
            return _json(self, 201, self.gateway.create_session(body.get("tenant_id", "local"), body.get("metadata") or {}))
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "commands":
            return _json(self, 202, self.gateway.send_command(parts[1], body.get("type", body.get("command_type", "echo")), body.get("payload") or {}))
        _json(self, 404, {"error": "not found"})


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    stack = local_stack(db_path)
    NadiHandler.gateway = stack["gateway"]
    return ThreadingHTTPServer((host, port), NadiHandler)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = make_server(db_path, host, port)
    print(json.dumps({"serving": f"http://{host}:{port}", "db": db_path}))
    httpd.serve_forever()
