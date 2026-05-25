# Aquifer — Full System Build Plan (v2)

## System Summary

Aquifer replaces River with a four-tier, Postgres-centric platform for session-based agent workloads on cloud VMs. Each tier runs on its own MIG. Postgres is the sole source of truth for all session state — it is “the thing that survives.”

-----

## Architecture Reference

### Four Tiers (each on its own MIG)

|Tier                   |Component |Zone Path                  |Role                                                               |
|-----------------------|----------|---------------------------|-------------------------------------------------------------------|
|1 — Gateway (public)   |`gateway` |`//system/aquifer/gateway` |Stateless edge. Auth · REST · ACP edge · SSE. No per-session state.|
|2 — Broker (control)   |`broker`  |`//system/aquifer/broker`  |Cell + Sandbox registries · Placement. **NOT in any data path.**   |
|3 — Cellhost (sessions)|`celld`   |`//system/aquifer/celld`   |Cell lifecycle · DB plane. Only Postgres client on the host.       |
|4 — Sandbox-host (exec)|`sandboxd`|`//system/aquifer/sandboxd`|Tool exec · Drivers · Snapshots. No agent code runs here.          |

### Postgres (Database / Store)

Tables: `sessions`, `events`, `command_inbox`, `session_leases`, `cell_hosts`

Source of truth for all session state. The thing that survives.

### External Clients

Slack · Web UI · CLI · CI · Bots → Gateway via REST / ACP / SSE

### Traffic Flows

|Flow           |Path                              |Arrow Color         |Protocol                  |
|---------------|----------------------------------|--------------------|--------------------------|
|Session traffic|External Clients → Gateway → celld|Cyan (solid)        |REST / ACP / SSE          |
|Lifecycle      |Gateway → Broker                  |Green (solid)       |Lifecycle events          |
|Control plane  |Broker ↔ Cellhost Fleet           |Dashed              |WS /ws · create_cell      |
|Control plane  |Broker ↔ Sandbox-host Fleet       |Dashed              |WS /ws                    |
|Tool calls     |Cell → sandboxd (direct)          |Yellow/amber (solid)|Direct RPC, **no broker** |
|DB access      |Gateway → Postgres                |Purple              |Commands (read)           |
|DB access      |celld → Postgres                  |Purple              |Session state (read/write)|
|Snapshots      |sandboxd → GCS                    |Yellow (dashed)     |BTRFS snapshots           |

### Session Cell — systemd transient unit

Lives inside a Cellhost, managed by `celld`. Contains two sub-components connected via RPC:

**Go Runtime** (`//system/aquifer/runtime`)

- Event sequencer
- Tool-call mediation
- ACP server (Unix socket)
- Inbox consumer

**Pi Harness** (`//system/aquifer/agent`) — RPC child of Go Runtime

- Agent loop (RPC child)
- Aquifer extension
- Side-channel tools
- Model calls

**Security invariant:** No Postgres credential · No model API key · No upstream secret. Holds only short-lived JWTs for its single session.

**Durability:** Entire cell state is reconstructable from Postgres event log.

**Sub-stores** (below session cell):

- **Profile Bundles** — riverbed · go-review · contexts
- **Memory Store** — agent · MRU · elo storage

### Sandbox (inside Sandbox-host)

Entered via `nsenter` from `sandboxd`. nspawn + BTRFS.

Contents: Repo · Dev tools · tec · Code runs here

**Security invariant:** NO agent code · NO real tokens. Cattle, not pets — created in seconds.

**Credentials Proxy** (`//system/aquifer/credentials-proxy`)

- Sandbox presents JWT → Credentials Proxy returns real tokens
- Sits adjacent to sandbox, on the sandbox-host

**gitd**

- World mirror · shared git objects
- Serves repo content without upstream access

-----

## Phase 0 — Foundations (Weeks 1–3)

### 0.1 Postgres Schema & Migrations

