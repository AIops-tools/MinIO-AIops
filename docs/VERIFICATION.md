# Live verification — minio-aiops

`minio-aiops` is published (PyPI, MCP Registry, ClawHub) and its behaviour is
exercised by a **mock-only** test suite. It has **not** yet been validated
end-to-end against a live MinIO server. Until it has, we make no claim that the
SDK call shapes, admin-API paths, and metric names match a real deployment.

This document defines exactly what a live verification run must cover, and the
criteria for recording this tool as live-verified. It is deliberately
checklist-shaped so the result is reproducible and auditable — not a subjective
"seems fine".

## What the mock suite already guarantees

- Every module imports; the CLI builds; every MCP tool carries the
  `@governed_tool` harness marker (`tests/test_smoke.py`).
- Pure analyses (`capacity_rca`, `bucket_exposure_audit`,
  `lifecycle_gap_analysis`, `healing_health`) are unit-tested against synthetic
  cluster state and metric payloads, including their thresholds and rankings.
- Write tools carry the correct risk tier and record the correct inverse undo
  descriptor, tested against a mocked connection.
- `bucket_delete` refuses a non-empty bucket; `remove_incomplete_uploads`
  respects its age safety window.

What it does **not** guarantee: that the `minio` SDK call shapes, the admin-API
paths (bucket quota, server info), the `/minio/health/*` responses, and the
`/minio/v2/metrics/cluster` metric names match a real MinIO build.

## Prerequisites for a live run

**Live verification is cheap here** — MinIO runs in a single container, so this
is a realistic community self-test rather than a lab exercise.

```bash
docker run -d --name minio-verify -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=verifyuser -e MINIO_ROOT_PASSWORD=verifypassword \
  quay.io/minio/minio server /data --console-address ":9001"

uv tool install minio-aiops
minio-aiops init      # endpoint + access key; secret key stored encrypted
```

Use a **throwaway server and throwaway buckets** you are willing to reconfigure
and destroy. Never verify against production object storage.

Note the coverage limit of a single-node server: erasure-set and heal findings
need a **multi-drive / distributed** deployment to observe for real. A
single-node run can still tick everything else; record the erasure-set boxes as
an explicit gap rather than a pass.

## Verification checklist

Tick every box. A box that cannot be ticked is a verification gap — record it,
do not silently pass.

### 1. Connectivity (the fastest live gate)
- [ ] `minio-aiops doctor` → all green: config, encrypted secret store, health
      endpoints reachable, S3 auth accepted, metrics endpoint scrapable in the
      configured auth mode (`jwt` bearer by default, or `public`).

### 2. Reads return real, well-shaped data
- [ ] `minio-aiops bucket ls` → the actual buckets, with populated names and
      creation dates.
- [ ] `minio-aiops bucket info <bucket>` → policy, versioning, lifecycle,
      encryption, quota and tags match what the MinIO Console shows.
- [ ] `minio-aiops health check` / `health status` → live/ready/cluster
      responses parse; no crash on fields the server omits.
- [ ] `minio-aiops capacity rca` → the used/total figures match the Console;
      thresholds fire on a deliberately filled bucket and not otherwise.
- [ ] `minio-aiops capacity usage` → per-bucket sizes are plausible against
      what you uploaded.
- [ ] `minio-aiops bucket audit` → a deliberately public-read bucket is flagged
      with the right finding code; a private one is not (no false positive).
- [ ] `minio-aiops bucket ilm-gap` → after leaving noncurrent versions and an
      aborted-mid-flight multipart upload, both are detected with a sane
      reclaimable estimate.
- [ ] `minio-aiops bucket uploads <bucket>` → the incomplete upload is listed
      (this uses the SDK's core ListMultipartUploads call — a prime candidate
      for drift against a real server).

### 3. A reversible write + its undo (governance closes the loop)
- [ ] `minio-aiops bucket versioning-set <bucket> Enabled --dry-run` → prints
      the API call, changes nothing.
- [ ] `minio-aiops bucket versioning-set <bucket> Enabled` → versioning is
      actually enabled on the server; the result carries an `_undo_id`; a row
      lands in `~/.minio-aiops/audit.db`.
- [ ] `minio-aiops undo list` → the token is present with the expected inverse.
- [ ] `minio-aiops undo apply <id>` → versioning returns to its **prior** state.
- [ ] `minio-aiops bucket lifecycle-set <bucket> --noncurrent-days 30
      --abort-days 7` then `undo apply` → the **prior** lifecycle config is
      restored, not an empty one (proves undo captured pre-state, not a guess).
- [ ] Same round-trip for a bucket policy (`bucket policy-set` → `undo apply`
      restores the exact prior policy JSON) and a quota (`bucket quota-set`).

### 4. Governance actually gates
- [ ] With no `~/.minio-aiops/rules.yaml`, a `high`-risk op
      (`minio-aiops bucket delete <empty-bucket>`) is **refused** unless
      `MINIO_AUDIT_APPROVED_BY` is set — secure-by-default.
- [ ] With the approver set, the same op succeeds and is audited with the
      approver and rationale recorded.
- [ ] `bucket delete` against a **non-empty** bucket is refused even with an
      approver (the emptiness re-check runs at execution time, and counts
      versions, delete markers and incomplete uploads).
- [ ] A tight poll loop trips the runaway budget guard rather than hammering
      the server.
- [ ] A failed operation is audited with `status=error` and does **not** record
      an undo token.

### 5. Erasure-set / healing (needs a multi-drive deployment)
- [ ] `minio-aiops heal status` → per-erasure-set online drives, write quorum
      and `failureToleranceRemaining` match reality.
- [ ] `minio-aiops heal drives` / `heal nodes` → an intentionally stopped drive
      or node shows up correctly, and `WRITE_QUORUM_AT_EDGE` fires only when it
      genuinely applies.

### 6. Cleanup
- [ ] Delete the test buckets; confirm each delete is audited and tagged
      `high`.
- [ ] `docker rm -f minio-verify` and remove the throwaway credentials from the
      secret store (`minio-aiops secret rm <name>`).

## Criteria to consider it live-verified

Record this tool as live-verified **only when all of the following hold**:

1. Every checklist box in sections 1–4 and 6 is ticked against at least one
   real MinIO server, and the server version is recorded (e.g. "verified on
   MinIO RELEASE.2025-xx-xx").
2. Section 5 is either ticked against a multi-drive deployment, or recorded as
   an explicit, named gap — never quietly skipped.
3. Any field-shape or metric-name mismatch found during the run is fixed and
   covered by a regression test.
4. The run is written up in this repo's release notes with the date and
   version, matching how the line records its other live-verified tools.

Until then this document stands as the accurate statement of status.

## Notes for maintainers

- `minio-aiops doctor` is the single fastest live entry point; start there.
- The metrics auth mode is the most likely first failure: the default `jwt`
  mode derives its bearer token from the stored credentials, so a doctor
  failure there usually means a credential problem, not a metrics problem.
- The verification story for the whole product line is tracked centrally; add
  this tool's result there once green so the verification-debt ledger stays
  accurate.
