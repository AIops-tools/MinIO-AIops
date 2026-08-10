"""Object-lock (WORM) MCP tools: lock inventory, per-object status, retention
gap analysis, and the four guarded writes.

Three of the writes record an undo built from the real captured prior state.
``set_object_retention`` deliberately records **none**: S3 will not shorten or
remove retention without a bypass header this SDK never sends, so an undo token
would be one whose replay is guaranteed to fail while the audit row claimed the
write was reversible. Every guard runs ahead of the ``dry_run`` return, so a
preview of a call that would be refused reports the refusal.
"""

from typing import Any, Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import lock_writes as writes
from minio_aiops.ops import objectlock as ops


def _has_prior(result: Any) -> bool:
    """True only for a REAL executed write (dry-run results carry no priorState)."""
    return isinstance(result, dict) and "priorState" in result


def _create_bucket_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of create_bucket: delete it — which only works while it is empty."""
    if not _has_prior(result):
        return None
    return {
        "tool": "bucket_delete",
        "params": {"bucket_name": params.get("bucket_name")},
        "note": (
            "Delete the bucket this call created. Refused once anything has been "
            "written into it — this tool never mass-deletes data to force a bucket "
            "empty."
        ),
    }


def _default_retention_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of a default-retention change: restore the prior rule, or clear it."""
    if not _has_prior(result):
        return None
    bucket = params.get("bucket_name")
    prior = (result.get("priorState") or {}).get("defaultRetention")
    if not prior:
        return {
            "tool": "clear_default_retention",
            "params": {"bucket_name": bucket},
            "note": (
                "The bucket had no default retention rule before — remove the new "
                "one. Objects written under it keep their own retention."
            ),
        }
    restore: dict[str, Any] = {"bucket_name": bucket, "mode": prior.get("mode")}
    if prior.get("days"):
        restore["days"] = int(prior["days"])
    elif prior.get("years"):
        restore["years"] = int(prior["years"])
    else:
        return None  # a rule with no duration is not something we can replay
    return {
        "tool": "set_default_retention",
        "params": restore,
        "note": (
            "Restore the bucket's prior default retention rule. Objects written "
            "while the new rule was in force keep the retention they were given."
        ),
    }


