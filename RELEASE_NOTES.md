# Release notes — minio-aiops 0.2.1

Previous release: 0.2.0.

## Live-verified

No behaviour changes. This release records the first end-to-end run against a real
**MinIO** server: connectivity, the reads, the exposure audit (it correctly scored an
anonymously-writable bucket `high` and named `PUBLIC_WRITE_POLICY` with a concrete
remediation), the governance loop (real `set_versioning` → `undo_apply` restoring it to
`Suspended`, the correct S3 inverse), and read-only mode removing exactly the 8 bucket
writes plus `undo_apply` while leaving every read in place.

Documentation now states what is confirmed and what is not:
**distributed / multi-node MinIO is still unverified** — healing was never exercised
against a real degraded drive or erasure set — as are lifecycle/quota writes and TLS
endpoints. See [docs/VERIFICATION.md](docs/VERIFICATION.md).
