# OpenRiver Roadmap

## Phase 0 — Foundations

- Postgres schema and migrations.
- MIG templates, VPC, firewall rules, GCS snapshot bucket.
- Secrets management and JWT signing infrastructure.
- Observability with traces, logs, and per-cell metrics.
- Monorepo and CI/CD setup.

## Phase 1 — Gateway

- Stateless auth/REST/ACP/SSE edge.
- Postgres-backed session routing.
- New-session lifecycle handoff to Broker.
- Load test target: 10K concurrent session lookups, p99 < 5ms.

## Phase 2 — Broker

- Cellhost and sandbox-host registries.
- Placement engine and `session_leases` writes.
- `create_cell` commands over WebSocket.
- Admin API and HA strategy.
- Verification that Broker is not in data paths.

## Phase 3 — Celld + Session Cell

- Cellhost registration and systemd transient unit management.
- Event sequencer, command inbox, ACP Unix socket.
- Tool-call mediation directly to Sandboxd.
- Pi Harness agent loop and model calls.
- Event-log-based reconstruction validation.

## Phase 4 — Sandboxd + Sandbox

- nspawn + BTRFS lifecycle.
- Tool execution RPC server.
- Snapshot export to GCS.
- Credentials proxy and gitd.
- Network namespace restrictions.

## Phase 5 — Integration & Migration

- End-to-end lifecycle and failure testing.
- River dual-write and shadow traffic.
- Incremental cutover and rollback plan.
- Runbooks, dashboards, alerts, and zone path validation.