def _legal_hold_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of set_legal_hold: put the hold back the way it was."""
    if not _has_prior(result):
        return None
    prior = (result.get("priorState") or {}).get("legalHold")
    if prior is None:
        return None  # prior state unknown — nothing honest to replay
    return {
        "tool": "set_legal_hold",
        "params": {
            "bucket_name": params.get("bucket_name"),
            "object_name": params.get("object_name"),
            "hold_on": bool(prior),
            "version_id": params.get("version_id"),
        },
        "note": "Restore the object's prior legal-hold state.",
    }


# ── reads ────────────────────────────────────────────────────────────────


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_lock_config(bucket_name: str, target: Optional[str] = None) -> dict:
    """[READ][risk=low] Object-lock (WORM) state of one bucket.

    Distinguishes the two absences that look alike and mean different things:
    objectLockEnabled=false (lock was never enabled and cannot be enabled on an
    existing bucket) versus objectLockEnabled=true with defaultRetention=null
    (WORM is available, but an upload that omits its own retention header is
    retained for nothing).

    Args:
        bucket_name: Bucket name (from bucket_ls).
        target: MinIO target name from config; omit for the default.
    """
    return ops.bucket_lock_config(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def object_lock_status(bucket_name: str, object_name: str,
                       version_id: Optional[str] = None,
                       target: Optional[str] = None) -> dict:
    """[READ][risk=low] Retention + legal hold for one object version.

    Reports whether the version is deletable right now and what blocks it.
    Retention and legal hold stack independently: lifting the hold does not help
    while retention runs, and a lapsed retention does not help while the hold is
    on.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        object_name: Object key (from bucket_objects).
        version_id: Specific version; omit for the current version.
        target: MinIO target name from config; omit for the default.
    """
    return ops.object_lock_status(_get_connection(target), bucket_name, object_name,
                                  version_id=version_id)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def diagnose_retention_gaps(limit: int = 50, target: Optional[str] = None) -> dict:
    """[READ][risk=low] WORM/retention gaps across every bucket, worst-first.

    Finds the contradictions rather than a house style: lock enabled with no
    default retention (protects nothing), a lifecycle expiry that retention
    outlives (the rule can never delete and the capacity never returns — both
    day counts reported), object lock on a bucket whose versioning is not
    active, and the mode choice with its consequence spelled out.

    Buckets whose probes fail are listed in bucketErrors rather than skipped: a
    clean findings list does not mean those buckets are clean.

    Args:
        limit: Maximum findings to return (envelope reports truncation).
        target: MinIO target name from config; omit for the default.
    """
    return ops.diagnose_retention_gaps(_get_connection(target), limit=limit)


# ── writes ───────────────────────────────────────────────────────────────


@mcp.tool()
@governed_tool(risk_level="medium", undo=_create_bucket_undo)
@tool_errors("dict")
def bucket_create(bucket_name: str, object_lock: bool = False, dry_run: bool = False,
                  target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Create a bucket, optionally WORM-capable. Reversible.

    object_lock=True is the ONLY way to get object lock: S3 accepts the flag at
    bucket creation and has no call that enables it later. The server
    force-enables versioning as a consequence; the result reports what it
    observed rather than what was assumed.

    Undo deletes the bucket, and only while it is still empty.

    Args:
        bucket_name: New bucket name (3-63 chars, lowercase/digits/dots/hyphens).
        object_lock: Enable object lock (WORM). Cannot be changed afterwards.
        dry_run: If True, preview without creating.
        target: MinIO target name from config; omit for the default.
    """
    if dry_run:
        return {"dryRun": True,
                "wouldCreateBucket": {"bucket": bucket_name,
                                      "objectLock": bool(object_lock)}}
    return writes.create_bucket(_get_connection(target), bucket_name,
                                object_lock=object_lock)


