---
name: minio-aiops
description: >
  Use this skill whenever the user needs to operate or diagnose MinIO object storage — explain why the cluster is filling up or refusing writes (capacity_rca), find publicly exposed buckets and hygiene gaps (bucket_exposure_audit), find storage that lifecycle/ILM should be reclaiming but isn't, including noncurrent versions and incomplete multipart uploads (lifecycle_gap_analysis), check heal backlog and erasure-set write-quorum risk (healing_health), read service health / cluster status / per-bucket config (policy, versioning, lifecycle, encryption, quota, tags) — plus governed writes (set or delete bucket policy, enable/suspend versioning, set or delete lifecycle rules, set bucket quota, purge incomplete uploads, delete an empty bucket).
  Always use this skill for "minio health", "why is my object storage full", "which bucket is biggest", "is any bucket public / anonymous access", "versioning / noncurrent versions piling up", "incomplete multipart uploads", "lifecycle / ILM rules", "bucket quota", "erasure set / drive failure tolerance", "healing backlog", or "delete a bucket safely" when the context is a MinIO deployment.
  Do NOT use when the target is not MinIO — for Ceph/RGW use ceph-aiops; for TrueNAS storage use truenas-aiops; for a hypervisor, backup product, container cluster, or network device route to the appropriate other AIops-tools skill (negative routing hint only).
  Preview — common MinIO ops with a built-in governance harness (audit, policy, token budget, undo, risk-tiers). Mock-validated only, not yet verified against a live server.
installer:
  kind: uv
  package: minio-aiops
argument-hint: "[minio question or describe your object-storage task]"
allowed-tools:
  - Bash
metadata: {"openclaw":{"requires":{"env":["MINIO_AIOPS_CONFIG"],"bins":["minio-aiops"],"config":["~/.minio-aiops/config.yaml","~/.minio-aiops/secrets.enc"]},"optional":{"env":["MINIO_AIOPS_MASTER_PASSWORD"]},"primaryEnv":"MINIO_AIOPS_CONFIG","homepage":"https://github.com/AIops-tools/MinIO-AIops","emoji":"🪣","os":["macos","linux"]}}
compatibility: >
  Standalone, self-governed MinIO operations (preview). The governance harness (audit, policy, token/runaway budget, undo, risk-tiers) is bundled in the package — no external skill-family dependency. Works against any reasonably current MinIO server (single-node or distributed/erasure-coded); admin features (quota, server info) need admin-capable keys.
  All write operations are audited to a local SQLite DB under ~/.minio-aiops/ (relocatable via MINIO_AIOPS_HOME).
  Connection: the S3 API endpoint (host:port, default 9000; SigV4 via the official SDK), plus the unauthenticated health endpoints (/minio/health/live|ready|cluster) and the cluster metrics endpoint (/minio/v2/metrics/cluster — public mode or the default bearer-token mode; the token is derived from the stored credentials, no extra secret). The access key lives in config.yaml; the secret key is stored ENCRYPTED in ~/.minio-aiops/secrets.enc (Fernet/AES-128 + scrypt-derived key) — never plaintext on disk. Run 'minio-aiops init' to onboard, or 'minio-aiops secret set <target>' to add one. The store is unlocked by a master password from MINIO_AIOPS_MASTER_PASSWORD (non-interactive/MCP/CI) or an interactive prompt (CLI on a TTY). A legacy plaintext env var MINIO_<TARGET_NAME_UPPER>_SECRET_KEY is still honoured as a fallback with a deprecation warning (migrate with 'minio-aiops secret migrate'). Secrets are held only in memory, never logged or echoed.
  State-changing operations require double confirmation at the CLI layer and support --dry-run. All write tools pass through the @governed_tool decorator (pre-check + budget guard + audit + risk-tier gate). bucket_delete is high-risk, dry-run + double-confirm, and refused unless the bucket is verifiably empty (including versions and delete markers); remove_incomplete_uploads only aborts uploads older than a safety window. Reversible writes (set/delete bucket policy, set_versioning, set/delete lifecycle, set_bucket_quota) capture the prior state and record an inverse undo descriptor.
  Webhooks: none — no outbound network calls beyond the configured MinIO endpoint.
  SSL: secure (https) and verify_ssl default to true; disable verification only for self-signed lab certificates.
  Transitive dependencies: the official minio SDK, httpx, and the MCP SDK. No post-install scripts or background services.
  PREVIEW: mock-validated only; erasure-set/healing findings need a multi-drive deployment to observe live (a single-node server running 'minio-aiops doctor' is the cheapest live path).
---

# MinIO AIops (preview)

