"""Guarded bucket-write MCP tools (policy / versioning / lifecycle / quota /
delete / incomplete-upload purge).

Every write takes ``dry_run`` (preview: no client call, no undo). Reversible
writes record an undo built from the REAL captured prior state; the two
irreversible ops — bucket delete (refused unless empty) and incomplete-upload
purge — record ``priorState`` only, because there is honestly no undo.
"""

from typing import Any, Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import bucket_writes as ops


def _has_prior(result: Any) -> bool:
    """True only for a REAL executed write (dry-run results carry no priorState)."""
    return isinstance(result, dict) and "priorState" in result


def _policy_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of set_bucket_policy: restore (or delete) the prior policy."""
    if not _has_prior(result):
        return None
    prior = (result.get("priorState") or {}).get("policyJson")
    bucket = params.get("bucket_name")
    if prior:
        return {"tool": "set_bucket_policy",
                "params": {"bucket_name": bucket, "policy_json": prior},
                "note": "Restore the bucket's prior policy JSON."}
    return {"tool": "delete_bucket_policy",
            "params": {"bucket_name": bucket},
            "note": "The bucket had no policy before — remove the new one."}


def _policy_delete_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of delete_bucket_policy: re-apply the captured prior policy."""
    if not _has_prior(result):
        return None
    prior = (result.get("priorState") or {}).get("policyJson")
    if not prior:
        return None  # nothing was actually removed
    return {"tool": "set_bucket_policy",
            "params": {"bucket_name": params.get("bucket_name"), "policy_json": prior},
            "note": "Re-apply the policy that was deleted."}


def _versioning_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of set_versioning: restore the captured prior state.

    S3 semantics: a bucket that was never versioned ('Off') cannot return to
    Off — the closest honest undo is Suspended, and the note says so.
    """
    if not _has_prior(result):
        return None
    prior = (result.get("priorState") or {}).get("versioning")
    if not prior:
        return None
    bucket = params.get("bucket_name")
    if prior in ("Enabled", "Suspended"):
        return {"tool": "set_versioning",
                "params": {"bucket_name": bucket, "status": prior},
                "note": "Restore the bucket's prior versioning state."}
    return {"tool": "set_versioning",
            "params": {"bucket_name": bucket, "status": "Suspended"},
            "note": "Prior state was 'Off'; S3 cannot return there — Suspended is "
                    "the closest reversal."}


def _lifecycle_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of set_lifecycle: restore the prior config XML (or delete)."""
    if not _has_prior(result):
        return None
    prior_xml = (result.get("priorState") or {}).get("lifecycleXml")
    bucket = params.get("bucket_name")
    if prior_xml:
        return {"tool": "set_lifecycle",
                "params": {"bucket_name": bucket, "lifecycle_xml": prior_xml},
                "note": "Restore the bucket's prior lifecycle configuration."}
    return {"tool": "delete_lifecycle",
            "params": {"bucket_name": bucket},
            "note": "The bucket had no lifecycle before — remove the new one."}


def _lifecycle_delete_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of delete_lifecycle: re-apply the captured prior config XML."""
    if not _has_prior(result):
        return None
    prior_xml = (result.get("priorState") or {}).get("lifecycleXml")
    if not prior_xml:
        return None  # nothing was actually removed
    return {"tool": "set_lifecycle",
            "params": {"bucket_name": params.get("bucket_name"),
                       "lifecycle_xml": prior_xml},
            "note": "Re-apply the lifecycle configuration that was deleted."}


def _quota_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of set_bucket_quota: restore the prior quota (0 clears)."""
    if not _has_prior(result):
        return None
    prior = (result.get("priorState") or {}).get("quotaBytes")
    if prior is None:
        return None
    return {"tool": "set_bucket_quota",
            "params": {"bucket_name": params.get("bucket_name"),
                       "size_bytes": int(prior)},
            "note": "Restore the bucket's prior hard quota (0 = unlimited)."}


# ── writes ───────────────────────────────────────────────────────────────


@mcp.tool()
@governed_tool(risk_level="medium", undo=_policy_undo)
@tool_errors("dict")
def set_bucket_policy(bucket_name: str, policy_json: str, dry_run: bool = False,
                      target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Replace the bucket policy. Reversible → prior policy JSON.

    An anonymous-Allow policy makes the bucket public — check
    bucket_exposure_audit after changing policies.

    Refuses a policy whose explicit Deny on s3:PutBucketPolicy covers this
    tool's own access key: an explicit Deny beats every Allow, so the undo that
    replays the prior policy would itself be denied. Enforced under dry_run too.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        policy_json: Full policy document as a JSON string (must contain 'Statement').
        dry_run: If True, preview without applying.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    # Ahead of the dry_run return: a preview whose real call would be refused
    # must say so, or the caller reads the refusal as transient and retries.
    ops.guard_set_bucket_policy(conn, policy_json)
    if dry_run:
        return {"dryRun": True,
                "wouldSetPolicy": {"bucket": bucket_name,
                                   "policyChars": len(policy_json or "")}}
    return ops.set_bucket_policy(conn, bucket_name, policy_json)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_policy_delete_undo)
@tool_errors("dict")
def delete_bucket_policy(bucket_name: str, dry_run: bool = False,
                         target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Remove the bucket policy (back to private/default).

    Reversible → the prior policy JSON is captured and re-applied on undo.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        dry_run: If True, preview without removing.
        target: MinIO target name from config; omit for the default.
    """
    if dry_run:
        return {"dryRun": True, "wouldDeletePolicy": {"bucket": bucket_name}}
    return ops.delete_bucket_policy(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_versioning_undo)
@tool_errors("dict")
def set_versioning(bucket_name: str, status: str, dry_run: bool = False,
                   target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Enable or suspend bucket versioning. Reversible → prior state.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        status: "Enabled" or "Suspended".
        dry_run: If True, preview without changing versioning.
        target: MinIO target name from config; omit for the default.
    """
    if dry_run:
        return {"dryRun": True,
                "wouldSetVersioning": {"bucket": bucket_name, "status": status}}
    return ops.set_versioning(_get_connection(target), bucket_name, status)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_lifecycle_undo)
