"""Cell host daemon and reconstructable session cells."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .runtime import Runtime
from .security import SessionJWT
from .store import Store


@dataclass
class Cell:
    # Deliberately no Store, DB credentials, API keys, or upstream secrets.
    session_id: str
    jwt: str
    state: dict[str, Any] = field(default_factory=lambda: {"messages": [], "tools": []})


class Celld:
    def __init__(self, host_id: str, store: Store, sandboxd: Any, jwt: SessionJWT):
        self.host_id = host_id
        self.store = store
        self.sandboxd = sandboxd
        self.jwt = jwt
        self.runtime = Runtime(sandboxd)
        self.cells: dict[str, Cell] = {}

    def create_cell(self, session_id: str) -> Cell:
        token = self.jwt.sign(session_id, "cell tool", ttl_seconds=3600, subject=self.host_id)
        cell = Cell(session_id=session_id, jwt=token)
        self.cells[session_id] = cell
        self.store.update_session_status(session_id, "running")
        self.append_event(session_id, "session.started", {"cell_host": self.host_id})
        return cell

    def reconstruct_cell(self, session_id: str) -> Cell:
        token = self.jwt.sign(session_id, "cell tool", ttl_seconds=3600, subject=self.host_id)
        cell = Cell(session_id=session_id, jwt=token)
        for event in self.store.get_events(session_id):
            self._apply(cell, event["event_type"], event["payload"])
        self.cells[session_id] = cell
        return cell

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        ev = self.store.append_event(session_id, event_type, payload or {})
        cell = self.cells.get(session_id)
        if cell:
            self._apply(cell, event_type, payload or {})
        return ev

    def consume_commands(self, session_id: str) -> list[dict[str, Any]]:
        cell = self.cells.get(session_id) or self.reconstruct_cell(session_id)
        commands = self.store.claim_pending_commands(session_id, self.host_id)
        emitted: list[dict[str, Any]] = []
        for cmd in commands:
            self.append_event(session_id, "command.received", {"command_id": cmd["id"], "command_type": cmd["command_type"], "payload": cmd["payload"]})
            for event_type, payload in self.runtime.handle(session_id, cell.jwt, cmd["command_type"], cmd["payload"]):
                emitted.append(self.append_event(session_id, event_type, payload))
            self.store.complete_command(cmd["id"])
        return emitted

    def _apply(self, cell: Cell, event_type: str, payload: dict[str, Any]) -> None:
        if event_type in {"assistant.message", "model.response"}:
            cell.state["messages"].append(payload.get("text", ""))
        elif event_type == "tool.result":
            cell.state["tools"].append(payload)
        elif event_type == "session.started":
            cell.state["started"] = True
