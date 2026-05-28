"""SQLite persistence for the local Nadi MVP.

The schema intentionally mirrors the Postgres concepts from migrations/ while
remaining self-contained for local development.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


Json = dict[str, Any]


def now() -> float:
    return time.time()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                external_id TEXT UNIQUE,
                tenant_id TEXT NOT NULL,
                profile_bundle TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                UNIQUE (session_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS command_inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                command_type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                available_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                completed_at REAL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cell_hosts (
                id TEXT PRIMARY KEY,
                host_id TEXT NOT NULL UNIQUE,
                zone TEXT NOT NULL,
                address TEXT NOT NULL,
                capacity_total INTEGER NOT NULL DEFAULT 0,
                capacity_used INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_heartbeat_at REAL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_leases (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                cell_host_id TEXT NOT NULL REFERENCES cell_hosts(id),
                lease_token TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_hosts (
                id TEXT PRIMARY KEY,
                host_id TEXT NOT NULL UNIQUE,
                zone TEXT NOT NULL,
                address TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_heartbeat_at REAL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                session_id TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_session_sequence ON events (session_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_command_inbox_pending ON command_inbox (session_id, status, available_at, id);
            """
        )
        self.conn.commit()

    def _json(self, data: Json | None) -> str:
        return json.dumps(data or {}, sort_keys=True)

    def _row(self, row: sqlite3.Row | None) -> Json | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("metadata", "payload"):
            if key in out and isinstance(out[key], str):
                out[key] = json.loads(out[key])
        return out

    def create_session(self, tenant_id: str, external_id: str | None = None, profile_bundle: str | None = None, metadata: Json | None = None) -> str:
        sid = str(uuid.uuid4())
        t = now()
        self.conn.execute(
            "INSERT INTO sessions(id, external_id, tenant_id, profile_bundle, status, metadata, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, external_id, tenant_id, profile_bundle, "pending", self._json(metadata), t, t),
        )
        self.conn.commit()
        return sid

    def update_session_status(self, session_id: str, status: str) -> None:
        self.conn.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?", (status, now(), session_id))
        self.conn.commit()

    def get_session(self, session_id: str) -> Json | None:
        return self._row(self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def append_event(self, session_id: str, event_type: str, payload: Json | None = None) -> Json:
        cur = self.conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE session_id=?", (session_id,))
        seq = int(cur.fetchone()[0])
        self.conn.execute(
            "INSERT INTO events(session_id, sequence, event_type, payload, created_at) VALUES(?,?,?,?,?)",
            (session_id, seq, event_type, self._json(payload), now()),
        )
        self.conn.commit()
        return {"session_id": session_id, "sequence": seq, "event_type": event_type, "payload": payload or {}}

    def get_events(self, session_id: str, after: int = 0) -> list[Json]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence", (session_id, after)
        ).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[list-item]

    def enqueue_command(self, session_id: str, command_type: str, payload: Json | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO command_inbox(session_id, command_type, payload, status, available_at, created_at) VALUES(?,?,?,?,?,?)",
            (session_id, command_type, self._json(payload), "pending", now(), now()),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def claim_pending_commands(self, session_id: str, claimed_by: str) -> list[Json]:
        rows = self.conn.execute(
            "SELECT * FROM command_inbox WHERE session_id=? AND status='pending' ORDER BY id", (session_id,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            q = ",".join("?" for _ in ids)
            self.conn.execute(f"UPDATE command_inbox SET status='claimed', claimed_by=?, claimed_at=? WHERE id IN ({q})", [claimed_by, now(), *ids])
            self.conn.commit()
        return [self._row(r) for r in rows]  # type: ignore[list-item]

    def complete_command(self, command_id: int) -> None:
        self.conn.execute("UPDATE command_inbox SET status='completed', completed_at=? WHERE id=?", (now(), command_id))
        self.conn.commit()

    def upsert_cell_host(self, host_id: str, zone: str, address: str, capacity_total: int, metadata: Json | None = None) -> str:
        existing = self.conn.execute("SELECT id FROM cell_hosts WHERE host_id=?", (host_id,)).fetchone()
        t = now()
        if existing:
            self.conn.execute("UPDATE cell_hosts SET zone=?, address=?, capacity_total=?, status='ready', last_heartbeat_at=?, metadata=?, updated_at=? WHERE host_id=?", (zone, address, capacity_total, t, self._json(metadata), t, host_id))
            cid = existing["id"]
        else:
            cid = str(uuid.uuid4())
            self.conn.execute("INSERT INTO cell_hosts(id, host_id, zone, address, capacity_total, capacity_used, status, last_heartbeat_at, metadata, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cid, host_id, zone, address, capacity_total, 0, "ready", t, self._json(metadata), t, t))
        self.conn.commit()
        return cid

    def upsert_sandbox_host(self, host_id: str, zone: str, address: str, metadata: Json | None = None) -> str:
        existing = self.conn.execute("SELECT id FROM sandbox_hosts WHERE host_id=?", (host_id,)).fetchone()
        t = now()
        if existing:
            self.conn.execute("UPDATE sandbox_hosts SET zone=?, address=?, status='ready', last_heartbeat_at=?, metadata=?, updated_at=? WHERE host_id=?", (zone, address, t, self._json(metadata), t, host_id))
            sid = existing["id"]
        else:
            sid = str(uuid.uuid4())
            self.conn.execute("INSERT INTO sandbox_hosts(id, host_id, zone, address, status, last_heartbeat_at, metadata, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (sid, host_id, zone, address, "ready", t, self._json(metadata), t, t))
        self.conn.commit()
        return sid

    def heartbeat_host(self, table: str, host_id: str) -> None:
        if table not in {"cell_hosts", "sandbox_hosts"}:
            raise ValueError("bad host table")
        self.conn.execute(f"UPDATE {table} SET last_heartbeat_at=?, status='ready', updated_at=? WHERE host_id=?", (now(), now(), host_id))
        self.conn.commit()

    def choose_cell_host(self) -> Json:
        row = self.conn.execute("SELECT * FROM cell_hosts WHERE status='ready' AND capacity_used < capacity_total ORDER BY capacity_used ASC, created_at ASC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("no cell host has available capacity")
        return self._row(row)  # type: ignore[return-value]

    def create_lease(self, session_id: str, cell_host_id: str, ttl_seconds: int = 3600) -> str:
        token = str(uuid.uuid4())
        t = now()
        self.conn.execute("INSERT OR REPLACE INTO session_leases(session_id, cell_host_id, lease_token, status, expires_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?)", (session_id, cell_host_id, token, "active", t + ttl_seconds, t, t))
        self.conn.execute("UPDATE cell_hosts SET capacity_used=capacity_used+1, updated_at=? WHERE id=?", (t, cell_host_id))
        self.conn.commit()
        return token

    def get_route(self, session_id: str) -> Json | None:
        row = self.conn.execute("""
            SELECT l.*, h.host_id, h.address, h.zone FROM session_leases l
            JOIN cell_hosts h ON h.id = l.cell_host_id
            WHERE l.session_id=? AND l.status='active' AND l.expires_at>?
        """, (session_id, now())).fetchone()
        return self._row(row)

    def audit(self, actor: str, action: str, session_id: str | None = None, payload: Json | None = None) -> int:
        cur = self.conn.execute("INSERT INTO audit_log(actor, action, session_id, payload, created_at) VALUES(?,?,?,?,?)", (actor, action, session_id, self._json(payload), now()))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def audit_entries(self, session_id: str | None = None) -> list[Json]:
        if session_id:
            rows = self.conn.execute("SELECT * FROM audit_log WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [self._row(r) for r in rows]  # type: ignore[list-item]
