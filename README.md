<!-- mcp-name: io.github.AIops-tools/minio-aiops -->

# MinIO AIops

> **Disclaimer**: Community-maintained open-source project. **Not affiliated with, endorsed by, or sponsored by MinIO, Inc. or any storage vendor.** Product and trademark names belong to their owners. MIT licensed.

Governed AI-ops for **MinIO** object storage — for the homelab and small/medium
self-hosted deployments where MinIO actually lives. Talks to the **S3 API**
(official `minio` SDK, SigV4), the **admin API** (bucket quota, server info),
the unauthenticated **health endpoints** (`/minio/health/live|ready|cluster`),
and the **cluster metrics endpoint** (`/minio/v2/metrics/cluster`, bearer-token
or public auth) — with a **built-in governance harness**: a unified audit log,
a token/runaway budget guard, undo-token recording, and a descriptive risk
tier on every audit row. Self-contained: no external skill-family dependency.

## What it does

Four flagship analyses, plus the guarded reads and writes around them:

- **`capacity_rca`** — capacity vs used, offline drives/nodes, per-drive
  hotspots and imbalance → each finding as **cause + suggested action**
  (nearfull/full thresholds are named constants, not magic).
- **`bucket_exposure_audit`** — every bucket scored and **ranked** for
  anonymous/public policy statements (read and, far worse, write), missing
  default encryption, versioning off, no lifecycle.
- **`lifecycle_gap_analysis`** — the storage ILM should be reclaiming but
  isn't: versioned buckets with **no noncurrent expiry** (old bytes accrue
  forever), **incomplete multipart uploads** with no abort rule (invisible
  space), large buckets with no lifecycle — with a clearly-labelled
  **reclaimable estimate**.
- **`healing_health`** — heal backlog and per-erasure-set **write-quorum
  risk**: how many more drive failures each set can tolerate, which sets are
  healing, where heal errors are piling up.
- **Governed writes.** Bucket policy / versioning / lifecycle / quota changes
  capture the **real prior state** and record an **undo descriptor**;
  `bucket_delete` is **refused unless the bucket is verifiably empty**
  (including versions and delete markers) and `remove_incomplete_uploads`
  only touches uploads older than a safety window.

## What works

- **CLI** (`minio-aiops ...`): `init`, `overview`, `doctor`, `health
  check/status`, `capacity rca/usage`, `heal status/drives/nodes`, `bucket
  ls/info/objects/audit/ilm-gap/uploads` plus guarded writes (`bucket
  versioning-set/policy-set/lifecycle-set/quota-set/purge-uploads/delete`),
  `secret set/list/rm/migrate/rotate-password`, `mcp`. Destructive commands
  take `--dry-run` and double-confirm.
- **MCP server** (`minio-aiops mcp` or `minio-aiops-mcp`): the full **31
  tools** (22 read, 9 write), every one wrapped with the bundled
  `@governed_tool` harness. The CLI is a convenience subset; the MCP surface
  is the whole tool. CLI writes delegate to the same governed functions, so
  they are audited identically.
- **Encrypted credentials**: the secret key lives in an encrypted store
  `~/.minio-aiops/secrets.enc` (Fernet + scrypt) — **never plaintext on
  disk**. Unlock with a master password from `MINIO_AIOPS_MASTER_PASSWORD`
  (MCP/CI) or an interactive prompt (CLI).
- **Metrics auth, both modes**: servers running
  `MINIO_PROMETHEUS_AUTH_TYPE=public` are scraped directly; for the default
  (`jwt`) mode the bearer token is **derived from the stored credentials** —
  no extra secret to manage.
- **Reversibility**: reversible writes capture prior state and record an
  inverse undo descriptor (prior policy JSON, prior lifecycle XML, prior
  versioning state, prior quota).

## Capability matrix (31 MCP tools)

| Group | Tools | Count | R/W |
|-------|-------|:-----:|:---:|
| **Health** | `health_live`, `health_ready`, `health_cluster`, `cluster_status`, `fleet_overview` | 5 | read |
| **Capacity** | `capacity_rca` (flagship), `usage_by_bucket` | 2 | read |
| **Healing** | `healing_health` (flagship), `drive_status`, `node_status` | 3 | read |
| **Exposure / ILM** | `bucket_exposure_audit` (flagship), `lifecycle_gap_analysis` (flagship) | 2 | read |
| **Buckets** | `bucket_ls`, `bucket_info`, `bucket_policy_get`, `bucket_lifecycle_get`, `bucket_versioning_get`, `bucket_quota_get`, `object_ls`, `incomplete_uploads_ls`, `server_info` | 9 | read |
| **Writes** | `set_bucket_policy` (med, undo), `delete_bucket_policy` (med, undo), `set_versioning` (med, undo), `set_lifecycle` (med, undo), `delete_lifecycle` (med, undo), `set_bucket_quota` (med, undo) | 6 | write |
| | `bucket_delete` (**high**, dry-run, empty-only, irreversible), `remove_incomplete_uploads` (med, dry-run, priorState only) | 2 | write |
| **Undo** | `undo_list`, `undo_apply` | 2 | read + replay |

Totals: **31 tools — 22 read (incl. `undo_list`), 9 write (incl. `undo_apply`).**

## What this tool does, and does not, decide

