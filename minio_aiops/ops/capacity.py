"""Capacity & usage RCA (read-only) — flagship analysis #1.

Takes the cluster metrics (capacity vs used, per-drive usage, offline
drives/nodes) and folds them into ranked findings, each with a plain-language
**cause + suggested action** — the answer to "why is the cluster filling up /
refusing writes, and what do I do about it".

Thresholds are named constants so operators can read (and PRs can tune) the
policy in one place.
"""

from __future__ import annotations

from typing import Any

from minio_aiops.ops import health as h
from minio_aiops.ops._util import pct, s
from minio_aiops.prom import by_label, first_value, sum_values

# ── thresholds (ratios 0-1) ────────────────────────────────────────────────
NEARFULL_RATIO = 0.85  # usable capacity used — warn: plan expansion/cleanup
FULL_RATIO = 0.95  # usable capacity used — critical: writes at risk
DRIVE_NEARFULL_RATIO = 0.90  # single drive used — hotspot
IMBALANCE_SPREAD = 0.25  # max-min drive usage ratio — rebalance hint
# The RCA carries only the fullest drives inline; the count is reported next to
# them (drivesTotal/drivesTruncated) so a big cluster's tail is not silently
# dropped. Use drive_status for the complete per-drive list.
DRIVE_ROWS_SHOWN = 24

M_DRIVE_USED = "minio_node_drive_used_bytes"
M_DRIVE_TOTAL = "minio_node_drive_total_bytes"
M_BUCKET_USAGE = "minio_bucket_usage_total_bytes"
M_BUCKET_OBJECTS = "minio_bucket_usage_object_total"


def _drive_key(sample: dict) -> str:
    labels = sample.get("labels", {})
    return f"{labels.get('server', '?')}:{labels.get('drive', labels.get('path', '?'))}"


def _drive_usage(metrics: dict) -> list[dict]:
    used = {_drive_key(x): x["value"] for x in metrics.get(M_DRIVE_USED) or []}
    total = {_drive_key(x): x["value"] for x in metrics.get(M_DRIVE_TOTAL) or []}
    rows = []
    for key, tot in total.items():
        ratio = pct(used.get(key), tot)
        rows.append(
            {
                "drive": s(key),
                "usedBytes": used.get(key),
                "totalBytes": tot,
                "usedRatio": ratio,
            }
        )
    return sorted(rows, key=lambda r: -(r["usedRatio"] or 0))