@tool_errors("dict")
def set_lifecycle(bucket_name: str, expire_days: Optional[int] = None,
                  noncurrent_expire_days: Optional[int] = None,
                  abort_incomplete_days: Optional[int] = None, prefix: str = "",
                  lifecycle_xml: Optional[str] = None, dry_run: bool = False,
                  target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Replace the bucket lifecycle. Reversible → prior config.

    Pass the day-count knobs (rules are built for you: current-version expiry,
    noncurrent-version expiry, abort-incomplete-uploads), or lifecycle_xml to
    apply a configuration verbatim (used by undo restores). REPLACES any
    existing rules — the prior config is captured for undo.

    The undo restores the RULE, not the data: objects the rule expires before
    you undo are deleted, and putting the prior configuration back does not
    bring them back.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        expire_days: Expire current objects after N days.
        noncurrent_expire_days: Expire noncurrent versions after N days.
        abort_incomplete_days: Abort incomplete multipart uploads after N days.
        prefix: Optional key prefix the rules apply to (empty = whole bucket).
        lifecycle_xml: Full lifecycle configuration XML to apply verbatim.
        dry_run: If True, preview without applying.
        target: MinIO target name from config; omit for the default.
    """
    if dry_run:
        return {"dryRun": True,
                "wouldSetLifecycle": {"bucket": bucket_name,
                                      "expireDays": expire_days,
                                      "noncurrentExpireDays": noncurrent_expire_days,
                                      "abortIncompleteDays": abort_incomplete_days,
                                      "prefix": prefix,
                                      "verbatimXml": bool(lifecycle_xml)}}
    return ops.set_lifecycle(_get_connection(target), bucket_name,
                             expire_days=expire_days,
                             noncurrent_expire_days=noncurrent_expire_days,
                             abort_incomplete_days=abort_incomplete_days,
                             prefix=prefix, lifecycle_xml=lifecycle_xml)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_lifecycle_delete_undo)
@tool_errors("dict")
def delete_lifecycle(bucket_name: str, dry_run: bool = False,
                     target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Remove all lifecycle rules. Reversible → prior config.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        dry_run: If True, preview without removing.
        target: MinIO target name from config; omit for the default.
    """
    if dry_run:
        return {"dryRun": True, "wouldDeleteLifecycle": {"bucket": bucket_name}}
    return ops.delete_lifecycle(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_quota_undo)
@tool_errors("dict")
def set_bucket_quota(bucket_name: str, size_bytes: int, dry_run: bool = False,
                     target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Set (>0) or clear (0) the bucket hard quota. Reversible.

    Needs admin-capable credentials (admin API).

    Args:
        bucket_name: Bucket name (from bucket_ls).
        size_bytes: New hard quota in bytes; 0 clears the quota.
        dry_run: If True, preview without changing the quota.
        target: MinIO target name from config; omit for the default.
    """
    if dry_run:
        return {"dryRun": True,
                "wouldSetQuota": {"bucket": bucket_name, "sizeBytes": size_bytes}}
    return ops.set_bucket_quota(_get_connection(target), bucket_name, size_bytes)


@mcp.tool()
@governed_tool(risk_level="high")
@tool_errors("dict")
def bucket_delete(bucket_name: str, dry_run: bool = False,
                  target: Optional[str] = None) -> dict:
    """[WRITE][risk=high] Delete a bucket — refused unless verifiably empty. Irreversible.

    Pass dry_run=True to preview. The emptiness check includes noncurrent
    versions and delete markers; this tool never mass-deletes data to force a
    bucket empty.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        dry_run: If True, preview (including the emptiness check) without deleting.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        # The preview runs the emptiness check this docstring has always
        # promised. It used to return a green "wouldDelete" carrying a note that
        # execution would re-check — the preview declining to answer the only
        # question worth asking before a delete. (The real path below guards
        # inside ops.delete_bucket, immediately before removing the bucket.)
        ops.guard_delete_bucket(conn, bucket_name)
        return {"dryRun": True,
                "wouldDelete": {"bucket": bucket_name, "verifiedEmpty": True}}
    return ops.delete_bucket(conn, bucket_name)


@mcp.tool()
@governed_tool(risk_level="medium")
@tool_errors("dict")
def remove_incomplete_uploads(bucket_name: str, older_than_days: int = 7,
                              dry_run: bool = False,
                              target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Abort abandoned multipart uploads (reclaims their parts).

    priorState records the count and a sample; the parts themselves are
    unrecoverable once aborted — no undo. Only uploads at least
    older_than_days old are touched (default 7), protecting in-flight uploads.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        older_than_days: Only abort uploads at least this old (0 = all).
        dry_run: If True, preview the matching uploads without aborting.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        # Counts the real candidates rather than pointing at another tool. The
        # purge is irreversible, so "3 of 12" is the fact worth having before
        # running it — and it comes from the same selection the purge uses, so
        # it cannot disagree with what actually gets aborted.
        uploads, victims = ops.select_stale_uploads(conn, bucket_name, older_than_days)
        return {"dryRun": True,
                "wouldRemoveUploads": {"bucket": bucket_name,
                                       "olderThanDays": older_than_days,
                                       "incompleteUploads": len(uploads),
                                       "matchedForPurge": len(victims)}}
    return ops.remove_incomplete_uploads(conn, bucket_name,
                                         older_than_days=older_than_days)