It delivers MinIO object-storage operations — reads and writes — accurately and
efficiently, and records every one of them. It does **not** decide whether a
write is allowed to happen. That is the agent's judgement, or the permission of
the access key you connect it with: give the key a read-only IAM policy and the
writes fail at the server — the place that actually owns the permission.

So there is no read-only switch, no policy file, no approval gate to configure.
The one thing the tool guarantees is that nothing is silent: **every call, over
MCP and over the CLI alike, lands an audit row** in `~/.minio-aiops/audit.db`,
and reversible writes still capture their real prior state and record an inverse
undo descriptor.

> Each tool declares a `risk_level`, kept in agreement with its `[READ]`/`[WRITE]`
> documentation tag by a test, and carried into the audit row as a descriptive
> tier — so a reviewer can see at a glance that a row was a high-risk delete. It
> is a label, not a gate.

Running a smaller / local model? See
[agent-guardrails.md](skills/minio-aiops/references/agent-guardrails.md) — it lists
the guardrails this tool enforces for you (so you don't spend prompt budget
restating them) and gives a ready-made system prompt for what's left.

## Quick start

```bash
uv tool install minio-aiops         # or: pipx install minio-aiops
minio-aiops init                    # wizard: endpoint + access key; secret key stored encrypted
minio-aiops doctor                  # live/ready + S3 auth + metrics reachability
minio-aiops overview                # health + capacity headline + exposure headline
minio-aiops capacity rca            # why is storage filling up, and what to do
minio-aiops bucket audit            # ranked bucket-exposure findings
```

Run as an MCP server (stdio):

```bash
export MINIO_AIOPS_MASTER_PASSWORD=...   # unlock secrets non-interactively
minio-aiops-mcp
```

### MCP client config

```json
{
  "mcpServers": {
    "minio-aiops": {
      "command": "uvx",
      "args": ["--from", "minio-aiops", "minio-aiops-mcp"],
      "env": { "MINIO_AIOPS_MASTER_PASSWORD": "your-master-password" }
    }
  }
}
```

> **Env-block caveat**: MCP clients launch the server **without a TTY and
> without your shell profile**, so the master password cannot be prompted for
> and an `export` in `~/.zshrc` is not seen — it must be passed in the client's
> `env` block (or the client process's environment) as above. Everything else
> (targets, TLS, region, metrics mode) comes from `~/.minio-aiops/config.yaml`
> written by `minio-aiops init`.

## Configuration

`~/.minio-aiops/config.yaml` (non-secret connection details only):

```yaml
targets:
  - name: lab1
    host: 192.0.2.10
    port: 9000
    access_key: minio-ops        # identifies the account; NOT the secret
    secure: true                 # https (false for plain-http labs)
    verify_ssl: true             # false for self-signed lab certs
    region: ""                   # optional
    metrics_public: false        # true when MINIO_PROMETHEUS_AUTH_TYPE=public
```

The secret key is stored with `minio-aiops secret set lab1` (encrypted; a
legacy `MINIO_LAB1_SECRET_KEY` env var is honoured as a fallback with a
migration warning).

## Governance

Every MCP tool passes through the bundled `@governed_tool` harness:

- **Audit** — every call (params, result, status, duration, risk tier, and any
  `MINIO_AUDIT_APPROVED_BY` / `MINIO_AUDIT_RATIONALE` annotations) is logged to
  `~/.minio-aiops/audit.db` (relocatable via `MINIO_AIOPS_HOME`).
- **Budget / runaway guard** — token and call budgets trip a circuit breaker.
  A safety backstop, not authorization.
- **Risk-tier labelling** — each tool's declared `risk_level` is recorded on the
  audit row as a descriptive tier (`bucket_delete` is high). It is a label for
  the reviewer, not a gate: there is no read-only switch, policy file, or
  approval gate, and `MINIO_AUDIT_APPROVED_BY` / `MINIO_AUDIT_RATIONALE` are
  optional annotations recorded when set, never required.
- **Undo recording** — reversible writes record an inverse descriptor built
  from the captured prior state.

## Supported scope & limitations

- **Deployments**: any reasonably current MinIO server (single-node or
  distributed/erasure-coded) reachable over its S3 port. Admin features
  (quota, `server_info`) need admin-capable keys. Generic S3 services are not
  a target: the health/metrics/admin surfaces used here are MinIO-specific.
- **Metrics**: the capacity/healing RCAs read the **v2 cluster metrics**
  endpoint; both `public` and bearer-token (default) auth modes are supported.
- **Incomplete-upload listing** uses the SDK's core ListMultipartUploads call
  (the public alias was removed from the SDK); it is exercised in tests and
  documented in `connection.py`.
- **Verification status.** **Live-verified against a real single-node MinIO
  server (2026-07-19)**: connectivity, the reads, the exposure audit (it correctly
  scored an anonymously-writable bucket `high` and named `PUBLIC_WRITE_POLICY`), and
  the governance loop (real `set_versioning` → undo restoring it to `Suspended`, the
  correct S3 inverse). **Distributed / multi-node MinIO is still unverified** —
  healing was never exercised against a real degraded drive or erasure set — as are
  lifecycle/quota writes and TLS endpoints. See
  [docs/VERIFICATION.md](docs/VERIFICATION.md); `minio-aiops doctor` is the fastest
  live check.

## Missing a capability?

Site replication status, object locking / legal-hold governance, per-user /
policy (IAM) management, tiering to remote storage — not here yet. **Open an
issue or send a PR** — feedback and contributions are welcome.
