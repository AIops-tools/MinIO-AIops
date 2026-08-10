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

- ~~**Multi-node (distributed) MinIO**~~ — **closed 2026-08-03 against a real
  4-node distributed deployment, and it found three defects** (all fixed):
  `minio_cluster_nodes_online_total` is exported by both `/cluster` and
  `/node`, so summing the concatenated endpoints reported **8 nodes online on a
  4-node cluster** (29 metric names overlap; the merge now de-duplicates by
  `(name, labels)`); `heal drives` listed **1 of 4 drives** as `returned: 1`
  with no way to tell a one-drive deployment from three unseen servers; and the
  heal counters rendered as floats. **Node-down handling is correct**: with one
  node stopped, `nodesOnline: 3 / nodesOffline: 1`, `drivesOffline: 1`, and the
  erasure set reported 3 online drives against a read quorum of 2.
- ~~**Actual healing**~~ — **closed 2026-08-03**: stopping a node produced real
  `minio_heal_*` counters (objects scanned and healed climbing while the
  deployment repaired itself), which is the state the previous round could only
  report as `null`.
- ~~**Quota writes** (`set_bucket_quota`) and undo~~ — **verified 2026-08-03**
  against a real MinIO (Docker). `quota-set vbucket 5242880` set a 5 MiB hard
  quota (`mc quota info` confirmed), captured `quotaBytes: 0` as `priorState`,
  and `undo apply` cleared it back to `0 B` (`mc` confirmed). Audit rows written.
- ~~**Lifecycle writes** (`set_lifecycle`)~~ — **`--expire-days` and
  `--noncurrent-days` verified 2026-08-03** (rules applied and round-tripped via
  `mc ilm rule ls`, undo restores the prior config XML).
  Undo restores the prior configuration (verified: after undo the bucket had no
  lifecycle configuration again, its pre-write state).
  **`--abort-days` was found completely broken and has been REMOVED.** MinIO
  **rejects any lifecycle rule whose only action is
  `AbortIncompleteMultipartUpload`** (schema-validation 400), so the tool's
  standalone abort rule failed on every real MinIO — and using `--abort-days`
  alongside `--expire-days` failed the *whole* request, taking the working knobs
  down with it. Worse, even when the abort action is attached to an expiration
  rule (which the server accepts), **MinIO does not echo it back on
  GetBucketLifecycle** — confirmed on both `RELEASE.2025-08` and
  `RELEASE.2024-01`, MinIO's own `mc ilm import` → `mc ilm export` loses it too,
  and `mc ilm rule add` exposes no abort knob at all. The knob therefore cannot
  be honoured round-trippably through the S3 lifecycle API, so it is gone from
  the CLI, the MCP tool and the ops/connection layers rather than left to look
  like it works. Abandoned uploads are reclaimed with `remove_incomplete_uploads`
  (the S3 multipart abort), which is verifiable; `ilm-gap`'s suggested action now
  says so instead of recommending the impossible rule.
  **A mock had encoded the defect as the spec**: `test_set_bucket_lifecycle_builds_all_three_rules`
  asserted the three-rule shape MinIO rejects. Replaced with a two-rule assertion
  plus a test pinning the knob's absence and the reason.
- ~~**Versioned-object accounting** in `capacity`~~ — **verified 2026-08-03** on a
  bucket with noncurrent versions (obj.txt at 3 versions + single.txt). The
  numbers are accurate: `usedBytes: 118` is version-inclusive (matches MinIO's
  `total_bytes` and `mc du --versions`), `objects: 2` is the current-object count
  (matches `object_total`). **Enhancement made this run**: `usage_by_bucket` now
  also surfaces `versions` (4) and `deleteMarkers` (0) from
  `minio_bucket_usage_version_total` / `_deletemarker_total`, so noncurrent-version
  overhead — the thing a noncurrent-expiration rule reclaims — is visible instead
  of hidden inside the version-inclusive `usedBytes`.
- ~~**TLS-secured endpoints**~~ — **closed 2026-08-10** against a MinIO serving
  HTTPS with its own certificate (plain HTTP refused, 400). With
  `verify_ssl: true` the health probe *and* the S3 call both fail with
  `CERTIFICATE_VERIFY_FAILED`, so verification is genuinely being performed
  rather than configured; with it off, `doctor` reports live + ready + S3
  authenticated + metrics reachable, and a full governed loop ran over TLS:
  `versioning-set Enabled` → confirmed `enabled` with `mc` → `undo apply` →
  confirmed `suspended` (the correct S3 inverse — a versioned bucket cannot
  return to `Off`).
