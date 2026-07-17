# minio-aiops capabilities

> Preview / mock-only. 29 MCP tools (21 read, 8 write) over four access paths:
> the **S3 API** (official SDK, SigV4), the **admin API**, the unauthenticated
> **health endpoints**, and the **cluster metrics endpoint**.

## Health (read)

| Tool | Surface | Returns |
|------|---------|---------|
| `health_live` | `GET /minio/health/live` | node liveness (reachable/healthy/status code) |
| `health_ready` | `GET /minio/health/ready` | node readiness |
| `health_cluster` | `GET /minio/health/cluster` | write-quorum health (503 = degraded) + live/ready + overall verdict |
| `cluster_status` | metrics endpoint | nodes/drives online+offline, raw/usable capacity, buckets, objects |
| `fleet_overview` | composite | health + capacity headline + exposure headline in one call |

## Flagship analyses (read)

| Tool | Surface | Returns |
|------|---------|---------|
| `capacity_rca` | metrics endpoint | findings with **cause + suggestedAction**: CLUSTER_FULL / CLUSTER_NEARFULL / DRIVES_OFFLINE / NODES_OFFLINE / DRIVE_HOTSPOT / DRIVE_IMBALANCE; per-drive usage table |
| `usage_by_bucket` | metrics endpoint | per-bucket bytes + objects, biggest first |
| `healing_health` | metrics endpoint | per-erasure-set online drives / write quorum / **failureToleranceRemaining**, healing drives, heal backlog + errors; findings WRITE_QUORUM_LOST / _AT_EDGE / LOW_FAILURE_TOLERANCE / HEALING_IN_PROGRESS / HEAL_ERRORS |
| `drive_status` | metrics endpoint | per-drive rows (server, drive, used ratio), fullest first |
| `node_status` | metrics endpoint | nodes online/offline + per-node drive counts |
| `bucket_exposure_audit` | S3 API per bucket | **ranked** findings: PUBLIC_WRITE_POLICY / PUBLIC_READ_POLICY / NO_DEFAULT_ENCRYPTION / VERSIONING_OFF / NO_LIFECYCLE with riskScore + riskLevel |
| `lifecycle_gap_analysis` | S3 API + metrics | gaps: NONCURRENT_VERSIONS_UNBOUNDED (+reclaimable estimate) / INCOMPLETE_UPLOADS_NO_ABORT_RULE (+counts, ages) / NO_LIFECYCLE_ON_LARGE_BUCKET |

## Buckets (read)

| Tool | Surface | Returns |
|------|---------|---------|
| `bucket_ls` | `ListBuckets` | name + creation time |
| `bucket_info` | composite per bucket | policy (present/publicRead/publicWrite), versioning, lifecycle rules, encryption, quota, tags — per-probe failures degrade to an `errors` list |
| `bucket_policy_get` | `GetBucketPolicy` | verbatim policy JSON + anonymous-access summary |
| `bucket_lifecycle_get` | `GetBucketLifecycle` | rules as dicts (ruleId, status, prefix, expirationDays, noncurrentExpirationDays, abortIncompleteDays) |
| `bucket_versioning_get` | `GetBucketVersioning` | Enabled / Suspended / Off |
| `bucket_quota_get` | admin API | hard quota bytes (0 = unlimited) |
| `object_ls` | `ListObjectsV2` | bounded listing (default 100, max 1000) under a prefix |
| `incomplete_uploads_ls` | `ListMultipartUploads` | object, uploadId, initiated time |
| `server_info` | admin API | mode, deployment id, server/pool counts |

## Writes (governed; all take `dry_run`)

| Tool | Risk | Undo | Notes |
|------|:----:|:----:|-------|
| `set_bucket_policy` | medium | prior policy JSON (or delete if none) | policy_json validated as JSON with a Statement |
| `delete_bucket_policy` | medium | re-apply prior policy JSON | no-op undo when there was none |
| `set_versioning` | medium | prior state | prior "Off" undoes to Suspended (S3 cannot return to Off — noted) |
| `set_lifecycle` | medium | prior lifecycle XML (or delete if none) | day-count knobs build rules; `lifecycle_xml` applies verbatim (undo path) |
| `delete_lifecycle` | medium | re-apply prior lifecycle XML | |
| `set_bucket_quota` | medium | prior quota (0 clears) | admin API |
| `bucket_delete` | **high** | none (irreversible) | refused unless verifiably empty (versions + delete markers included); priorState = bucket meta |
| `remove_incomplete_uploads` | medium | none (parts unrecoverable) | age-gated (default: only uploads ≥ 7 days old); priorState = count + sample |

## Out of scope (v0.1.0)

- Site replication status/management
- Object locking / legal hold governance
- IAM (users, groups, canned policies) management
- Tiering to remote storage

Missing something you need? **Open an issue or send a PR** — feedback welcome.
