# MinIO AIops v0.1.0 — preview

Governed AI-ops for **MinIO** object storage for AI agents (MCP) and humans
(CLI), with a bundled governance harness: audit, policy, token/runaway budget,
undo recording, graduated risk tiers.

> **Preview**: behaviour is validated against mocked SDK/HTTP responses; it
> has not been run against a live MinIO server. The fastest live check is a
> single-node server running `minio-aiops doctor`.

## Surface

- **29 MCP tools** (21 read, 8 write) over four access paths: S3 API (official
  SDK), admin API (quota, server info), unauthenticated health endpoints, and
  the cluster metrics endpoint (public or bearer-token auth — the token is
  derived from the stored credentials).
- **Flagship analyses**: `capacity_rca`, `bucket_exposure_audit`,
  `lifecycle_gap_analysis`, `healing_health` — every finding is
  cause + suggested action, thresholds are named constants.
- **Guarded writes**: policy / versioning / lifecycle / quota (reversible,
  prior state captured, undo recorded), `bucket_delete` (high risk, empty-only,
  irreversible), `remove_incomplete_uploads` (age-gated purge, priorState
  only). All writes take `dry_run`; destructive CLI commands double-confirm.

## Governance

- Unified audit log `~/.minio-aiops/audit.db` (relocatable via
  `MINIO_AIOPS_HOME`); CLI writes route through the same governed functions.
- Secure by default: with no `rules.yaml`, high-risk writes require a named
  approver (`MINIO_AUDIT_APPROVED_BY`); `init` seeds an explicit starter
  policy.
- Encrypted secret store (`secrets.enc`, Fernet + scrypt) — no plaintext
  secrets on disk.

## Quality gates (this release)

- 114 tests green (pytest), `ruff check` clean, bandit 0 Medium+ findings.
- Every MCP tool carries the `_is_governed_tool` marker.
- Undo descriptors are replay-tested against the target tool signatures.
