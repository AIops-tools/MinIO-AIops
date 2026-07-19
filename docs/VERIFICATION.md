# Live verification status

This document records what has and has not been validated against a real MinIO
server, so the maturity claim is auditable rather than a vibe.

## Already live-verified ✅ — single-node, and a real 4-drive erasure set (2026-07-19/20)

Two live runs: a single-drive server, then a **4-drive erasure set** (one pool,
one erasure set, stripe size 4, write quorum 3).

- `doctor`: liveness/readiness, S3 API authenticated, metrics endpoint reachable.
- Reads against the live server: `overview`, `health check/status`, `bucket ls`,
  `capacity usage`, `capacity rca`, `heal status/drives`.
- **Exposure audit found the real risk**: it scored an anonymously-writable bucket
  `high` (riskScore 105, worst-first, now carrying an explicit `rank`) and named
  `PUBLIC_WRITE_POLICY` with a concrete remediation.
- **Erasure-set analysis is correct on real topology**: with 4 drives and write
  quorum 3 it reported `LOW_FAILURE_TOLERANCE` — "can tolerate only 1 more drive
  failure" — which is exactly right, and correctly made `healthy: false`.
- Governance loop: `set_versioning` really enabled versioning on the live bucket,
  captured `Off` as `priorState`, and `undo_apply` restored it to `Suspended` —
  the correct S3 inverse, since a bucket cannot return to `Off`.
- Read-only mode: 31 tools → 22, removing exactly the 8 bucket writes plus
  `undo_apply` (correctly classed as a write, since it replays a mutation).

### Two real bugs found by the erasure-set run — both silent

1. **Only one of three metrics endpoints was being scraped.** MinIO splits its
   Prometheus exposition across `/cluster`, `/node` and `/bucket`, and the names do
   not overlap. This package consumes 30 metric names; **12 of them are absent from
   `/cluster`** — every `minio_node_drive_*` (per-drive capacity), every
   `minio_heal_*`, and every `minio_bucket_usage_*`. So `heal drives` returned `[]`
   on every real server, and per-bucket capacity was empty. Fixed: `metrics()` now
   merges `/cluster` + `/node` (required) and `/bucket` (best-effort, since some
   deployments disable it and it is the expensive scrape).
2. **`drive_status` swallowed every scrape error and returned `[]`** — which is why
   bug 1 stayed invisible for the life of the tool: "no drives" looked like success.
   It now returns an envelope with an explicit `error`, so a broken probe can never
   be read as a healthy empty server.

Also: Prometheus values are float on the wire, so byte and object counts rendered
as `1500000.0` / `3.0`; these are now integers, with absent staying `null`.

## Not yet live-verified ⚠️

- **Multi-node (distributed) MinIO** — the erasure set was verified on a
  single node with four drives. Cross-node healing, node-down handling, and
  `heal nodes` against a genuinely offline peer are still untested.
- **Actual healing**: the `minio_heal_*` counters only appear once a heal has run;
  they read `null` here (correctly — unknown, not zero). Nothing has been verified
  against a real degraded drive being rebuilt.
- **Lifecycle and quota writes** (`set_lifecycle`, `set_bucket_quota`) and their
  undo paths.
- **Versioned-object accounting** in `capacity rca` (needs noncurrent versions).
- **TLS-secured endpoints** — both verified instances ran plaintext on a lab port.
