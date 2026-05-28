"""SQLite persistence for the local Nadi MVP.

The schema intentionally mirrors the Postgres concepts from migrations/ while
remaining self-contained for local development.
"""
from __future__ import annotations

import json
import sqlite3
import threading
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
        # WAL mode allows concurrent reads alongside writes without blocking.
        self.conn.execute("PRAGMA journal_mode = WAL")
        # Serialise all writes through a single lock so concurrent threads
        # never race on sequence numbers or command claims.
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
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
                    claim_token TEXT,
                    claimed_at REAL,
                    completed_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revoked_tokens (
                    jti TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    revoked_at REAL NOT NULL
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
                CREATE TABLE IF NOT EXISTS channels (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    initiator_resource_id TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (platform, channel_id, thread_id)
                );
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    memory TEXT NOT NULL DEFAULT '{}',
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
                CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires ON revoked_tokens (expires_at);
                CREATE INDEX IF NOT EXISTS idx_channels_platform ON channels (platform, channel_id, thread_id);
                """
            )
            # Migrations for columns added after initial release.
            for stmt in [
                "ALTER TABLE command_inbox ADD COLUMN claim_token TEXT",
                "ALTER TABLE command_inbox ADD COLUMN actor_resource_id TEXT",
                "ALTER TABLE events ADD COLUMN actor_resource_id TEXT",
            ]:
                try:
                    self.conn.execute(stmt)
                except Exception:
                    pass
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
        with self._lock:
            self.conn.execute(
                "INSERT INTO sessions(id, external_id, tenant_id, profile_bundle, status, metadata, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (sid, external_id, tenant_id, profile_bundle, "pending", self._json(metadata), t, t),
            )
            self.conn.commit()
        return sid

    def update_session_status(self, session_id: str, status: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?", (status, now(), session_id))
            self.conn.commit()

    def get_session(self, session_id: str) -> Json | None:
        return self._row(self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def append_event(self, session_id: str, event_type: str, payload: Json | None = None, actor_resource_id: str | None = None) -> Json:
        # Hold the lock for the entire SELECT-INSERT so no concurrent caller
        # can steal the same sequence number.
        with self._lock:
            cur = self.conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE session_id=?", (session_id,))
            seq = int(cur.fetchone()[0])
            self.conn.execute(
                "INSERT INTO events(session_id, sequence, event_type, payload, actor_resource_id, created_at) VALUES(?,?,?,?,?,?)",
                (session_id, seq, event_type, self._json(payload), actor_resource_id, now()),
            )
            self.conn.commit()
        return {"session_id": session_id, "sequence": seq, "event_type": event_type, "payload": payload or {}, "actor_resource_id": actor_resource_id}

    def get_events(self, session_id: str, after: int = 0) -> list[Json]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence", (session_id, after)
        ).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[list-item]

    _MAX_PENDING_COMMANDS = 100
    _MAX_EVENTS_PER_SESSION = 10_000

    def enqueue_command(self, session_id: str, command_type: str, payload: Json | None = None, actor_resource_id: str | None = None) -> int:
        with self._lock:
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM command_inbox WHERE session_id=? AND status='pending'",
                (session_id,),
            ).fetchone()[0]
            if pending >= self._MAX_PENDING_COMMANDS:
                raise ValueError(f"session {session_id} has too many pending commands")
            event_count = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            if event_count >= self._MAX_EVENTS_PER_SESSION:
                raise ValueError(f"session {session_id} has reached the event limit")
            cur = self.conn.execute(
                "INSERT INTO command_inbox(session_id, command_type, payload, actor_resource_id, status, available_at, created_at) VALUES(?,?,?,?,?,?,?)",
                (session_id, command_type, self._json(payload), actor_resource_id, "pending", now(), now()),
            )
            self.conn.commit()
        return int(cur.lastrowid or 0)

    def claim_pending_commands(self, session_id: str, claimed_by: str) -> list[Json]:
        # Generate a per-invocation claim_token so that rows claimed in this call
        # are uniquely identified even across multiple processes sharing the same
        # host_id. The SELECT scopes to this token rather than host_id, preventing
        # a restarted worker from re-processing rows claimed by a prior instance.
        claim_token = str(uuid.uuid4())
        t = now()
        with self._lock:
            self.conn.execute(
                "UPDATE command_inbox SET status='claimed', claimed_by=?, claim_token=?, claimed_at=? "
                "WHERE session_id=? AND status='pending'",
                (claimed_by, claim_token, t, session_id),
            )
            self.conn.commit()
            rows = self.conn.execute(
                "SELECT * FROM command_inbox WHERE claim_token=? AND status='claimed' ORDER BY id",
                (claim_token,),
            ).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[list-item]

    def complete_command(self, command_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE command_inbox SET status='completed', completed_at=? WHERE id=?", (now(), command_id))
            self.conn.commit()

    def upsert_cell_host(self, host_id: str, zone: str, address: str, capacity_total: int, metadata: Json | None = None) -> str:
        t = now()
        with self._lock:
            existing = self.conn.execute("SELECT id FROM cell_hosts WHERE host_id=?", (host_id,)).fetchone()
            if existing:
                self.conn.execute("UPDATE cell_hosts SET zone=?, address=?, capacity_total=?, status='ready', last_heartbeat_at=?, metadata=?, updated_at=? WHERE host_id=?", (zone, address, capacity_total, t, self._json(metadata), t, host_id))
                cid = existing["id"]
            else:
                cid = str(uuid.uuid4())
                self.conn.execute("INSERT INTO cell_hosts(id, host_id, zone, address, capacity_total, capacity_used, status, last_heartbeat_at, metadata, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cid, host_id, zone, address, capacity_total, 0, "ready", t, self._json(metadata), t, t))
            self.conn.commit()
        return cid

    def upsert_sandbox_host(self, host_id: str, zone: str, address: str, metadata: Json | None = None) -> str:
        t = now()
        with self._lock:
            existing = self.conn.execute("SELECT id FROM sandbox_hosts WHERE host_id=?", (host_id,)).fetchone()
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
        # Use an explicit allowlist mapping to avoid any f-string interpolation risk.
        safe = {"cell_hosts": "cell_hosts", "sandbox_hosts": "sandbox_hosts"}[table]
        with self._lock:
            self.conn.execute(f"UPDATE {safe} SET last_heartbeat_at=?, status='ready', updated_at=? WHERE host_id=?", (now(), now(), host_id))
            self.conn.commit()

    def choose_cell_host(self) -> Json:
        row = self.conn.execute("SELECT * FROM cell_hosts WHERE status='ready' AND capacity_used < capacity_total ORDER BY capacity_used ASC, created_at ASC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("no cell host has available capacity")
        return self._row(row)  # type: ignore[return-value]

    def create_lease(self, session_id: str, cell_host_id: str, ttl_seconds: int = 3600) -> str:
        token = str(uuid.uuid4())
        t = now()
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO session_leases(session_id, cell_host_id, lease_token, status, expires_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?)", (session_id, cell_host_id, token, "active", t + ttl_seconds, t, t))
            self.conn.execute("UPDATE cell_hosts SET capacity_used=capacity_used+1, updated_at=? WHERE id=?", (t, cell_host_id))
            self.conn.commit()
        return token

    def get_route(self, session_id: str) -> Json | None:
        # Hold the lock so this read is serialised with concurrent writes,
        # preventing a stale WAL snapshot from hiding a just-committed lease.
        with self._lock:
            row = self.conn.execute("""
                SELECT l.*, h.host_id, h.address, h.zone FROM session_leases l
                JOIN cell_hosts h ON h.id = l.cell_host_id
                WHERE l.session_id=? AND l.status='active' AND l.expires_at>?
            """, (session_id, now())).fetchone()
        return self._row(row)

    def audit(self, actor: str, action: str, session_id: str | None = None, payload: Json | None = None) -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO audit_log(actor, action, session_id, payload, created_at) VALUES(?,?,?,?,?)", (actor, action, session_id, self._json(payload), now()))
            self.conn.commit()
        return int(cur.lastrowid or 0)

    def audit_entries(self, session_id: str | None = None) -> list[Json]:
        if session_id:
            rows = self.conn.execute("SELECT * FROM audit_log WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [self._row(r) for r in rows]  # type: ignore[list-item]

    # ── Channel routing ──────────────────────────────────────────────────────

    def upsert_channel(self, platform: str, channel_id: str, thread_id: str, session_id: str, initiator_resource_id: str, metadata: Json | None = None) -> str:
        """Map a platform channel/thread to a session. Returns the internal channel row id."""
        t = now()
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM channels WHERE platform=? AND channel_id=? AND thread_id=?",
                (platform, channel_id, thread_id),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE channels SET session_id=?, metadata=?, updated_at=? WHERE id=?",
                    (session_id, self._json(metadata), t, existing["id"]),
                )
                cid = existing["id"]
            else:
                cid = str(uuid.uuid4())
                self.conn.execute(
                    "INSERT INTO channels(id, platform, channel_id, thread_id, session_id, initiator_resource_id, metadata, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (cid, platform, channel_id, thread_id, session_id, initiator_resource_id, self._json(metadata), t, t),
                )
            self.conn.commit()
        return cid

    def get_channel(self, platform: str, channel_id: str, thread_id: str = "") -> Json | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM channels WHERE platform=? AND channel_id=? AND thread_id=?",
                (platform, channel_id, thread_id),
            ).fetchone()
        return self._row(row)

    # ── Resource (per-user) memory ───────────────────────────────────────────

    def get_resource_memory(self, resource_id: str) -> Json:
        row = self.conn.execute("SELECT memory FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            return {}
        return json.loads(row["memory"])

    def set_resource_memory(self, resource_id: str, memory: Json, platform: str = "unknown") -> None:
        t = now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO resources(resource_id, platform, memory, created_at, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(resource_id) DO UPDATE SET memory=excluded.memory, updated_at=excluded.updated_at""",
                (resource_id, platform, self._json(memory), t, t),
            )
            self.conn.commit()

    def revoke_token(self, jti: str, expires_at: float) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO revoked_tokens(jti, expires_at, revoked_at) VALUES(?,?,?)",
                (jti, expires_at, now()),
            )
            self.conn.commit()

    def is_token_revoked(self, jti: str) -> bool:
        # Opportunistically purge tokens whose original expiry has passed —
        # once they'd be rejected by the exp check anyway, the revocation row
        # is redundant.
        t = now()
        with self._lock:
            self.conn.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (t,))
            row = self.conn.execute("SELECT 1 FROM revoked_tokens WHERE jti=?", (jti,)).fetchone()
        return row is not None
