# Changelog

## v0.10.0 — 2026-08-11

### Added
- **Object lock / WORM governance — 8 new tools** (`bucket_lock_config`, `object_lock_status`, `diagnose_retention_gaps`, `bucket_create`, `set_default_retention`, `clear_default_retention`, `set_object_retention`, `set_legal_hold`), previously listed as out of scope. Two design points carry most of the value:
  - **The two ways a bucket can have "no retention" are kept apart.** `objectLockEnabled: false` means lock was never enabled, and since S3 accepts the flag only at bucket **creation** it never can be — the only route is a new bucket plus a migration. `objectLockEnabled: true` with `defaultRetention: null` means WORM is available but an upload that omits its own retention header is retained for nothing, while every dashboard and audit questionnaire reads "object lock: enabled" as "data is protected". Collapsing those into one falsy value is what makes an audit call an unprotected bucket protected. Demonstrated end to end against a live MinIO: on a lock-enabled bucket with no default rule, a version was permanently deleted with no bypass permission at all. MinIO's own `mc retention info` reports that bucket as "Object locking is not enabled."
  - **`set_object_retention` records no undo token, because none can exist.** S3 refuses to shorten or remove retention without `x-amz-bypass-governance-retention`, and the `minio` SDK never sends that header (checked in its source), so no credential can walk it back through this tool. An undo token here would be one whose replay is guaranteed to fail while the audit row claimed the write was reversible. The tool instead refuses, before the round trip, any call that would shorten or downgrade retention already in force, and requires `acknowledge_irreversible=True` for COMPLIANCE. Both refusals fire under `dry_run`.
- **`diagnose_retention_gaps`** reports contradictions rather than a house style, worst-first with an explicit `rank`. The flagship finding is arithmetic: a lifecycle rule expiring objects after 30 days on a bucket whose default retention holds them for 365 **can never delete anything**, so the capacity it was added to reclaim never returns — both day counts and the shortfall are in the payload, so the finding is checkable rather than asserted. Buckets whose probes fail are listed in `bucketErrors` rather than skipped.
- **`object_lock_status` reports `versionDestroyable`, not `deletable`.** Measured against a live server: a plain `DELETE` on a retained key **always succeeds** on a versioned bucket — it writes a delete marker, the key stops appearing in listings, and the retained version is untouched. Only deleting that *version* is refused. "deletable: false" would have told a caller the object cannot be deleted when anyone can make it disappear from view a second later; what retention buys is that the bytes survive, and the payload now says exactly that.

### Verified
Against a live MinIO (`RELEASE.2025-09-07`), with every claim cross-checked against `mc` as ground truth rather than the tool's own read-back:
- **COMPLIANCE really is unliftable**, including by the root credential with `--bypass`: clearing, downgrading to GOVERNANCE, and deleting the version were all refused with "Object is WORM protected and cannot be overwritten". GOVERNANCE yields to `mc retention clear` and to `mc rm --bypass --version-id`.
- **The shortening guard matches the server exactly** — the same call `mc` could not make without `--bypass` ("Object is WORM protected") is the one this tool refuses locally, so it is not over-refusing; it just returns the reason before the round trip.
- Full governed loops: `set_default_retention` → `mc` confirms `45DAYS` → a new upload inherits it → `undo_apply` clears the rule → **the already-written object keeps its inherited retention**, exactly as the write's note promised. `set_legal_hold` on → `mc legalhold info` ON → `undo_apply` → OFF.
- The audit trail is faithful: refusals recorded `status=error`, successes `ok`, tiers `critical→review` / `high→review` / `medium→confirm`, and **4 writes produced only 3 undo tokens** — the retention write recorded none.

