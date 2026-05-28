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

    def get_or_create_channel_session(
        self,
        platform: str,
        channel_id: str,
        thread_id: str = "",
        tenant_id: str = "local",
        initiator_resource_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently map a platform channel/thread to a session.

        Race-safe: creates a candidate session, then uses INSERT OR IGNORE on
        the channel row so that concurrent callers both converge on the same
        session. If we lose the race, our candidate session is abandoned and
        the winner's session_id is returned. The channel row's session_id is
        immutable once written.
        """
        import uuid as _uuid
        # Each anonymous caller gets a unique resource ID to avoid memory collisions.
        resource_id = initiator_resource_id or f"{platform}:anon-{_uuid.uuid4().hex[:8]}"
        sid = self.store.create_session(tenant_id=tenant_id, metadata=metadata or {})
        try:
            self.broker.place_session(sid)
            actual_sid, cid, created = self.store.get_or_create_channel(
                platform, channel_id, thread_id, sid, resource_id, metadata
            )
            if not created:
                # We lost the race — mark our candidate session as abandoned so
                # it doesn't leak as a permanent zombie.
                self.store.update_session_status(sid, "abandoned")
            return {"session_id": actual_sid, "channel_id": cid, "created": created}
        except Exception:
            self.store.update_session_status(sid, "abandoned")
            raise

    def route(self, session_id: str) -> dict[str, Any]:
        route = self.store.get_route(session_id)
        if not route:
            raise KeyError(f"no active route for session {session_id}")
        return route

    def send_command(self, session_id: str, command_type: str, payload: dict[str, Any] | None = None, actor_resource_id: str | None = None) -> dict[str, Any]:
        # Gateway is stateless: validate route before enqueuing so an expired/missing
        # route raises immediately without orphaning a command in the inbox.
        self.route(session_id)
        command_id = self.store.enqueue_command(session_id, command_type, payload or {}, actor_resource_id=actor_resource_id)
        celld = self.broker.celld_for_route(session_id)
        events = celld.consume_commands(session_id)
        return {"command_id": command_id, "events": events}

    def get_session_events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        self.route(session_id)
        return self.store.get_events(session_id, after)