- Tables: `sessions`, `events`, `command_inbox`, `session_leases`, `cell_hosts`
- Indexes optimized for gateway lookup pattern (`session_leases × cell_hosts`)
- Event log schema (append-only, supports cell state reconstruction)
- Connection pooling (PgBouncer or cloud-native)
- Migration tooling (language-agnostic choice)

### 0.2 Infrastructure-as-Code

- Four MIG templates (one per tier)
- VPC + subnets + firewall rules isolating tiers
- GCS bucket for BTRFS snapshots
- Secrets management (Vault / cloud KMS) — celld-only PG credentials
- JWT signing infrastructure (for session-scoped tokens)
- Observability: OpenTelemetry traces, structured logs, per-cell metrics

### 0.3 Monorepo & CI/CD

```
aquifer/
├── proto/                  # gRPC / API definitions
├── gateway/                # Tier 1
├── broker/                 # Tier 2
├── celld/                  # Tier 3
│   ├── runtime/            # Go Runtime
│   └── agent/              # Pi Harness
├── sandboxd/               # Tier 4
│   ├── credentials-proxy/
│   └── gitd/
├── migrations/
├── infra/                  # IaC modules
├── scripts/
└── docs/
```

- Per-component container image builds
- Integration test harness (all tiers collocated on single node)

-----

## Phase 1 — Gateway (Weeks 3–5)

**Goal:** Stateless edge routing any session to the correct cellhost.

### Deliverables

- [ ] Gateway service: Auth · REST · ACP edge · SSE
- [ ] Zero per-session state — all routing via PG lookup (`session_leases × cell_hosts`)
- [ ] Postgres connection (read-only: commands, session routing)
- [ ] Session routing: lookup → proxy to celld (cyan arrow)
- [ ] New session: request placement from broker via lifecycle API (green arrow)
- [ ] SSE streaming for long-lived session connections
- [ ] Health check endpoint, crash-safe (no warmup)
- [ ] MIG auto-scaling policy
- [ ] Load test: 10K concurrent session lookups, p99 < 5ms

-----

## Phase 2 — Broker (Weeks 5–8)

**Goal:** Control plane for registries and placement. Never touches data path.

### Deliverables

- [ ] Cell registry: cellhosts register/heartbeat via WS /ws
- [ ] Sandbox registry: sandbox-hosts register capacity via WS /ws
- [ ] Placement engine: assign sessions to cellhosts, write `session_leases`
- [ ] `create_cell` command: broker → cellhost via WS /ws
- [ ] Lifecycle API: gateway → broker (green arrow)
- [ ] HA: active-passive or leader election
- [ ] Admin API: list cells/sandboxes, drain host, force-migrate session
- [ ] **Verify:** broker is never in session traffic or tool call path

-----

## Phase 3 — Celld + Session Cell (Weeks 8–14)

**Goal:** Core execution tier. This is the hardest phase.

### 3.1 Celld Daemon

- [ ] Register with broker via WS /ws
- [ ] Accept `create_cell` commands
- [ ] Manage session cells as systemd transient units
- [ ] Only Postgres client on the host (DB plane)
- [ ] Session state writes: events, command inbox, leases
- [ ] Graceful drain: stop new cells, wait for existing to complete
- [ ] Survive celld restart (reconnect to existing systemd units)

### 3.2 Go Runtime (inside session cell)

- [ ] Event sequencer: ordered event processing from Postgres event log
- [ ] Tool-call mediation: route tool calls directly to sandboxd (yellow arrow, no broker)
- [ ] ACP server: Unix socket for intra-cell communication
- [ ] Inbox consumer: poll `command_inbox` via celld’s PG connection

### 3.3 Pi Harness (RPC child of Go Runtime)

- [ ] Agent loop: spawned as RPC child process
- [ ] Aquifer extension: Aquifer-specific agent capabilities
- [ ] Side-channel tools: auxiliary tool interfaces
- [ ] Model calls: outbound LLM API calls

### 3.4 Security

- [ ] Session-scoped JWT issuance (short-lived, single session)
- [ ] Cells hold NO Postgres credential, NO model API key, NO upstream secret
- [ ] Validate: cell state fully reconstructable from Postgres event log

### 3.5 Sub-stores