### Fixed
- **A failed read-back after `bucket_create` was swallowed.** The call reports whether the server actually enabled object lock; if that read failed, `objectLockEnabled` became `null` with no reason given — indistinguishable from "the server said no", which is the one question the call exists to settle, and object lock cannot be added afterwards. It now carries `readBackError` and says the state is unknown rather than false (bug class #3).
- **`diagnose_retention_gaps` scanned every bucket without a cap**, three calls each, while its sibling analyses (`bucket_exposure_audit`, `lifecycle_gap_analysis`) cap and report what they did not reach. It now takes `max_buckets` (default 100) and reports `bucketsTotal` / `bucketsTruncated` with a note, so an unexamined remainder is never read as a clean one.
- **A refusal message named a flag that does not exist.** The remedy for retention already in force pointed at `mc retention clear --bypass`; that command has no `--bypass` flag and errors with "flag provided but not defined" — while plain `mc retention clear` does lift GOVERNANCE retention for a privileged credential (and is refused for COMPLIANCE). An error message that hands the operator a rejected flag sends them to debug the wrong thing.
- **The CLI preview of the one irreversible write was less informative than the MCP one.** `lock retention-set --dry-run` echoed back only the arguments, omitting the computed `retainUntil` and the `reversible: false` the MCP caller already received — the two facts a preview of a permanent write exists to deliver.

## v0.9.0 — 2026-08-10

### Fixed
- **An undetermined outcome no longer exits as a plain failure.** A write whose response was lost carries *both* `error` and `outcomeUnknown`, and the harness deliberately judges unknown first when writing the audit row — the change may have taken effect, so a blind retry could apply it twice. The CLI guard judged `error` first, so the audit said "may have taken effect" while the exit status told a script it had not happened. The two layers now agree (exit 2, not 1), and a test pins the ordering so it cannot silently flip back.
- **The CLI reported a refused or failed governed write as a success.** 7 write call sites printed the governed twin's payload and exited **0** whatever it said — and `@tool_errors` flattens every refusal, guard rejection and upstream failure into `{"error": ...}` rather than raising, so nothing downstream of a `&&` chain or a CI step could tell a blocked write from a landed one. The dry-run path already exited non-zero, which made the asymmetry worse: the preview was stricter than the write it previews. Results now route through a `checked()` helper — exit 1 on an error payload, exit 2 on an undetermined outcome, unchanged on success. This defect class had been fixed repo-by-repo several times and kept coming back; an audit across the whole line found it live in **18 of the 24 tools at once (87 call sites)**, so each tool now carries an invariant test that fails if any future CLI command prints a governed result without checking it.

### Verified
- **TLS-secured endpoints** are now live-verified (they had only ever been exercised on plaintext lab ports): with `verify_ssl: true` against a self-signed MinIO both the health probe and the S3 call fail with `CERTIFICATE_VERIFY_FAILED`, and with verification off a full governed loop — `set_versioning` → server confirmed → `undo_apply` → server confirmed — completed over HTTPS.

## v0.8.0 — 2026-08-10

### Removed (BREAKING)
- **`set_lifecycle`'s abort-incomplete-uploads knob is gone** (`--abort-days` on the CLI, `abort_incomplete_days` on the MCP tool and ops layers). MinIO cannot honour it: a lifecycle rule whose only action is `AbortIncompleteMultipartUpload` is refused with a schema-validation 400 — so the standalone rule this tool built **failed on every real MinIO server** — and combining `--abort-days` with `--expire-days` failed the whole request, taking the working knobs down with it. Even attached to an expiration rule (which the server accepts) MinIO does not echo the action back on GetBucketLifecycle; confirmed on `RELEASE.2025-08` and `RELEASE.2024-01`, with MinIO's own `mc ilm import`/`export` losing it too and `mc ilm rule add` offering no such option. Reclaim abandoned multipart uploads with `remove_incomplete_uploads` instead (the S3 multipart abort, which is verifiable); `ilm-gap`'s suggested action now says that rather than recommending the impossible rule. A unit test had encoded the broken three-rule shape as the spec and has been replaced.

### Added
- **`usage_by_bucket` surfaces version and delete-marker totals.** On a versioned bucket `usedBytes` is version-inclusive while `objects` counts only current objects, so the two could look inconsistent (e.g. 118 bytes across "2 objects") with no way to see that the difference is noncurrent-version overhead. Each row now carries `versions` and `deleteMarkers` from `minio_bucket_usage_version_total` / `_deletemarker_total`, making the overhead a noncurrent-expiration lifecycle rule would reclaim visible. Verified against a real MinIO bucket holding 2 current objects across 4 versions.

## v0.7.0 — 2026-08-03

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
