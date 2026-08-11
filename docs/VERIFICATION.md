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

## Object lock / WORM ✅ — live-verified 2026-08-11 (RELEASE.2025-09-07)

The whole object-lock surface (8 tools) was exercised against a real MinIO, with
**`mc` as ground truth** rather than the tool's own read-back, and with the
enforcement checked functionally rather than by reading configuration.

What was proven, not asserted:

- **Object lock is enabled at creation only.** `bucket_create --object-lock`
  produced a bucket reporting `objectLockEnabled: true, versioning: Enabled`
  (read back, not echoed); the control bucket created without the flag reported
  `false` / `Off`.
- **The false-safety case is real and now visible.** On a lock-enabled bucket
  with **no default rule**, a version was permanently deleted with **no bypass
  permission at all** — while MinIO's own `mc retention info` describes that same
  bucket as "Object locking is not enabled." The tool separates the two states
  (`objectLockEnabled: true` + `defaultRetention: null`) and says in the payload
  that uploads omitting a retention header are unprotected.
- **COMPLIANCE is unliftable, including by root with `--bypass`.** Clearing,
  downgrading to GOVERNANCE, and deleting the version were each refused with
  "Object is WORM protected and cannot be overwritten". GOVERNANCE, by contrast,
  yields to `mc retention clear` and to `mc rm --bypass --version-id`. This is
  the claim the tool's refusal messages rest on, so it was measured both ways.
- **The local shortening guard matches the server exactly.** The call the tool
  refuses (shorten / downgrade retention in force) is the same one `mc` could not
  make without `--bypass`. So the guard is not over-refusing — it returns the
  reason, and the out-of-band remedy, before the round trip.
- **Retention protects the version, not the key.** A plain `mc rm` on a retained
  object always succeeded, writing a delete marker; only `--version-id` was
  refused. Hence `versionDestroyable` + `deleteMarkerStillPossible` in the
  payload rather than a flat `deletable`.
- **Governed loops closed against the server**: `set_default_retention` → `mc`
  confirms `GOVERNANCE 45DAYS` → a fresh upload inherits it → `undo_apply` clears
  the rule → **the already-written object keeps its inherited retention**, which
  is exactly what the write's note promises. `set_legal_hold on` → `mc legalhold
  info` ON → `undo_apply` → OFF.
- **Audit fidelity**: every refusal recorded `status=error`, every success `ok`,
  tiers `critical→review` / `high→review` / `medium→confirm`, and **4 retention
  writes produced 0 undo tokens** while the reversible writes produced one each.
- The `diagnose_retention_gaps` contradiction finding was raised against state
  seeded by `mc` (default retention 365 days + a 30-day expiry rule), reporting
  `expirationDays: 30`, `defaultRetentionDays: 365`, `shortfallDays: 335`.

Still unverified on this surface:

- **Governance bypass through this tool** — deliberately absent, not untested:
  the `minio` SDK sends no bypass header, so the tool cannot shorten or remove
  retention at all and does not pretend to.
- **A non-root credential** carrying `s3:BypassGovernanceRetention` explicitly.
  The live run used the root credential, which bypasses policy evaluation; the
  distinction does not affect what the tool does (it never sends the header)
  but it means the *permission* boundary was not exercised, only the WORM one.
- **Retention interaction with replication and tiering**, both out of scope.

## IAM ✅ — live-verified against MinIO RELEASE.2025-09-07 (2026-08-11)

Verified against a real server with `mc` as ground truth. The five checks this
section previously listed as outstanding are done; two remain, noted at the end.

**1. The field shapes — the biggest risk, and the guess was right for a reason.**
The parser accepted `policyName` / `policy` / `policies` and string-or-list
defensively. The real responses:

```
user_list  → {"alice": {"policyName": "readonly", "status": "enabled"},
              "bob":   {"status": "enabled", "memberOf": ["devs"]},
              "carol": {"status": "enabled"}}
group_info → {"name": "devs", "status": "enabled",
              "members": ["bob"], "policy": "readwrite"}
