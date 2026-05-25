"""Deterministic local Pi Harness/runtime."""
from __future__ import annotations

from typing import Any, Protocol


class ToolExecutor(Protocol):
    def execute_tool(self, session_id: str, token: str, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]: ...


class Runtime:
    def __init__(self, sandbox: ToolExecutor):
        self.sandbox = sandbox

    def handle(self, session_id: str, token: str, command_type: str, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if command_type == "echo":
            return [("assistant.message", {"text": str(payload.get("text", ""))})]
        if command_type == "model":
            prompt = str(payload.get("prompt", ""))
            return [("model.response", {"text": f"deterministic-model:{prompt}"})]
        if command_type == "tool":
            out = self.sandbox.execute_tool(session_id, token, str(payload.get("name", "echo")), payload.get("args") or {})
            return [("tool.result", out)]
        return [("command.error", {"error": f"unknown command: {command_type}"})]


def local_stack(db_path: str = ":memory:"):
    """Build an all-in-one local OpenRiver stack for demos/tests."""
    from .broker import Broker
    from .celld import Celld
    from .gateway import Gateway
    from .sandboxd import Sandboxd
    from .security import SessionJWT
    from .store import Store

    store = Store(db_path)
    jwt = SessionJWT()
    sandbox = Sandboxd(store, jwt)
    celld = Celld("cell-local", store, sandbox, jwt)
    broker = Broker(store)
    broker.register_sandbox_host("sandbox-local", "local", "inproc://sandbox", object_ref=sandbox)
    broker.register_cell_host("cell-local", "local", "inproc://cell", 100, create_cell=celld.create_cell, object_ref=celld)
    gateway = Gateway(store, broker)
    return {"store": store, "jwt": jwt, "sandboxd": sandbox, "celld": celld, "broker": broker, "gateway": gateway}