> **Disclaimer**: Community-maintained open-source project, **not affiliated with, endorsed by, or sponsored by MinIO, Inc. or any storage vendor.** Product and trademark names belong to their owners. Source at [github.com/AIops-tools/MinIO-AIops](https://github.com/AIops-tools/MinIO-AIops) under the MIT license.

Governed MinIO object-storage operations — **29 MCP tools**, every one wrapped with the bundled `@governed_tool` harness: a local unified audit log under `~/.minio-aiops/`, policy engine, token/runaway budget guard, undo-token recording, and graduated-autonomy risk tiers. The secret key is stored **encrypted** (`~/.minio-aiops/secrets.enc`, Fernet + scrypt) — never plaintext on disk. Four flagship analyses turn raw state into plain-language **cause + suggested action**: `capacity_rca`, `bucket_exposure_audit`, `lifecycle_gap_analysis`, `healing_health`.

> **Standalone**: the governance harness is bundled in the package (`minio_aiops.governance`) — minio-aiops has no external skill-family dependency. **Preview / mock-only**: not yet validated against a live server.

## What This Skill Does

| Group | Tools | Count | Read or Write |
|-------|-------|:-----:|:-------------:|
| **Health** | health_live, health_ready, health_cluster, cluster_status, fleet_overview | 5 | 5 read |
| **Capacity** | capacity_rca (flagship), usage_by_bucket | 2 | 2 read |
| **Healing** | healing_health (flagship), drive_status, node_status | 3 | 3 read |
| **Exposure / ILM** | bucket_exposure_audit (flagship), lifecycle_gap_analysis (flagship) | 2 | 2 read |
| **Buckets** | bucket_ls, bucket_info, bucket_policy_get, bucket_lifecycle_get, bucket_versioning_get, bucket_quota_get, object_ls, incomplete_uploads_ls, server_info | 9 | 9 read |
| **Writes** | set_bucket_policy, delete_bucket_policy, set_versioning, set_lifecycle, delete_lifecycle, set_bucket_quota, bucket_delete, remove_incomplete_uploads | 8 | 8 write |

Totals: **29 tools — 21 read, 8 write.** The MCP server exposes all 29; the CLI is a convenience subset.

## Quick Install

```bash
uv tool install minio-aiops
minio-aiops init       # interactive wizard: endpoint + access key + encrypted secret key
minio-aiops doctor
```

## When to Use This Skill

- **"Storage is filling up / writes are failing"** → `capacity_rca` (capacity vs used, offline drives/nodes, hotspots — cause + action per finding), then `usage_by_bucket` for the biggest consumers
- **"Is anything exposed?"** → `bucket_exposure_audit` (ranked: public read/write policies, missing encryption, versioning off, no lifecycle)
- **"Where did my space go?"** → `lifecycle_gap_analysis` (unbounded noncurrent versions, incomplete multipart uploads, reclaimable estimate) then `remove_incomplete_uploads` + `set_lifecycle` to fix it for good
- **"How many more drives can fail?"** → `healing_health` (per-erasure-set online drives vs write quorum, heal backlog/errors)
- One-shot triage (`fleet_overview` / `minio-aiops overview`): health + capacity headline + exposure headline
- Per-bucket questions: `bucket_info` (policy/versioning/lifecycle/encryption/quota/tags in one answer)
- Safely change policy/versioning/lifecycle/quota (reversible, undo recorded) or delete an **empty** bucket (governed, dry-run + double confirm)

**Do NOT use when** the target is not MinIO — for Ceph/RGW use **ceph-aiops**; for TrueNAS use **truenas-aiops**; for a hypervisor, backup product, container cluster, or network device route to the appropriate **other AIops-tools** skill.

## Related Skills — Skill Routing

| If the user wants… | Use |
|--------------------|-----|
| MinIO: capacity RCA, bucket exposure, ILM gaps, healing, bucket writes | **minio-aiops** (this skill) |
| Ceph/RGW storage | **ceph-aiops** |
| TrueNAS storage appliances | **truenas-aiops** |
| Any other target (hypervisor, backup, cluster, network) | the appropriate **other AIops-tools** skill |

## Common Workflows

### "The cluster is nearly full" (capacity RCA → reclaim)

1. `minio-aiops capacity rca` → findings with cause + action (`CLUSTER_NEARFULL`, `DRIVES_OFFLINE`, `DRIVE_HOTSPOT`, …)
2. `minio-aiops capacity usage` → biggest buckets first
3. `minio-aiops bucket ilm-gap` → what ILM should be reclaiming (noncurrent versions, incomplete uploads) + estimate
4. Reclaim: `bucket purge-uploads <bucket>` (dry-run first, double confirm), then prevent recurrence: `bucket lifecycle-set <bucket> --noncurrent-days 30 --abort-days 7` (reversible — prior config captured)

### Bucket exposure audit → fix

1. `minio-aiops bucket audit` → ranked findings, riskiest first
2. For a `PUBLIC_WRITE_POLICY` finding: `bucket_policy_get` to see the offending statement, then `set_bucket_policy` with a restricted document or `delete_bucket_policy` (both reversible — prior policy JSON captured)
3. Re-run the audit to confirm the score dropped

### Safely delete a bucket (governed)

1. `minio-aiops bucket info <bucket>` and `object_ls` → confirm it is the right bucket
2. `minio-aiops bucket delete <bucket> --dry-run` → preview
3. Re-run without `--dry-run` (double confirm, **high** risk — set `MINIO_AUDIT_APPROVED_BY`/`MINIO_AUDIT_RATIONALE`). Execution re-checks emptiness (versions and delete markers included) and refuses otherwise — this tool never mass-deletes data.
- **Secure by default**: with no `~/.minio-aiops/rules.yaml`, high-risk operations are denied unless `MINIO_AUDIT_APPROVED_BY` names an approver. `minio-aiops init` seeds a starter rules.yaml; an operator-authored rules file is honoured as-is.

### "How many more drive failures can I take?"

`healing_health` → per erasure set: online drives, write quorum, **failureToleranceRemaining**, healing drives, heal backlog. `WRITE_QUORUM_AT_EDGE` means the next failure stops writes — replace drives before any maintenance.

## Governance & Safety

- Every tool is audited to `~/.minio-aiops/audit.db` (relocatable via `MINIO_AIOPS_HOME`).
- High-risk ops (`bucket_delete`) require a named approver: set `MINIO_AUDIT_APPROVED_BY` and `MINIO_AUDIT_RATIONALE`.
- Destructive writes support `--dry-run` and double confirmation at the CLI.
- Reversible writes record an inverse descriptor capturing the real prior state (policy JSON, lifecycle XML, versioning state, quota).

## References

- `references/capabilities.md` — full tool → API-surface → returns reference
- `references/cli-reference.md` — CLI command reference
- `references/setup-guide.md` — onboarding, credentials, and connectivity
