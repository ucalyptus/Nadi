# Security Model

## Trust boundaries

- Gateway authenticates external clients and forwards session traffic.
- Broker only manages placement and fleet control; it is not a data-plane service.
- Celld is the only Postgres client on a cellhost.
- Session cells hold only short-lived JWTs scoped to one session.
- Sandboxd hosts isolated execution environments and the credentials proxy.

## Secrets handling

- Postgres credentials are held by celld, not cells or sandboxes.
- Model API keys and upstream credentials are never placed in cells or sandboxes.
- Sandboxes present short-lived JWTs to the credentials proxy.
- Credentials proxy returns scoped real tokens and audit-logs every exchange.

## Isolation

- Session cells run as systemd transient units.
- Sandboxes use nspawn + BTRFS and are entered by sandboxd via `nsenter`.
- Sandboxes default to no outbound network access.
- No agent code runs inside sandboxes.

## Required validation

- Kill and reconstruct a cell from the Postgres event log.
- Prove Broker is absent from session and tool-call paths.
- Verify sandboxes do not contain upstream tokens or agent code.
- Validate credential proxy audit logs for every token exchange.
