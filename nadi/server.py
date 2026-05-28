"""Minimal stdlib HTTP JSON API."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlparse

from .gateway import Gateway
from .runtime import local_stack

_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB hard limit
_MAX_FIELD_BYTES = 8192             # per-field limit for string fields


def _json(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    raw = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _check_api_key(handler: BaseHTTPRequestHandler, api_key: str | None) -> bool:
    """Return True if the request is authorised. Always True when no key is configured."""
    if api_key is None:
        return True
    return handler.headers.get("x-nadi-key", "") == api_key


class NadiHandler(BaseHTTPRequestHandler):
    gateway: ClassVar[Gateway]
    api_key: ClassVar[str | None] = None
    # tenant_id is pinned server-side when an api_key is configured; callers
    # cannot override it via the request body to prevent cross-tenant escalation.
    tenant_id: ClassVar[str] = "local"

    def log_message(self, format: str, *args: Any) -> None:  # quiet in tests/demos
        return

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("content-length", "0"))
        if n > _MAX_BODY_BYTES:
            raise ValueError(f"request body too large ({n} > {_MAX_BODY_BYTES})")
        return json.loads(self.rfile.read(n) or b"{}")

    def _require_auth(self) -> bool:
        """Send 401 and return False if the request is not authorised."""
        if not _check_api_key(self, self.api_key):
            _json(self, 401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            return _json(self, 200, {"ok": True})
        if not self._require_auth():
            return
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "events":
            return _json(self, 200, {"events": self.gateway.get_session_events(parts[1])})
        _json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as exc:
            return _json(self, 413, {"error": str(exc)})

        if path == "/sessions":
            return _json(self, 201, self.gateway.create_session(self.tenant_id, body.get("metadata") or {}))

        if path == "/channels":
            # Validate field sizes to prevent DoS via oversized strings.
            for field in ("platform", "channel_id", "thread_id", "initiator_resource_id"):
                val = body.get(field, "")
                if isinstance(val, str) and len(val.encode()) > _MAX_FIELD_BYTES:
                    return _json(self, 400, {"error": f"field '{field}' exceeds size limit"})
            return _json(self, 200, self.gateway.get_or_create_channel_session(
                platform=str(body.get("platform", "")),
                channel_id=str(body.get("channel_id", "")),
                thread_id=str(body.get("thread_id", "")),
                tenant_id=self.tenant_id,  # always server-side — never from body
                initiator_resource_id=str(body.get("initiator_resource_id", "")),
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


def make_server(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    tenant_id: str = "local",
) -> ThreadingHTTPServer:
    stack = local_stack(db_path)
    NadiHandler.gateway = stack["gateway"]
    NadiHandler.api_key = api_key
    NadiHandler.tenant_id = tenant_id
    return ThreadingHTTPServer((host, port), NadiHandler)


def serve(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    tenant_id: str = "local",
) -> None:
    httpd = make_server(db_path, host, port, api_key=api_key, tenant_id=tenant_id)
    print(json.dumps({"serving": f"http://{host}:{port}", "db": db_path}))
    httpd.serve_forever()
