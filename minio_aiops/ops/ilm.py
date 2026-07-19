"""Lifecycle / ILM gap analysis (read-only) — flagship analysis #3.

Finds the storage that ILM should be reclaiming but isn't:

  * versioned buckets with **no noncurrent-version expiry** rule — every
    overwrite/delete quietly keeps paying for the old bytes;
  * buckets with **incomplete multipart uploads** and no abort rule — parts of
    uploads that died mid-flight sit invisible to normal listings;
  * large buckets with no lifecycle at all.

Each gap carries cause + suggested action and a **reclaimable estimate** where
the metrics allow one (noncurrent bytes are estimated from the version/object
ratio — an estimate, clearly labelled, not an invoice).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from minio_aiops.ops._util import as_int, s
from minio_aiops.prom import by_label

M_BUCKET_USAGE = "minio_bucket_usage_total_bytes"
M_BUCKET_OBJECTS = "minio_bucket_usage_object_total"
M_BUCKET_VERSIONS = "minio_bucket_usage_version_total"

# Buckets with no lifecycle at all only become a finding above this size —
# flagging every 3-object test bucket would be noise.
LARGE_BUCKET_BYTES = 10 * 1024**3  # 10 GiB
# An incomplete upload older than this is treated as abandoned.
ABANDONED_UPLOAD_DAYS = 7


def _age_days(initiated_iso: str | None) -> float | None:
    if not initiated_iso:
        return None
    try:
        started = datetime.fromisoformat(initiated_iso)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (datetime.now(UTC) - started).total_seconds() / 86400


def _rules_have(rules: list[dict] | None, key: str) -> bool:
    return any(r.get(key) is not None and r.get("status") == "Enabled" for r in rules or [])


def lifecycle_gap_analysis(conn: Any, limit: int = 100) -> dict:
    """[READ] ILM gaps per bucket: what accumulates, why, and the fix."""
    requested = max(1, int(limit))
    try:
        buckets = list(conn.list_buckets())
    except Exception as exc:  # noqa: BLE001 — an analysis must survive a broken target
        return {"error": s(exc, 200)}
    try:
        metrics = conn.metrics()
    except Exception:  # noqa: BLE001 — degrade to structural analysis without sizes
        metrics = {}

    usage = by_label(metrics, M_BUCKET_USAGE, "bucket")
    objects = by_label(metrics, M_BUCKET_OBJECTS, "bucket")
    versions = by_label(metrics, M_BUCKET_VERSIONS, "bucket")

    findings: list[dict] = []
    total_reclaimable = 0.0
    for bucket_row in buckets[:requested]:
        name = bucket_row["name"]
        gaps: list[dict] = []
        errors: list[str] = []
        try:
            rules = conn.get_bucket_lifecycle(name)
        except Exception as exc:  # noqa: BLE001
            rules, errors = None, [f"lifecycle: {s(exc, 120)}"]
        try:
            versioning = conn.get_bucket_versioning(name)
        except Exception as exc:  # noqa: BLE001
            versioning = "unknown"
            errors.append(f"versioning: {s(exc, 120)}")

        bucket_bytes = usage.get(name)
        object_count = objects.get(name)
        version_count = versions.get(name)

        # Gap 1: versioning on, no noncurrent expiry — old versions accrue.
        if versioning == "Enabled" and not _rules_have(rules, "noncurrentExpirationDays"):
            reclaimable = None
            if (
                isinstance(bucket_bytes, (int, float))
                and isinstance(object_count, (int, float))
                and isinstance(version_count, (int, float))
                and version_count > object_count > 0
            ):
                extra = version_count - object_count
                reclaimable = bucket_bytes * (extra / version_count)
                total_reclaimable += reclaimable
            gaps.append(
                {
                    "gap": "NONCURRENT_VERSIONS_UNBOUNDED",
                    "cause": (
                        "Versioning is enabled but no noncurrent-version expiry rule "
                        "exists — every overwrite/delete keeps its old bytes forever"
                        + (
                            f" (~{int(version_count - object_count)} noncurrent versions)."
                            if isinstance(version_count, (int, float))
                            and isinstance(object_count, (int, float))
                            and version_count > object_count
                            else "."
                        )
                    ),
                    "suggestedAction": (
                        "Add a noncurrent expiry: set_lifecycle(bucket_name, "
                        "noncurrent_expire_days=30) — tune the days to your restore "
                        "window."
                    ),
                    "reclaimableEstimateBytes": (
                        int(reclaimable) if reclaimable is not None else None
                    ),
                }
            )

        # Gap 2: incomplete multipart uploads with no abort rule.
        if not _rules_have(rules, "abortIncompleteDays"):
            try:
                uploads = conn.list_incomplete_uploads(name)
            except Exception as exc:  # noqa: BLE001
                uploads = []
                errors.append(f"uploads: {s(exc, 120)}")
            abandoned = [
                u
                for u in uploads
                if (_age_days(u.get("initiated")) or 0) >= ABANDONED_UPLOAD_DAYS
            ]
            if uploads:
                ages = [a for a in (_age_days(u.get("initiated")) for u in uploads) if a]
                gaps.append(
                    {
                        "gap": "INCOMPLETE_UPLOADS_NO_ABORT_RULE",
                        "cause": (
                            f"{len(uploads)} incomplete multipart upload(s) "
                            f"({len(abandoned)} older than {ABANDONED_UPLOAD_DAYS}d, "
                            f"oldest ~{int(max(ages)) if ages else '?'}d) and no "
                            f"abort-incomplete rule — their parts hold space invisibly."
                        ),
                        "suggestedAction": (
                            "Reclaim now with remove_incomplete_uploads(bucket_name), "
                            "then prevent recurrence: set_lifecycle(bucket_name, "
                            "abort_incomplete_days=7)."
                        ),
                        "incompleteUploads": len(uploads),
                        "abandonedUploads": len(abandoned),
                    }
                )

        # Gap 3: big bucket, no lifecycle at all.
        if rules is None and isinstance(bucket_bytes, (int, float)) and (
            bucket_bytes >= LARGE_BUCKET_BYTES
        ):
            gaps.append(
                {
                    "gap": "NO_LIFECYCLE_ON_LARGE_BUCKET",
                    "cause": (
                        f"The bucket holds {int(bucket_bytes)} bytes with no lifecycle "
                        f"rules — nothing ever expires."
                    ),
                    "suggestedAction": (
                        "If the data ages out, add set_lifecycle(bucket_name, "
                        "expire_days=...); if it must be kept, this is informational."
                    ),
                }
            )

        if gaps:
            row = {
                "bucket": s(name),
                "versioning": s(versioning),
                "usedBytes": as_int(bucket_bytes),
                "gaps": gaps,
            }
            if errors:
                row["errors"] = errors
            findings.append(row)

    findings.sort(key=lambda r: -(r.get("usedBytes") or 0))
    return {
        "bucketsAnalyzed": min(len(buckets), requested),
        "bucketsTotal": len(buckets),
        "limit": requested,
        "truncated": len(buckets) > requested,
        "bucketsWithGaps": len(findings),
        "totalReclaimableEstimateBytes": int(total_reclaimable),
        "note": "Reclaimable bytes are estimated from version/object counts, not an exact scan.",
        "findings": findings,
    }