@mcp.tool()
@governed_tool(risk_level="high", undo=_default_retention_undo)
@tool_errors("dict")
def set_default_retention(bucket_name: str, mode: str, days: Optional[int] = None,
                          years: Optional[int] = None, dry_run: bool = False,
                          target: Optional[str] = None) -> dict:
    """[WRITE][risk=high] Set the bucket's DEFAULT retention. Reversible → prior rule.

    Applies to objects written from now on; objects already stored are untouched.
    Undo restores the prior rule — but objects written while this rule was in
    force keep the retention they were given, and COMPLIANCE retention on them
    cannot be shortened by anyone afterwards.

    Refused (under dry_run too) when the bucket has no object lock, since the
    rule would have nothing to attach to.

    Args:
        bucket_name: Bucket name (must already have object lock enabled).
        mode: "GOVERNANCE" (an admin with bypass permission can lift it) or
            "COMPLIANCE" (nobody can lift it before the date).
        days: Retention duration in days. Pass exactly one of days/years.
        years: Retention duration in years. Pass exactly one of days/years.
        dry_run: If True, preview without applying.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    # Ahead of the dry_run return: a preview whose real call would be refused
    # for want of object lock must say so, not report green.
    writes.guard_set_default_retention(conn, bucket_name, mode)
    if dry_run:
        return {"dryRun": True,
                "wouldSetDefaultRetention": {"bucket": bucket_name, "mode": mode,
                                             "days": days, "years": years},
                "currentLock": writes.summarize_lock(conn, bucket_name)}
    return writes.set_default_retention(conn, bucket_name, mode, days=days, years=years)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_default_retention_undo)
@tool_errors("dict")
def clear_default_retention(bucket_name: str, dry_run: bool = False,
                            target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Remove the DEFAULT retention rule. Reversible → prior rule.

    Clears the rule only. Object lock stays enabled on the bucket (S3 has no
    call that disables it) and objects already carrying retention keep it, so
    this makes future uploads unprotected and changes nothing already stored.

    Args:
        bucket_name: Bucket name (must have object lock enabled).
        dry_run: If True, preview without clearing.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        return {"dryRun": True,
                "wouldClearDefaultRetention": {"bucket": bucket_name},
                "currentLock": writes.summarize_lock(conn, bucket_name)}
    return writes.clear_default_retention(conn, bucket_name)


@mcp.tool()
@governed_tool(risk_level="critical")
@tool_errors("dict")
def set_object_retention(bucket_name: str, object_name: str, mode: str, days: int,
                         version_id: Optional[str] = None,
                         acknowledge_irreversible: bool = False,
                         dry_run: bool = False,
                         target: Optional[str] = None) -> dict:
    """[WRITE][risk=critical] Put retention on one object version. NO UNDO EXISTS.

    This is extend-only and irreversible through this tool, not by policy but by
    construction: S3 refuses to shorten or remove retention without the
    x-amz-bypass-governance-retention header, and the minio SDK never sends it.
    COMPLIANCE mode cannot be shortened by any credential at all — not root —
    until the date passes, and the storage cannot be reclaimed before then.

    No undo token is recorded. Calls that would shorten or downgrade retention
    already in force are refused, and COMPLIANCE requires
    acknowledge_irreversible=True. Both refusals fire under dry_run.

    Args:
        bucket_name: Bucket name (must have object lock enabled).
        object_name: Object key (from bucket_objects).
        mode: "GOVERNANCE" (liftable out of band with `mc retention clear` by a
            holder of s3:BypassGovernanceRetention — note that command has no
            --bypass flag) or "COMPLIANCE" (liftable by nobody before the date,
            root included).
        days: Retain for this many days from now.
        version_id: Specific version; omit for the current version.
        acknowledge_irreversible: Required True for COMPLIANCE mode.
        dry_run: If True, run every guard and report the before-state without writing.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    # The guards run first in BOTH paths, from one function, so a preview can
    # never report green for a call the real write would refuse.
    current = writes.guard_set_object_retention(
        conn, bucket_name, object_name, mode, days, version_id=version_id,
        acknowledge_irreversible=acknowledge_irreversible)
    if dry_run:
        return {"dryRun": True,
                "wouldSetObjectRetention": {"bucket": bucket_name,
                                            "objectName": object_name,
                                            "mode": str(mode).upper(),
                                            "days": days,
                                            "retainUntil": current["newRetainUntil"]},
                "currentRetention": {"mode": current["mode"],
                                     "retainUntil": current["retainUntil"],
                                     "daysRemaining": current["daysRemaining"]},
                "reversible": False}
    return writes.set_object_retention(
        conn, bucket_name, object_name, mode, days, version_id=version_id,
        acknowledge_irreversible=acknowledge_irreversible)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_legal_hold_undo)
@tool_errors("dict")
def set_legal_hold(bucket_name: str, object_name: str, hold_on: bool,
                   version_id: Optional[str] = None, dry_run: bool = False,
                   target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Turn a legal hold on/off. Reversible → prior hold state.

    A legal hold blocks deletion with no date attached and is lifted by turning
    it off — the one WORM control that is reversible by design. It stacks with
    retention: lifting the hold does not make an object deletable while its
    retention still runs.

    Args:
        bucket_name: Bucket name (must have object lock enabled).
        object_name: Object key (from bucket_objects).
        hold_on: True places the hold, False lifts it.
        version_id: Specific version; omit for the current version.
        dry_run: If True, preview (reading the current hold) without changing it.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    prior = writes.guard_set_legal_hold(conn, bucket_name, object_name)
    if dry_run:
        return {"dryRun": True,
                "wouldSetLegalHold": {"bucket": bucket_name,
                                      "objectName": object_name,
                                      "holdOn": bool(hold_on)},
                "currentLegalHold": prior}
    return writes.set_legal_hold(conn, bucket_name, object_name, hold_on,
                                 version_id=version_id)
