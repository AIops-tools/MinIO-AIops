"""Guarded bucket writes (policy / versioning / lifecycle / quota / delete / uploads).

Every reversible write captures the bucket's BEFORE state into ``priorState``
so the harness can record a faithful undo (restore the prior policy JSON,
lifecycle XML, versioning state, or quota). The two footguns — deleting a
bucket (only allowed when verifiably empty) and purging incomplete uploads
(the parts are unrecoverable) — record ``priorState`` only: there is honestly
no undo, and pretending otherwise would be worse than saying so.

All bucket names pass ``check_bucket_name`` (the injection gate) first.
"""

from __future__ import annotations

import json
from typing import Any

from minio_aiops.ops._util import check_bucket_name, s
from minio_aiops.ops.ilm import _age_days

VERSIONING_STATES = ("Enabled", "Suspended")
# Only uploads at least this old are purged by default — an in-flight upload
# younger than this is plausibly still legitimate.
DEFAULT_PURGE_OLDER_THAN_DAYS = 7


def set_bucket_policy(conn: Any, bucket: str, policy_json: str) -> dict:
    """[WRITE][medium] Replace the bucket policy. Reversible → prior policy JSON."""
    check_bucket_name(bucket)
    try:
        parsed = json.loads(policy_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"policy_json is not valid JSON: {s(exc, 120)}") from exc
    if not isinstance(parsed, dict) or "Statement" not in parsed:
        raise ValueError("policy_json must be a policy document with a 'Statement' list.")
    prior = conn.get_bucket_policy(bucket)
    conn.set_bucket_policy(bucket, policy_json)
    return {
        "action": "set_bucket_policy",
        "bucket": s(bucket),
        "priorState": {"policyJson": prior},
    }


def delete_bucket_policy(conn: Any, bucket: str) -> dict:
    """[WRITE][medium] Remove the bucket policy. Reversible → prior policy JSON."""
    check_bucket_name(bucket)
    prior = conn.get_bucket_policy(bucket)
    conn.delete_bucket_policy(bucket)
    return {
        "action": "delete_bucket_policy",
        "bucket": s(bucket),
        "priorState": {"policyJson": prior},
    }


def set_versioning(conn: Any, bucket: str, status: str) -> dict:
    """[WRITE][medium] Enable/suspend versioning. Reversible → prior state.

    Note the S3 semantics: a bucket that has EVER been versioned can only be
    suspended, never returned to 'Off' — the undo honours that.
    """
    check_bucket_name(bucket)
    normalized = str(status).strip().capitalize()
    if normalized not in VERSIONING_STATES:
        raise ValueError(
            f"status must be one of {VERSIONING_STATES} (got {s(status, 40)!r})."
        )
    prior = conn.get_bucket_versioning(bucket)
    conn.set_bucket_versioning(bucket, normalized)
    return {
        "action": "set_versioning",
        "bucket": s(bucket),
        "status": normalized,
        "priorState": {"versioning": prior},
    }


def set_lifecycle(
    conn: Any,
    bucket: str,
    *,
    expire_days: int | None = None,
    noncurrent_expire_days: int | None = None,
    abort_incomplete_days: int | None = None,
    prefix: str = "",
    lifecycle_xml: str | None = None,
) -> dict:
    """[WRITE][medium] Replace the bucket lifecycle. Reversible → prior config XML.

    Either pass the day-count knobs (rules are built for you) or
    ``lifecycle_xml`` to apply a config verbatim (the undo-restore path).
    """
    check_bucket_name(bucket)
    for name, value in (
        ("expire_days", expire_days),
        ("noncurrent_expire_days", noncurrent_expire_days),
        ("abort_incomplete_days", abort_incomplete_days),
    ):
        if value is not None and (not isinstance(value, int) or value < 1):
            raise ValueError(f"{name} must be a positive integer (got {value!r}).")
    prior_xml = conn.get_bucket_lifecycle_xml(bucket)
    if lifecycle_xml:
        conn.set_bucket_lifecycle_xml(bucket, lifecycle_xml)
    else:
        conn.set_bucket_lifecycle(
            bucket,
            expire_days=expire_days,
            noncurrent_expire_days=noncurrent_expire_days,
            abort_incomplete_days=abort_incomplete_days,
            prefix=prefix,
        )
    return {
        "action": "set_lifecycle",
        "bucket": s(bucket),
        "applied": {
            "expireDays": expire_days,
            "noncurrentExpireDays": noncurrent_expire_days,
            "abortIncompleteDays": abort_incomplete_days,
            "prefix": s(prefix, 120),
            "verbatimXml": bool(lifecycle_xml),
        },
        "priorState": {"lifecycleXml": prior_xml},
    }


