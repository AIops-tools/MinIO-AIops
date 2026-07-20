# Changelog

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
