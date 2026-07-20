"""Guarded bucket writes (policy / versioning / lifecycle / quota / delete / uploads).

Every reversible write captures the bucket's BEFORE state into ``priorState``
so the harness can record a faithful undo (restore the prior policy JSON,
lifecycle XML, versioning state, or quota). The two footguns — deleting a
bucket (only allowed when verifiably empty) and purging incomplete uploads
(the parts are unrecoverable) — record ``priorState`` only: there is honestly
no undo, and pretending otherwise would be worse than saying so.

``set_bucket_policy`` additionally refuses a policy that would revoke this
tool's own ability to put a policy (:class:`SelfLockout`). An explicit Deny
beats any IAM Allow, so a statement denying ``s3:PutBucketPolicy`` to the
configured access key makes the undo — which replays the prior policy — denied
in turn. This tool has no IAM surface at all, so a bucket policy is the only
way it can revoke its own access, and there would be no way back in-tool.

All bucket names pass ``check_bucket_name`` (the injection gate) first.
"""

from __future__ import annotations

import json
from typing import Any

from minio_aiops.ops._util import check_bucket_name, opt_s, s
from minio_aiops.ops.ilm import _age_days

VERSIONING_STATES = ("Enabled", "Suspended")
# Only uploads at least this old are purged by default — an in-flight upload
# younger than this is plausibly still legitimate.
DEFAULT_PURGE_OLDER_THAN_DAYS = 7

# The action that puts a policy. Denying it to ourselves is what makes a policy
# write irreversible; s3:* covers it too, and so does the s3:PutBucket* glob.
_PUT_POLICY_ACTION = "s3:putbucketpolicy"
_WILDCARD_ACTIONS = ("*", "s3:*")


class SelfLockout(ValueError):  # noqa: N818 — teaching error, reads as a statement
    """Refused: the operation would revoke this tool's own access to the target."""


def _as_list(value: Any) -> list[str]:
    """S3 policy fields are 'string or list of string' everywhere."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def _covers_put_policy(actions: list[str]) -> bool:
    """Whether an Action set covers s3:PutBucketPolicy (literal, glob or s3:*)."""
    for action in actions:
        normalized = action.strip().lower()
        if normalized in _WILDCARD_ACTIONS or normalized == _PUT_POLICY_ACTION:
            return True
        # A trailing glob such as 's3:PutBucket*' or 's3:Put*'.
        if normalized.endswith("*") and _PUT_POLICY_ACTION.startswith(normalized[:-1]):
            return True
    return False


def _principals(statement: dict) -> list[str]:
    """Flatten a Principal field, which may be '*', a list, or {'AWS': [...]}."""
    principal = statement.get("Principal")
    if isinstance(principal, dict):
        flattened: list[str] = []
        for value in principal.values():
            flattened.extend(_as_list(value))
        return flattened
    return _as_list(principal)


def _hits_self(statement: dict, access_key: str) -> bool:
    """Whether a Deny statement's Principal covers the configured access key.

    ``*`` matches everyone, this tool included. Otherwise the access key has to
    appear — either bare or as the tail of an ARN (``arn:aws:iam::…:user/KEY``).
    """
    for principal in _principals(statement):
        candidate = principal.strip()
        if candidate == "*":
            return True
        if access_key and (candidate == access_key or candidate.endswith(f"/{access_key}")):
            return True
    return False


def guard_set_bucket_policy(conn: Any, policy_json: str) -> None:
    """Raise the :class:`SelfLockout` ``set_bucket_policy`` would raise, without writing.

    Called by ``set_bucket_policy`` itself *and* by the MCP wrapper ahead of its
    ``dry_run`` early return, so a preview of a self-denying policy reports the
    refusal instead of a green ``wouldSetPolicy``. Both paths run this one
    function, so the preview and the real call can never disagree.

    Detection is purely local — the submitted JSON plus ``target.access_key`` —
    so there is no round trip and no unknown-identity case. A malformed document
    is left to the caller's own validation.
    """
    try:
        parsed = json.loads(policy_json)
    except (TypeError, ValueError):
        return  # not our error to raise; set_bucket_policy reports it properly
    if not isinstance(parsed, dict):
        return
    raw_key = getattr(getattr(conn, "target", None), "access_key", "")
    # Only a real string identifies us; anything else is treated as "no key",
    # which still catches the Principal:"*" case (that one denies everyone).
    access_key = raw_key if isinstance(raw_key, str) else ""
    for statement in parsed.get("Statement") or []:
        if not isinstance(statement, dict):
            continue
        if str(statement.get("Effect", "")).strip().lower() != "deny":
            continue
        if not _covers_put_policy(_as_list(statement.get("Action"))):
            continue
        if not _hits_self(statement, access_key):
            continue
        raise SelfLockout(
            "Refusing this policy: it contains an explicit Deny on "
            "s3:PutBucketPolicy that covers the access key this tool "
            "authenticates with. An explicit Deny beats every Allow, so the "
            "policy would apply and the undo that replays the prior policy "
            "would itself be denied — this tool has no IAM surface, so there "
            "would be no way back. Scope the Deny to a specific other "
            "principal, or apply it with mc/an admin credential that keeps a "
            "route back in."
        )


def set_bucket_policy(conn: Any, bucket: str, policy_json: str) -> dict:
    """[WRITE][medium] Replace the bucket policy. Reversible → prior policy JSON.

    **Refuses a policy that denies ``s3:PutBucketPolicy`` to this tool's own
    access key** (directly, via ``s3:*``, or via a ``*`` Principal) — that write
    would revoke the permission its own undo needs. Note this only bites when
    the tool is configured with a non-root key: ``MINIO_ROOT_USER`` bypasses
    policy evaluation entirely, and both configurations are in the field.
    """
    check_bucket_name(bucket)
    guard_set_bucket_policy(conn, policy_json)
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

    The undo restores the RULE, not the data. Between this call and the undo,
    MinIO applies the rule: objects it expires are deleted, and putting the
    prior XML back does not bring them back. Reversible here means the
    configuration returns, exactly as ``delete_queue`` in the sibling queue tool
    means the queue returns and its messages do not.
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
        "note": (
            "Undo restores the prior lifecycle configuration; objects this rule "
            "expires in the meantime are NOT restored."
        ),
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
                {
                    "objectName": opt_s(u.get("objectName"), 120),
                    "initiated": opt_s(u.get("initiated")),
                }
                for u in victims[:20]
            ],
        },
    }
    if failures:
        result["failures"] = failures
    return result
