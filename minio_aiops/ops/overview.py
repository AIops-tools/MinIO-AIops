"""One-shot MinIO deployment overview (read-only).

Folds service health, cluster status, and the exposure headline into a single
summary an agent can call first. Resilient — a failing sub-call degrades to a
partial summary with an ``errors`` list rather than a raised traceback.
"""

from __future__ import annotations

from typing import Any

from minio_aiops.ops import exposure as e
from minio_aiops.ops import health as h


def fleet_overview(conn: Any) -> dict:
    """[READ] Deployment summary: health, capacity headline, exposure headline."""
    errors: list[str] = []

    def _safe(fn: Any, label: str, default: Any) -> Any:
        try:
            return fn(conn)
        except Exception as exc:  # noqa: BLE001 — collect, keep going
            errors.append(f"{label}: {str(exc)[:120]}")
            return default

    health = _safe(h.service_health, "health", {})
    status = _safe(h.cluster_status, "status", {})
    audit = _safe(e.bucket_exposure_audit, "exposure", {})

    return {
        "healthy": bool(health.get("healthy")),
        "degraded": bool(status.get("degraded")),
        "usableUsedRatio": status.get("usableUsedRatio"),
        "drivesOffline": status.get("drivesOffline"),
        "nodesOffline": status.get("nodesOffline"),
        "buckets": status.get("buckets"),
        "objects": status.get("objects"),
        "bucketsAtRisk": audit.get("atRisk"),
        "highRiskBuckets": audit.get("highRisk"),
        "errors": errors,
    }