```

**Users carry `policyName`; groups carry `policy`.** That inconsistency is exactly
what the defensive read existed for — had only `policyName` been handled, bob's
group-inherited policy would have vanished and he would have been reported
`NO_EFFECTIVE_POLICY`, a confident false alarm on a correctly configured account.
Tool output matched `mc` for all three users and the group.

**2. `attach_policy` / `detach_policy` exist on this build.** `attach` succeeded,
`mc admin user list` confirmed `carol → readonly`, `undo_apply` replayed the
detach, and `mc` confirmed carol had no policy again. Full governed loop closed.

**3. The root credential is not an IAM user.** `iamroot` is absent from
`user_list`, so the "empty list is a real state" note is correct.

**4. `create_user` is an upsert — proven functionally, not from documentation.**
Re-creating `dave` with a different secret: the **old** secret then failed with
`SignatureDoesNotMatch` (so it really was replaced) and the **new** one with
`AccessDenied` (so it authenticated). The undo was recorded for the first,
genuinely new account and **not** for the upsert, as designed.

**5. Self-lockout holds against the real credential.** All four user-targeting
writes aimed at `iamroot` were refused with exit 1 — `user-status`, `user-remove`,
`policy-detach`, `policy-attach` — and the refusal fires under `--dry-run` too.

**Bonus: `NO_EFFECTIVE_POLICY` is a real functional condition.** The same test
proved the flagship finding rather than inferring it: `dave` authenticated with
the correct secret and still received `AccessDenied`, because MinIO denies by
default. An account with no policy really can do nothing, and `carol` (no policy)
versus `bob` (policy via group) were discriminated correctly — carol flagged,
bob not.

**Audit fidelity on real data.** Failed calls recorded `status=error` (never a
false `ok`), tiers were `medium→confirm`, and the secret was stored as
`{"access_key": "dave", "secret_key": "***"}`. A byte search of `audit.db`,
`undo.db` **and their WAL files** found none of the four secrets used.

### Still outstanding on this surface

- **The `set_user_status` disable→undo→enable loop and `remove_user`** were not
  completed: the lab host dropped off the network mid-test. The disable attempt
  correctly surfaced the connection failure as an error rather than a false
  success, which is the behaviour under transport loss, but the enable/disable
  effect on a live account is unconfirmed.
- **Groups are read-only here**; group-membership writes remain out of scope.

## (superseded) IAM ⚠️ — the pre-verification status

The IAM surface (9 tools) ships mock-tested only. Stated plainly because this
line's record is unambiguous: **every tool pointed at a real server produced at
least one defect the mocks could not see.** The lab MinIO went offline before
this surface could be exercised, so nothing below is a live claim.

What the 34 mock tests do guarantee:

- every user-targeting write refuses the tool's own access key, and nothing
  reaches the connection when it does;
- the guard is a pure local comparison, so it fires under `dry_run` identically —
  asserted by calling each write with `dry_run=True` against the own key;
- no secret appears in any returned payload (asserted by serialising the result
  and searching for the secret), and `create_user` declares
  `sensitive_params=["secret_key"]`;
- `remove_user` records no undo descriptor, and `create_user` records none when
  it replaced an existing key's secret;
- every undo descriptor is **replayed** against a mocked connection, so a
  signature mismatch fails here rather than in an incident;
- group-inherited policies are resolved before judging "no effective policy".

What a live run must still check:

1. **The actual shapes MinIO returns.** `user_list`, `group_info` and
   `policy_list` are parsed defensively (`policyName` vs `policy` vs `policies`,
   string vs list) precisely because the real shapes are unconfirmed — that
   defensiveness is a guess until measured, and a wrong guess yields empty
   policy lists, which `diagnose_iam_exposure` would then report as
   `NO_EFFECTIVE_POLICY` for every account. **Cross-check every field against
   `mc admin user list` / `mc admin policy list` output.**
2. **Whether `attach_policy`/`detach_policy` exist on the target build.** The SDK
   exposes both `attach_policy` (newer) and `policy_set` (older); only the newer
   pair is used. A MinIO old enough to lack the attach/detach endpoints would 404
   on every policy write — the same shape as the HAProxy Data Plane API v2/v3
   split in proxy-aiops, which was invisible until a live run.
3. **Root-vs-IAM behaviour.** The self-lockout guard compares access keys, which
   is correct for both cases, but the claim that "the root credential never
   appears in user_list" is from MinIO's documentation, not from measurement.
4. **That `create_user` really is an upsert** on an existing key (the undo is
   suppressed on that basis).
5. **A full governed loop per write**: real change → `mc` confirms → `audit.db`
   row → `undo_apply` → `mc` confirms the reversal.

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
