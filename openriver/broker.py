"""Broker control plane: host registry, placement, leases, lifecycle."""
from __future__ import annotations

from typing import Any, Callable

from .store import Store

CreateCell = Callable[[str], Any]


class Broker:
    def __init__(self, store: Store):
        self.store = store
        self.cell_callbacks: dict[str, CreateCell] = {}
        self.cell_objects: dict[str, Any] = {}
        self.sandbox_objects: dict[str, Any] = {}
        self.tool_path_calls = 0  # tests assert this stays zero for tool execution

    def register_cell_host(self, host_id: str, zone: str, address: str, capacity_total: int, create_cell: CreateCell, object_ref: Any | None = None) -> str:
        cid = self.store.upsert_cell_host(host_id, zone, address, capacity_total)
        self.cell_callbacks[host_id] = create_cell
        if object_ref is not None:
            self.cell_objects[host_id] = object_ref
        return cid

    def register_sandbox_host(self, host_id: str, zone: str, address: str, object_ref: Any | None = None) -> str:
        sid = self.store.upsert_sandbox_host(host_id, zone, address)
        if object_ref is not None:
            self.sandbox_objects[host_id] = object_ref
        return sid

    def heartbeat(self, host_type: str, host_id: str) -> None:
        table = {"cell": "cell_hosts", "sandbox": "sandbox_hosts"}[host_type]
        self.store.heartbeat_host(table, host_id)

    def place_session(self, session_id: str) -> dict[str, Any]:
        host = self.store.choose_cell_host()
        lease = self.store.create_lease(session_id, host["id"])
        callback = self.cell_callbacks.get(host["host_id"])
        if not callback:
            raise RuntimeError(f"no celld callback registered for {host['host_id']}")
        callback(session_id)
        return {"session_id": session_id, "cell_host": host["host_id"], "lease_token": lease, "address": host["address"]}

    def celld_for_route(self, session_id: str) -> Any:
        route = self.store.get_route(session_id)
        if not route:
            raise RuntimeError("no active route")
        return self.cell_objects[route["host_id"]]
