# Security Policy

## Disclaimer

Community-maintained open-source project. **Not affiliated with, endorsed by, or
sponsored by MinIO, Inc. or any storage vendor.** Product and trademark names
belong to their owners. Source is publicly auditable under the MIT license.

## Reporting Vulnerabilities

Report privately via a GitHub Security Advisory on
[github.com/AIops-tools/MinIO-AIops](https://github.com/AIops-tools/MinIO-AIops/security/advisories)
or email zhouwei008@gmail.com. Please do not open public issues for security
reports.

## Security Design

### Credential Management
- Per-target **secret keys** live **encrypted** in
  `~/.minio-aiops/secrets.enc` (Fernet/AES-128 + scrypt-derived key; chmod
  600), never in `config.yaml` and never in source. The master password is
  never stored — only a per-store random salt and the ciphertext are on disk.
- A legacy plaintext env var `MINIO_<TARGET_NAME_UPPER>_SECRET_KEY` is still
  honoured as a fallback with a deprecation warning (migrate with
  `minio-aiops secret migrate`).
- S3/admin requests are **SigV4-signed** by the official SDK; the secret key
  is held only in memory, never logged or echoed. The metrics bearer token is
  derived from the credentials at request time (short TTL) and never stored.
  The config file holds only host, port, access key, TLS/region/metrics
  settings.

### Governed Operations
Every MCP tool runs through the bundled `@governed_tool` harness
(`minio_aiops.governance`):
- **Audit** — every call logged to a local SQLite DB under `~/.minio-aiops/`
  (relocatable via `MINIO_AIOPS_HOME`), agent-attributed, secret-redacted.
- **Token/runaway budget** — hard ceilings (`MINIO_MAX_TOOL_CALLS` /
  `MINIO_MAX_TOOL_SECONDS`) plus an on-by-default guard that trips a tight
  poll/retry loop, preventing unbounded API consumption.
- **Graduated risk tiers, secure by default** — with no
  `~/.minio-aiops/rules.yaml`, high-risk writes require a recorded approver;
  the `init` wizard seeds an explicit, editable starter policy.
- **Undo-token recording** — reversible writes capture the BEFORE state and
  record an inverse descriptor (prior policy JSON, prior lifecycle XML, prior
  versioning state, prior quota) so the change can be rolled back.

### State-Changing Operations
`bucket_delete` is `risk_level=high`, accepts a `dry_run` preview, requires a
recorded approver (`MINIO_AUDIT_APPROVED_BY` + `MINIO_AUDIT_RATIONALE`) under
the default policy, and is **refused unless the bucket is verifiably empty**
(including noncurrent versions and delete markers) — this tool never
mass-deletes objects to force a bucket empty. `remove_incomplete_uploads`
only aborts uploads older than a safety window (default 7 days) and records
priorState (count + sample). The CLI double-confirms both and supports
`--dry-run` everywhere. All agent-supplied bucket names are validated against
strict S3 naming rules before any request is built (the injection gate).

### SSL/TLS Verification
`secure` (https) and `verify_ssl` default to true; disable verification only
for self-signed lab certificates.

### Output Hygiene
All server-returned text (bucket/object names, policy contents, error bodies,
metric labels) is passed through a `sanitize()` truncate + control-character
strip before reaching the agent.

### Network Scope
No webhooks, no telemetry, no outbound calls beyond the configured MinIO
endpoint (S3/admin/health/metrics paths on the same origin). No post-install
scripts or background services.

## Static Analysis

```bash
uvx bandit -r minio_aiops/ mcp_server/
uv run ruff check .
```

## Supported Versions

The latest released version receives security fixes. This is a preview (0.x);
pin a version in production.
