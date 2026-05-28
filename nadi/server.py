"""Minimal stdlib HTTP JSON API."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlparse

from .gateway import Gateway
from .runtime import local_stack

_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB hard limit


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
        if n > _MAX_BODY_BYTES:
            raise ValueError(f"request body too large ({n} > {_MAX_BODY_BYTES})")
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
        try:
            body = self._body()
        except ValueError as exc:
            return _json(self, 413, {"error": str(exc)})
        if path == "/sessions":
            return _json(self, 201, self.gateway.create_session(body.get("tenant_id", "local"), body.get("metadata") or {}))
        if path == "/channels":
            # Idempotent channel→session routing for multi-user platforms.
            return _json(self, 200, self.gateway.get_or_create_channel_session(
                platform=body.get("platform", ""),
                channel_id=body.get("channel_id", ""),
                thread_id=body.get("thread_id", ""),
                tenant_id=body.get("tenant_id", "local"),
                initiator_resource_id=body.get("initiator_resource_id", ""),
                metadata=body.get("metadata") or {},
            ))
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "commands":
            return _json(self, 202, self.gateway.send_command(
                parts[1],
                body.get("type", body.get("command_type", "echo")),
                body.get("payload") or {},
                actor_resource_id=body.get("actor_resource_id"),
            ))
        _json(self, 404, {"error": "not found"})


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    stack = local_stack(db_path)
    NadiHandler.gateway = stack["gateway"]
    return ThreadingHTTPServer((host, port), NadiHandler)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = make_server(db_path, host, port)
    print(json.dumps({"serving": f"http://{host}:{port}", "db": db_path}))
    httpd.serve_forever()
