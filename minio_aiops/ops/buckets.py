"""Bucket inventory + per-bucket configuration reads.

Pure reads: list buckets, fold one bucket's whole configuration surface
(policy/versioning/lifecycle/encryption/quota/tags) into a single teaching
summary, bounded object listing, and incomplete-upload listing. All
agent-supplied bucket names pass ``check_bucket_name`` first.
"""

from __future__ import annotations

from typing import Any

from minio_aiops.ops._util import check_bucket_name, opt_s, s
from minio_aiops.ops.exposure import _anonymous_statements

#: Default cap on a bucket listing. Deployments with thousands of buckets
#: would otherwise blow an agent's context on one call.
DEFAULT_BUCKET_LIMIT = 500
#: Default cap on an incomplete-upload listing.
DEFAULT_UPLOAD_LIMIT = 200


def list_buckets(conn: Any, limit: int = DEFAULT_BUCKET_LIMIT) -> dict:
    """[READ] Buckets (name + creation time), capped at ``limit``.

    Returns an envelope rather than a bare list::

        {"buckets": [...], "returned": N, "limit": L, "truncated": true/false}

    so a truncated read announces itself. A bare list cannot say "there is
    more" — the consumer has to infer it from the length happening to equal
    the limit, and a smaller local model faced with a long result tends to
    report that nothing came back at all. ``list_buckets`` is a single
    non-paginated S3 call, so ``truncated`` is measured against the *full*
    list length rather than a fetched extra row — measured either way, never
    guessed.
    """
    requested = max(1, int(limit))
    raw = list(conn.list_buckets())
    truncated = len(raw) > requested
    buckets = [
        {"bucket": opt_s(b.get("name")), "createdAt": opt_s(b.get("createdAt"))}
        for b in raw[:requested]
    ]
    return {
        "buckets": buckets,
        "returned": len(buckets),
        "limit": requested,
        "truncated": truncated,
    }


def bucket_info(conn: Any, bucket: str) -> dict:
    """[READ] One bucket's full config surface; per-probe failures degrade."""
    check_bucket_name(bucket)
    out: dict[str, Any] = {"bucket": s(bucket)}
    errors: list[str] = []

    try:
        policy = conn.get_bucket_policy(bucket)
        public_read, public_write = _anonymous_statements(policy)
        out["policy"] = {
            "present": policy is not None,
            "publicRead": public_read,
            "publicWrite": public_write,
        }
    except Exception as exc:  # noqa: BLE001 — collect, keep going
        errors.append(f"policy: {s(exc, 120)}")
    try:
        out["versioning"] = conn.get_bucket_versioning(bucket)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"versioning: {s(exc, 120)}")
    try:
        out["lifecycleRules"] = conn.get_bucket_lifecycle(bucket)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"lifecycle: {s(exc, 120)}")
    try:
        out["encryption"] = conn.get_bucket_encryption(bucket)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"encryption: {s(exc, 120)}")
    try:
        out["quota"] = conn.get_bucket_quota(bucket)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"quota: {s(exc, 120)}")
    try:
        out["tags"] = conn.get_bucket_tags(bucket)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tags: {s(exc, 120)}")

    if errors:
        out["errors"] = errors
    return out


def get_bucket_policy(conn: Any, bucket: str) -> dict:
    """[READ] The bucket's policy JSON (verbatim) + anonymous-access summary."""
    check_bucket_name(bucket)
    policy = conn.get_bucket_policy(bucket)
    public_read, public_write = _anonymous_statements(policy)
    return {
        "bucket": s(bucket),
        "policyJson": policy,
        "publicRead": public_read,
        "publicWrite": public_write,
    }


def get_bucket_lifecycle(conn: Any, bucket: str) -> dict:
    """[READ] The bucket's lifecycle rules (None when no lifecycle is set)."""
    check_bucket_name(bucket)
    return {"bucket": s(bucket), "lifecycleRules": conn.get_bucket_lifecycle(bucket)}


def get_bucket_versioning(conn: Any, bucket: str) -> dict:
    """[READ] The bucket's versioning state: Enabled / Suspended / Off."""
    check_bucket_name(bucket)
    return {"bucket": s(bucket), "versioning": conn.get_bucket_versioning(bucket)}


def get_bucket_quota(conn: Any, bucket: str) -> dict:
    """[READ] The bucket's hard quota (0 = unlimited). Needs admin credentials."""
    check_bucket_name(bucket)
    return {"bucket": s(bucket), **conn.get_bucket_quota(bucket)}


def list_objects(conn: Any, bucket: str, prefix: str = "", limit: int = 100) -> dict:
    """[READ] Objects under ``prefix``, capped at ``limit``, in an envelope.

    Returns::

        {"objects": [...], "returned": N, "limit": L, "truncated": true/false}

    Object listings are the single most likely result to be cut off — a bucket
    can hold millions of keys. One extra object is requested from the paged
    listing so ``truncated`` is **measured**, not guessed from the returned
    count happening to equal the limit.

    ``lastModified`` and ``versionId`` come back as ``null`` when the source
    had no value, never as ``""``.
    """
    check_bucket_name(bucket)
    requested = max(1, min(int(limit), 1000))
    raw = list(conn.list_objects_page(bucket, prefix=prefix, limit=requested + 1))
    truncated = len(raw) > requested
    objects = [
        {
            "objectName": opt_s(o.get("objectName"), 1024),
            "sizeBytes": o.get("sizeBytes"),
            "lastModified": opt_s(o.get("lastModified")),
            "isLatest": o.get("isLatest"),
            "versionId": opt_s(o.get("versionId")),
        }
        for o in raw[:requested]
    ]
    return {
        "objects": objects,
        "returned": len(objects),
        "limit": requested,
        "truncated": truncated,
    }


def list_incomplete_uploads(
    conn: Any, bucket: str, prefix: str = "", limit: int = DEFAULT_UPLOAD_LIMIT
) -> dict:
    """[READ] In-flight/abandoned multipart uploads for one bucket, in an envelope.

    Returns::

        {"uploads": [...], "returned": N, "limit": L, "truncated": true/false}

    The underlying ListMultipartUploads walk returns the complete set, so
    ``truncated`` is measured against the full length before slicing.
    ``initiated`` is ``null`` when the source gave no time, never ``""``.
    """
    check_bucket_name(bucket)
    requested = max(1, int(limit))
    raw = list(conn.list_incomplete_uploads(bucket, prefix=prefix))
    truncated = len(raw) > requested
    uploads = [
        {
            "objectName": opt_s(u.get("objectName"), 1024),
            "uploadId": opt_s(u.get("uploadId")),
            "initiated": opt_s(u.get("initiated")),
        }
        for u in raw[:requested]
    ]
    return {
        "uploads": uploads,
        "returned": len(uploads),
        "limit": requested,
        "truncated": truncated,
    }


def server_info(conn: Any) -> dict:
    """[READ] Admin server info: mode, pools/sets summary, version."""
    info = conn.server_info()
    return {
        "mode": opt_s(info.get("mode")),
        "deploymentId": opt_s(info.get("deploymentID") or info.get("deploymentId")),
        "servers": len(info.get("servers") or []),
        "pools": len(info.get("pools") or {}) or None,
        "raw": {
            k: info.get(k)
            for k in ("mode", "region", "sqsARN", "backend")
            if info.get(k) is not None
        },
    }
