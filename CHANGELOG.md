# Changelog

## Unreleased

### Fixed
- **A cluster-wide gauge was double-counted, reporting 8 nodes online on a 4-node cluster.** 29 metric names are exported by **both** `/cluster` and `/node`, including `minio_cluster_nodes_online_total`, and the two endpoints were concatenated — so every aggregate over an overlapping name doubled. The merge now skips a `(name, labels)` series it has already absorbed, which cannot drop real data because two genuinely different series never share both. Measured on a real 4-node distributed MinIO.
- **Per-drive and per-node listings say they cover one server.** `heal drives` on that same 4-node cluster listed **1 drive** and reported `returned: 1`, indistinguishable from "this deployment has one drive". `heal drives` and `heal nodes` now carry `scope: "node"`, and drives additionally reports `clusterDrivesOnline` with a note when the deployment has more drives than the queried server can see.
- **Heal counters are ints.** `healObjectsScanned`, `healObjectsHealed`, `healBacklogObjects`, `healErrors` and `clusterHealthStatus` rendered as floats (`4.0` objects scanned) while `drivesOffline` in the same payload was an int.
- **`undo apply` replays against the target the original write ran on.** It dispatched the inverse against whatever target the *caller* named — in practice the config's first entry — while the write's own target sat unused in the undo record. On a multi-target config the inverse therefore ran against the wrong host; it only looks harmless because the resource usually is not there, but two hosts holding the same name and the inverse **succeeds on the wrong one, silently**. An explicitly named target still wins. Line-wide: all 24 copies had the identical defect. Caught live in container-host-aiops, where a stop recorded against a Podman target replayed against a Portainer one.

## v0.6.0 — 2026-08-02

### Changed (BREAKING)
- **Requires MCP SDK 2.0** (`mcp[cli]>=2.0,<3.0`). `mcp.server.fastmcp` no longer exists in 2.0; the server is now built with `MCPServer` and reports its package version in the stdio handshake.

### Fixed
- **`undo apply` works from the CLI.** Every write tool is imported lazily inside its own CLI command, so a CLI-driven undo ran in a process where the inverse tool was never registered and failed with "inverse tool is not registered" — for every write tool. Only the MCP entry point, which imports the whole server, worked. Found while live-verifying against a real cluster.
- **An undetermined outcome is audited `unknown`, not `ok`.** The harness only classified a result as undetermined when the payload *also* carried an `error` key, so a write that looked successful but had not been confirmed was recorded as a success.
- **`as_int` no longer round-trips integers through float64**, which cannot represent values above 2**53 exactly. A line-wide sweep found only one of six vendored copies had actually been fixed after the original precision bug. A bool is treated as non-numeric (`None`, matching this tool's unknown-vs-zero contract) rather than being returned unchanged — `bool` subclasses `int`.


## v0.5.0 — 2026-07-21

### Changed (BREAKING)
- **Removed the authorization layer** — read-only mode, the approver gate, and rules.yaml deny are gone. The skill no longer decides read vs write; that is the agent's judgement or the connecting account's permissions. `<PREFIX>_READ_ONLY` now has no effect (a startup warning is logged); `<PREFIX>_AUDIT_APPROVED_BY`/`_RATIONALE` are optional audit annotations.
- The retained guarantee is **unbypassable audit over MCP and CLI alike** — no unaudited entry point. Harness = audit + runaway safety guard + undo + sanitize; `risk_level` is a descriptive audit label, not a gate.

See RELEASE_NOTES.md for tool-specific changes.


## v0.4.0 — 2026-07-20

### Fixed
- **`set_bucket_policy` refuses a policy that denies this tool its own `PutBucketPolicy`.** An explicit Deny beats any allow, so such a policy made its own undo un-appliable — and this tool has no IAM surface, so a bucket policy is the only way it can revoke its own access.
- Harness: a write whose response is lost is audited `status=unknown`, not `error` — it may have taken effect. Undo tokens gain `effectVerified` (undo.db migrated in place).
- Harness: a dry-run no longer records an undo token, and no longer requires a named approver. Guards now run on the preview path.
- Truncated strings end in an ellipsis instead of being cut silently; error messages are capped at 800 chars, not 300.

See RELEASE_NOTES.md for the full detail.

All notable changes to **minio-aiops** are documented here.

## v0.1.0 — 2026-07-17

Initial preview release: governed AI-ops for **MinIO** object storage over the
S3 API (official SDK), the admin API, the unauthenticated health endpoints,
and the cluster metrics endpoint — with a bundled governance harness.

### Highlights

- **Four flagship analyses** (cause + suggested action, thresholds as named
  constants):
  - `capacity_rca` — usable capacity vs used, offline drives/nodes, per-drive
    hotspots, fill imbalance.
  - `bucket_exposure_audit` — ranked findings: anonymous/public policy
    statements (read/write), missing default encryption, versioning off, no
    lifecycle.
  - `lifecycle_gap_analysis` — unbounded noncurrent versions, incomplete
    multipart uploads with no abort rule, large buckets with no lifecycle —
    with a labelled reclaimable estimate.
  - `healing_health` — heal backlog/errors + per-erasure-set write-quorum
    risk and remaining drive-failure tolerance.
- **29 MCP tools** (21 read, 8 write), every one wrapped with the bundled
  `@governed_tool` harness (audit / budget / risk-tier / undo).
- **Guarded writes**: `set_bucket_policy`, `delete_bucket_policy`,
  `set_versioning`, `set_lifecycle`, `delete_lifecycle`, `set_bucket_quota`
  (all reversible — real prior state captured, undo recorded);
  `bucket_delete` (high risk, refused unless verifiably empty, irreversible);
  `remove_incomplete_uploads` (age-gated, priorState only). All writes take
  `dry_run`.
- **CLI**: `init` wizard (encrypted secret store, TLS prompts with lab hints,
  seeds a secure-by-default `rules.yaml`), `doctor` (live/ready + S3 auth +
  metrics reachability), `overview`, read groups (`health`, `capacity`,
  `heal`, `bucket`) and guarded write commands with `--dry-run` +
  double-confirm, delegated to the governed MCP twins (CLI writes are
  audited).
- **Metrics auth, both modes**: `MINIO_PROMETHEUS_AUTH_TYPE=public` scraped
  directly; default `jwt` mode uses a bearer token derived from the stored
  credentials.
- **Encrypted credentials**: secret key in `~/.minio-aiops/secrets.enc`
  (Fernet + scrypt), master password via `MINIO_AIOPS_MASTER_PASSWORD`;
  legacy `MINIO_<TARGET>_SECRET_KEY` env fallback with `secret migrate`.

### Known limitations

- **Preview / mock-only** — validated against mocked SDK/HTTP responses, not
  yet against a live server. Fastest live check: a single-node MinIO server
  running `minio-aiops doctor`.
- Incomplete-upload listing uses the SDK's core ListMultipartUploads call
  (the public alias was removed upstream).
- Out of scope for v0.1.0: site replication, object locking/legal hold, IAM
  (users/policies) management, remote tiering.