- [ ] Profile Bundles: riverbed · go-review · contexts
- [ ] Memory Store: agent · MRU · elo storage
- [ ] Both reconstructable from event log

-----

## Phase 4 — Sandboxd + Sandbox (Weeks 14–18)

**Goal:** Isolated tool execution. No agent code runs here.

### 4.1 Sandboxd Daemon

- [ ] Register with broker via WS /ws
- [ ] Tool execution RPC server: accept calls from cells (yellow arrow)
- [ ] nspawn sandbox lifecycle: create/destroy
- [ ] BTRFS subvolume management: snapshot, clone, cleanup
- [ ] Snapshot export to GCS
- [ ] nsenter into sandboxes for tool execution

### 4.2 Sandbox Environment

- [ ] nspawn + BTRFS base image
- [ ] Contents: Repo · Dev tools · tec
- [ ] Code runs here (tool code, not agent code)
- [ ] NO agent code, NO real tokens
- [ ] Cattle, not pets — target creation time in seconds
- [ ] Network namespace: no outbound by default

### 4.3 Credentials Proxy

- [ ] JWT → real token exchange
- [ ] Sandbox presents short-lived JWT, proxy returns scoped real credentials
- [ ] Runs on sandbox-host, adjacent to sandboxes
- [ ] Audit logging of all token exchanges

### 4.4 gitd

- [ ] World mirror: shared git objects across sandboxes
- [ ] Serve repo content without giving sandboxes upstream access
- [ ] Efficient deduplication via shared object store

-----

## Phase 5 — Integration & Migration (Weeks 18–22)

### 5.1 End-to-End Validation

- [ ] Full session lifecycle: client → gateway → celld (Go Runtime → Pi Harness) → sandboxd → sandbox → response
- [ ] Tool call path: Pi Harness → Go Runtime → sandboxd → nsenter → sandbox (verify broker NOT in path)
- [ ] Credential flow: sandbox → JWT → Credentials Proxy → real token
- [ ] Cell reconstruction: kill cell, rebuild from Postgres event log
- [ ] Failure scenarios: celld crash, sandboxd crash, gateway crash, PG failover
- [ ] Broker placement under load (100+ concurrent session creates)

### 5.2 Migration from River

- [ ] Dual-write: River + Aquifer Postgres side by side
- [ ] Shadow traffic: replay River sessions, compare outputs
- [ ] Incremental cutover: route N% of new sessions to Aquifer
- [ ] Rollback plan: drain Aquifer → River within SLA

### 5.3 Operational Readiness

- [ ] Runbooks: deploy, scale, drain, incident response
- [ ] Dashboards: per-tier health, session latency, PG pool, placement lag, JWT issuance rate
- [ ] Alerting: cell OOM, sandbox leak, broker placement failure, PG replication lag, credential proxy errors
- [ ] Zone path validation: every component at its `//system/aquifer/*` path

-----

## What This Eliminates (vs. River)

|River Component          |Aquifer Replacement                             |
|-------------------------|------------------------------------------------|
|`containerctld`          |systemd transient units via celld               |
|`gcsfuse`                |BTRFS subvolumes on sandbox-hosts               |
|SSH into sandboxes       |nsenter from sandboxd + direct RPC              |
|Warm container pool      |On-demand nspawn (BTRFS snapshot = seconds)     |
|In-memory broker registry|Postgres tables (`cell_hosts`, `session_leases`)|

-----

## Timeline Summary

|Phase                   |Weeks|Milestone                                       |
|------------------------|-----|------------------------------------------------|
|0 — Foundations         |1–3  |Schema, IaC, CI/CD, JWT infra                   |
|1 — Gateway             |3–5  |Stateless routing operational                   |
|2 — Broker              |5–8  |Placement + registries live                     |
|3 — Celld + Session Cell|8–14 |Go Runtime + Pi Harness running in systemd cells|
|4 — Sandboxd + Sandbox  |14–18|Tool exec + Credentials Proxy + gitd            |
|5 — Integration         |18–22|E2E, migration, go-live                         |