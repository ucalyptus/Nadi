# Nadi Architecture

Nadi is a four-tier platform for durable, session-based agent workloads. The core design principle is that **Postgres is the sole durable source of truth**. Runtime processes are replaceable; session state is reconstructed from the database event log.

## Tiers

### 1. Gateway

- Public stateless edge.
- Handles auth, REST, ACP edge, and SSE.
- Routes existing sessions by reading `session_leases × cell_hosts` from Postgres.
- Requests new-session placement from Broker.
- Stores no per-session state.

### 2. Broker

- Control plane for cellhost and sandbox-host registries.
- Computes placement and writes `session_leases`.
- Sends `create_cell` commands to cellhosts over WebSocket.
- Never participates in session traffic or tool calls.

### 3. Celld / Session Cell

- Runs on cellhost MIGs.
- Owns the DB plane for the host.
- Manages session cells as systemd transient units.
- Session cells contain:
  - Go Runtime: event sequencer, tool mediation, ACP Unix socket, inbox consumer.
  - Pi Harness: agent loop, Aquifer/Nadi extension, side-channel tools, model calls.

### 4. Sandboxd / Sandbox-host

- Runs isolated tool execution.
- Manages nspawn + BTRFS sandboxes.
- Executes tool calls via `nsenter`.
- Exports snapshots to GCS.
- Hosts credentials proxy and gitd.

## Data paths

- Session traffic: client → gateway → celld.
- Tool calls: cell → sandboxd directly.
- DB plane: gateway read-only lookups; celld read/write state.
- Control plane: broker ↔ fleets over WebSocket.

## Security invariants

- Cells have no Postgres credentials, no model API keys, and no upstream secrets.
- Sandboxes have no agent code and no real tokens.
- Credentials proxy exchanges short-lived session JWTs for scoped real credentials with audit logging.
- Broker is not on sensitive data paths.
