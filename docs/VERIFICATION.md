# Live verification status

This document records what has and has not been validated against a real MinIO
server, so the maturity claim is auditable rather than a vibe.

## Already live-verified ✅ — MinIO (RELEASE.2026-era image), 2026-07-19

Exercised end-to-end against a real **MinIO** server (Docker) with three
buckets, uploaded objects, and one bucket deliberately made anonymously
readable+writable:

- `doctor` against a live server: liveness/readiness probes, S3 API
  authenticated, metrics endpoint reachable — all three checks green.
- Reads ran clean against the live server: `overview`, `health check/status`,
  `bucket ls`, `capacity usage`, `capacity rca`, `heal status/drives`.
- **Exposure audit found the real risk**: `bucket exposure_audit` scored the
  anonymous bucket `high` (riskScore 105, worst-first) and named
  `PUBLIC_WRITE_POLICY` with a concrete remediation, alongside
  `NO_DEFAULT_ENCRYPTION` / `VERSIONING_OFF` / `NO_LIFECYCLE`. The truncation
  envelope (`bucketsAudited` / `bucketsTotal` / `limit` / `truncated`) was
  present and correct.
- Governance loop end-to-end: `set_versioning` really enabled versioning on the
  live bucket, captured `Off` as `priorState`, and `undo_apply` restored it to
  `Suspended` — the correct S3 inverse, since a bucket cannot return to `Off`
  once versioning has been enabled.
- Read-only mode: `MINIO_READ_ONLY=1` took the registry from 31 tools to 22,
  removing exactly the 8 bucket writes plus `undo_apply` (correctly classed as
  a write, since it replays a mutation) while leaving every read — including
  `bucket_exposure_audit` — in place.

## Not yet live-verified ⚠️

- **Distributed / multi-node MinIO** — the verified instance was single-node,
  so `heal drives/nodes` had no real erasure-set topology to report and
  healing was never exercised against an actual degraded drive.
- **Lifecycle and quota writes** (`set_lifecycle`, `set_bucket_quota`) and
  their undo paths.
- Versioned-object accounting in `capacity rca` (needs noncurrent versions).
- TLS-secured endpoints (the verified instance ran plaintext on a lab port).