def delete_lifecycle(conn: Any, bucket: str) -> dict:
    """[WRITE][medium] Remove all lifecycle rules. Reversible → prior config XML."""
    check_bucket_name(bucket)
    prior_xml = conn.get_bucket_lifecycle_xml(bucket)
    conn.delete_bucket_lifecycle(bucket)
    return {
        "action": "delete_lifecycle",
        "bucket": s(bucket),
        "priorState": {"lifecycleXml": prior_xml},
    }


def set_bucket_quota(conn: Any, bucket: str, size_bytes: int) -> dict:
    """[WRITE][medium] Set (>0) or clear (0) the bucket hard quota. Reversible."""
    check_bucket_name(bucket)
    if not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError(f"size_bytes must be an integer >= 0 (got {size_bytes!r}).")
    prior = conn.get_bucket_quota(bucket)
    conn.set_bucket_quota(bucket, size_bytes)
    return {
        "action": "set_bucket_quota",
        "bucket": s(bucket),
        "sizeBytes": size_bytes,
        "priorState": {"quotaBytes": prior.get("quotaBytes", 0)},
    }


def delete_bucket(conn: Any, bucket: str) -> dict:
    """[WRITE][high] Delete a bucket — refused unless verifiably empty. Irreversible.

    The emptiness check includes noncurrent versions and delete markers; a
    bucket that still holds any of those cannot be deleted safely (or at all).
    """
    check_bucket_name(bucket)
    if not conn.is_bucket_empty(bucket):
        raise ValueError(
            f"Bucket '{s(bucket, 80)}' is not empty (objects, versions, or delete "
            f"markers remain). Empty it first — this tool never mass-deletes data."
        )
    prior_meta: dict[str, Any] = {"bucket": s(bucket)}
    try:
        prior_meta["versioning"] = conn.get_bucket_versioning(bucket)
        prior_meta["policyPresent"] = conn.get_bucket_policy(bucket) is not None
    except Exception:  # noqa: BLE001 — best-effort forensic capture, never a blocker
        pass
    conn.remove_bucket(bucket)
    return {"action": "delete_bucket", "bucket": s(bucket), "priorState": prior_meta}


def remove_incomplete_uploads(
    conn: Any, bucket: str, older_than_days: int = DEFAULT_PURGE_OLDER_THAN_DAYS
) -> dict:
    """[WRITE][medium] Abort abandoned multipart uploads. priorState only, no undo.

    Only uploads at least ``older_than_days`` old are aborted (default 7),
    protecting legitimately in-flight uploads; pass 0 to purge everything.
    """
    check_bucket_name(bucket)
    if not isinstance(older_than_days, int) or older_than_days < 0:
        raise ValueError(f"older_than_days must be an integer >= 0 (got {older_than_days!r}).")
    uploads = conn.list_incomplete_uploads(bucket)
    victims = [
        u for u in uploads if (_age_days(u.get("initiated")) or 0) >= older_than_days
    ]
    aborted = 0
    failures: list[str] = []
    for upload in victims:
        try:
            conn.abort_incomplete_upload(bucket, upload["objectName"], upload["uploadId"])
            aborted += 1
        except Exception as exc:  # noqa: BLE001 — keep purging, report the stragglers
            failures.append(f"{s(upload.get('objectName'), 80)}: {s(exc, 100)}")
    result = {
        "action": "remove_incomplete_uploads",
        "bucket": s(bucket),
        "olderThanDays": older_than_days,
        "aborted": aborted,
        "priorState": {
            "incompleteUploads": len(uploads),
            "matchedForPurge": len(victims),
            "sample": [
                {"objectName": s(u.get("objectName"), 120), "initiated": s(u.get("initiated"))}
                for u in victims[:20]
            ],
        },
    }
    if failures:
        result["failures"] = failures
    return result
