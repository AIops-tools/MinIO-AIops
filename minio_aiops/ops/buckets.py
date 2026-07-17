"""Bucket inventory + per-bucket configuration reads.

Pure reads: list buckets, fold one bucket's whole configuration surface
(policy/versioning/lifecycle/encryption/quota/tags) into a single teaching
summary, bounded object listing, and incomplete-upload listing. All
agent-supplied bucket names pass ``check_bucket_name`` first.
"""

from __future__ import annotations

from typing import Any

from minio_aiops.ops._util import check_bucket_name, s
from minio_aiops.ops.exposure import _anonymous_statements


def list_buckets(conn: Any) -> list[dict]:
    """[READ] All buckets: name + creation time."""
    return [
        {"bucket": s(b.get("name")), "createdAt": s(b.get("createdAt"))}
        for b in conn.list_buckets()
    ]


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


def list_objects(conn: Any, bucket: str, prefix: str = "", limit: int = 100) -> list[dict]:
    """[READ] First ``limit`` objects under ``prefix`` (bounded listing)."""
    check_bucket_name(bucket)
    return conn.list_objects_page(bucket, prefix=prefix, limit=max(1, min(limit, 1000)))


def list_incomplete_uploads(conn: Any, bucket: str, prefix: str = "") -> list[dict]:
    """[READ] In-flight/abandoned multipart uploads for one bucket."""
    check_bucket_name(bucket)
    return conn.list_incomplete_uploads(bucket, prefix=prefix)


def server_info(conn: Any) -> dict:
    """[READ] Admin server info: mode, pools/sets summary, version."""
    info = conn.server_info()
    return {
        "mode": s(info.get("mode")),
        "deploymentId": s(info.get("deploymentID") or info.get("deploymentId")),
        "servers": len(info.get("servers") or []),
        "pools": len(info.get("pools") or {}) or None,
        "raw": {
            k: info.get(k)
            for k in ("mode", "region", "sqsARN", "backend")
            if info.get(k) is not None
        },
    }
