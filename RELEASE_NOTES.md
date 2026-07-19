# Release notes — minio-aiops 0.3.0

Previous release: 0.2.1.

## Fixed: only one of MinIO's three metrics endpoints was being scraped

MinIO splits its Prometheus exposition across `/cluster`, `/node` and `/bucket`, and
the metric names do not overlap. This package consumes 30 metric names and **12 of
them are absent from `/cluster`** — every `minio_node_drive_*` (per-drive capacity),
every `minio_heal_*`, and every `minio_bucket_usage_*`.

The practical effect on a real server: `heal drives` returned an empty list, and
per-bucket capacity readers saw nothing. Confirmed against a live 4-drive erasure set.

`metrics()` now merges `/cluster` + `/node` (both required) and `/bucket`
(best-effort — some deployments disable it, and it is the expensive scrape on a
server with many buckets).

## Fixed: `drive_status` reported a broken scrape as "no drives"

It caught every exception and returned `[]`, so a failed metrics scrape was
indistinguishable from a healthy server with nothing to report. That is how the
bug above stayed invisible for the life of the tool.

**BREAKING** — `drive_status` now returns an envelope instead of a bare list:
`{"drives": [...], "returned": N, "error": str | None}`. A non-null `error` means
the scrape failed; do not read an empty list as "healthy, nothing to see".

## Also

- Byte and object counts are integers again. Prometheus is float-typed on the wire,
  so these rendered as `1500000.0` / `3.0`; absent values stay `null` rather than
  becoming `0`.
- `bucket_exposure_audit` findings now carry an explicit 1-based `rank`. They were
  already ordered worst-first by `riskScore`; the priority is now stated in the
  payload instead of left implicit in list position.

## Live-verified

Against a real **4-drive erasure set**: the erasure-set analysis correctly reported
`LOW_FAILURE_TOLERANCE` (write quorum 3 of 4 — only one more drive may fail), the
exposure audit correctly scored an anonymously-writable bucket `high`, and the
governance loop (`set_versioning` → `undo_apply` restoring `Suspended`) closed on
the live server. See [docs/VERIFICATION.md](docs/VERIFICATION.md) — **multi-node
MinIO and real healing remain unverified.**