def capacity_rca(conn: Any) -> dict:
    """[READ] Capacity & usage RCA: cause + suggested action per finding."""
    try:
        metrics = conn.metrics()
    except Exception as exc:  # noqa: BLE001 — an RCA must survive a broken scrape
        return {"error": s(exc, 200)}

    usable_total = first_value(metrics, h.M_CAP_USABLE_TOTAL)
    usable_free = first_value(metrics, h.M_CAP_USABLE_FREE)
    used = (
        usable_total - usable_free
        if isinstance(usable_total, (int, float)) and isinstance(usable_free, (int, float))
        else None
    )
    used_ratio = pct(used, usable_total)
    drives_offline = sum_values(metrics, h.M_DRIVES_OFFLINE) or 0
    nodes_offline = sum_values(metrics, h.M_NODES_OFFLINE) or 0
    drives = _drive_usage(metrics)

    findings: list[dict] = []
    if used_ratio is not None and used_ratio >= FULL_RATIO:
        findings.append(
            {
                "issue": "CLUSTER_FULL",
                "severity": "critical",
                "cause": (
                    f"Usable capacity is {used_ratio:.0%} used (>= {FULL_RATIO:.0%}) — "
                    f"writes will start failing when free space runs out."
                ),
                "suggestedAction": (
                    "Reclaim space now: run lifecycle_gap_analysis to find expired "
                    "versions and abandoned multipart uploads (remove_incomplete_uploads), "
                    "then plan a pool/drive expansion."
                ),
            }
        )
    elif used_ratio is not None and used_ratio >= NEARFULL_RATIO:
        findings.append(
            {
                "issue": "CLUSTER_NEARFULL",
                "severity": "warning",
                "cause": (
                    f"Usable capacity is {used_ratio:.0%} used (>= {NEARFULL_RATIO:.0%}) — "
                    f"headroom is shrinking."
                ),
                "suggestedAction": (
                    "Check usage_by_bucket for the biggest consumers, add lifecycle "
                    "expiry where data is disposable (set_lifecycle), and schedule "
                    "capacity expansion before the full threshold."
                ),
            }
        )
    if drives_offline:
        findings.append(
            {
                "issue": "DRIVES_OFFLINE",
                "severity": "critical",
                "cause": (
                    f"{int(drives_offline)} drive(s) offline — raw capacity and "
                    f"failure tolerance are reduced while data re-heals onto the rest."
                ),
                "suggestedAction": (
                    "Run drive_status/healing_health to locate the offline drives and "
                    "the erasure sets at risk; replace or remount the drives."
                ),
            }
        )
    if nodes_offline:
        findings.append(
            {
                "issue": "NODES_OFFLINE",
                "severity": "critical",
                "cause": f"{int(nodes_offline)} node(s) offline — their drives are unavailable.",
                "suggestedAction": (
                    "Check node_status and the hosts themselves (service, network); "
                    "restore the nodes before capacity/quorum degrade further."
                ),
            }
        )
    hot = [d for d in drives if (d["usedRatio"] or 0) >= DRIVE_NEARFULL_RATIO]
    if hot:
        findings.append(
            {
                "issue": "DRIVE_HOTSPOT",
                "severity": "warning",
                "cause": (
                    f"{len(hot)} drive(s) over {DRIVE_NEARFULL_RATIO:.0%} used "
                    f"(worst: {hot[0]['drive']}) — uneven pool fill or undersized drives."
                ),
                "suggestedAction": (
                    "Verify pools/erasure sets are sized evenly; expansion adds a new "
                    "pool rather than growing hot drives."
                ),
            }
        )
    if len(drives) >= 2:
        ratios = [d["usedRatio"] for d in drives if d["usedRatio"] is not None]
        if ratios and (max(ratios) - min(ratios)) >= IMBALANCE_SPREAD:
            findings.append(
                {
                    "issue": "DRIVE_IMBALANCE",
                    "severity": "info",
                    "cause": (
                        f"Drive usage spread is {max(ratios) - min(ratios):.0%} "
                        f"(>= {IMBALANCE_SPREAD:.0%}) — pools are filling unevenly."
                    ),
                    "suggestedAction": (
                        "Newer pools absorb new writes first; this evens out over time, "
                        "but check for a pool added much later than the rest."
                    ),
                }
            )

    return {
        "healthy": not findings,
        "usableTotalBytes": usable_total,
        "usableUsedBytes": used,
        "usedRatio": used_ratio,
        "drivesOffline": int(drives_offline),
        "nodesOffline": int(nodes_offline),
        "drives": drives[:DRIVE_ROWS_SHOWN],
        "drivesReturned": min(len(drives), DRIVE_ROWS_SHOWN),
        "drivesTotal": len(drives),
        "drivesTruncated": len(drives) > DRIVE_ROWS_SHOWN,
        "findings": findings,
    }


def usage_by_bucket(conn: Any, limit: int = 25) -> dict:
    """[READ] Per-bucket usage (bytes + objects), biggest first, in an envelope.

    Returns::

        {"buckets": [...], "returned": N, "limit": L, "truncated": true/false}

    so a cut-off "who is using the space" answer says so rather than looking
    complete. The metrics scrape returns every bucket at once, so ``truncated``
    is measured against the full row count before slicing — never guessed from
    the returned count equalling the limit.

    Resilient by design: a failing metrics scrape returns an empty envelope.
    """
    requested = max(1, int(limit))
    try:
        metrics = conn.metrics()
    except Exception:  # noqa: BLE001 — a usage probe must survive a broken scrape
        return {"buckets": [], "returned": 0, "limit": requested, "truncated": False}
    usage = by_label(metrics, M_BUCKET_USAGE, "bucket")
    objects = by_label(metrics, M_BUCKET_OBJECTS, "bucket")
    rows = [
        {"bucket": s(name), "usedBytes": val, "objects": objects.get(name)}
        for name, val in usage.items()
    ]
    rows.sort(key=lambda r: -(r["usedBytes"] or 0))
    truncated = len(rows) > requested
    kept = rows[:requested]
    return {
        "buckets": kept,
        "returned": len(kept),
        "limit": requested,
        "truncated": truncated,
    }
