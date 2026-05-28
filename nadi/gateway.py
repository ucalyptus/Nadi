"""Stateless gateway facade over Store and Broker."""
from __future__ import annotations

from typing import Any

from .broker import Broker
from .store import Store


class Gateway:
    def __init__(self, store: Store, broker: Broker):
        self.store = store
        self.broker = broker

    def create_session(self, tenant_id: str = "local", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        sid = self.store.create_session(tenant_id=tenant_id, metadata=metadata or {})
        placement = self.broker.place_session(sid)
        return {"session_id": sid, "status": "running", "route": placement}

    def route(self, session_id: str) -> dict[str, Any]:
        route = self.store.get_route(session_id)
        if not route:
            raise KeyError(f"no active route for session {session_id}")
        return route

    def send_command(self, session_id: str, command_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # Gateway is stateless: validate route before enqueuing so an expired/missing
        # route raises immediately without orphaning a command in the inbox.
        self.route(session_id)
        command_id = self.store.enqueue_command(session_id, command_type, payload or {})
        celld = self.broker.celld_for_route(session_id)
        events = celld.consume_commands(session_id)
        return {"command_id": command_id, "events": events}

    def get_session_events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        self.route(session_id)
        return self.store.get_events(session_id, after)
