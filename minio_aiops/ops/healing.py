"""Healing & erasure-set health RCA (read-only) — flagship analysis #4.

Reads the cluster metrics for self-healing progress and per-erasure-set drive
counts / write quorum, then answers the question raw gauges don't: **how many
more drive failures can each set tolerate, and is healing keeping up?**

Thresholds are named constants; every finding carries cause + suggested action.
"""

from __future__ import annotations

from typing import Any

from minio_aiops.ops._util import as_int, pct, s
from minio_aiops.prom import first_value, sum_values

# Erasure-set metrics (labels: pool, set).
M_SET_ONLINE = "minio_cluster_health_erasure_set_online_drives"
M_SET_HEALING = "minio_cluster_health_erasure_set_healing_drives"
M_SET_READ_QUORUM = "minio_cluster_health_erasure_set_read_quorum"
M_SET_WRITE_QUORUM = "minio_cluster_health_erasure_set_write_quorum"
M_SET_STATUS = "minio_cluster_health_erasure_set_status"
M_HEALTH_STATUS = "minio_cluster_health_status"

# Heal-activity metrics.
M_HEAL_SCANNED = "minio_heal_objects_total"
M_HEAL_HEALED = "minio_heal_objects_heal_total"
M_HEAL_ERRORS = "minio_heal_objects_errors_total"
M_HEAL_LAST_ACTIVITY_NS = "minio_heal_time_last_activity_nano_seconds"

M_DRIVES_OFFLINE = "minio_cluster_drive_offline_total"

# Tolerance thresholds: remaining failures before write quorum is lost.
TOLERANCE_CRITICAL = 0  # at/below write quorum — one more failure loses writes
TOLERANCE_WARNING = 1  # a single further drive failure reaches the edge


def _set_key(sample: dict) -> str:
    labels = sample.get("labels", {})
    return f"pool={labels.get('pool', '?')},set={labels.get('set', '?')}"


def _per_set(metrics: dict, name: str) -> dict[str, float]:
    return {_set_key(x): x["value"] for x in metrics.get(name) or []}


def healing_health(conn: Any) -> dict:
    """[READ] Healing backlog + per-erasure-set write-quorum risk, cause + action."""
    try:
        metrics = conn.metrics()
    except Exception as exc:  # noqa: BLE001 — an RCA must survive a broken scrape
        return {"error": s(exc, 200)}

    online = _per_set(metrics, M_SET_ONLINE)
    healing = _per_set(metrics, M_SET_HEALING)
    write_q = _per_set(metrics, M_SET_WRITE_QUORUM)
    read_q = _per_set(metrics, M_SET_READ_QUORUM)

    sets: list[dict] = []
    findings: list[dict] = []
    for key, online_drives in online.items():
        wq = write_q.get(key)
        tolerance = int(online_drives - wq) if isinstance(wq, (int, float)) else None
        healing_drives = int(healing.get(key, 0))
        sets.append(
            {
                "erasureSet": s(key),
                "onlineDrives": int(online_drives),
                "healingDrives": healing_drives,
                "readQuorum": int(read_q[key]) if key in read_q else None,
                "writeQuorum": int(wq) if isinstance(wq, (int, float)) else None,
                "failureToleranceRemaining": tolerance,
            }
        )
        if tolerance is not None and tolerance < TOLERANCE_CRITICAL:
            findings.append(
                {
                    "issue": "WRITE_QUORUM_LOST",
                    "erasureSet": s(key),
                    "severity": "critical",
                    "cause": (
                        f"Erasure set {key} has {int(online_drives)} drives online, below "
                        f"its write quorum of {int(wq)} — writes to this set are failing."
                    ),
                    "suggestedAction": (
                        "Bring offline drives/nodes back immediately (drive_status, "
                        "node_status); data in this set is read-only at best until quorum "
                        "is restored."
                    ),
                }
            )
        elif tolerance is not None and tolerance <= TOLERANCE_CRITICAL:
            findings.append(
                {
                    "issue": "WRITE_QUORUM_AT_EDGE",
                    "erasureSet": s(key),
                    "severity": "critical",
                    "cause": (
                        f"Erasure set {key} is exactly at write quorum "
                        f"({int(online_drives)} online = quorum {int(wq)}) — one more "
                        f"drive failure stops writes."
                    ),
                    "suggestedAction": (
                        "Replace the failed drives now and let healing finish before any "
                        "maintenance that takes further drives offline."
                    ),
                }
            )
        elif tolerance is not None and tolerance <= TOLERANCE_WARNING:
            findings.append(
                {
                    "issue": "LOW_FAILURE_TOLERANCE",
                    "erasureSet": s(key),
                    "severity": "warning",
                    "cause": (
                        f"Erasure set {key} can tolerate only {tolerance} more drive "
                        f"failure(s) before losing write quorum."
                    ),
                    "suggestedAction": (
                        "Schedule drive replacement; avoid rolling restarts that overlap "
                        "with the degraded set."
                    ),
                }
            )
        if healing_drives:
            findings.append(
                {
                    "issue": "HEALING_IN_PROGRESS",
                    "erasureSet": s(key),
                    "severity": "info",
                    "cause": f"{healing_drives} drive(s) in erasure set {key} are healing "
                    f"(fresh replacements re-filling).",
                    "suggestedAction": (
                        "Normal after a replacement — watch the heal backlog below; "
                        "tolerance is reduced until healing completes."
                    ),
                }
            )

    scanned = first_value(metrics, M_HEAL_SCANNED)
    healed = first_value(metrics, M_HEAL_HEALED)
    errors = first_value(metrics, M_HEAL_ERRORS)
    backlog = (
        int(scanned - healed)
        if isinstance(scanned, (int, float)) and isinstance(healed, (int, float))
        else None
    )
    if isinstance(errors, (int, float)) and errors > 0:
        findings.append(
            {
                "issue": "HEAL_ERRORS",
                "severity": "warning",
                "cause": f"The self-healing scanner reported {int(errors)} object heal "
                f"error(s) in the current run.",
                "suggestedAction": (
                    "Inspect server logs for the failing objects; persistent heal errors "
                    "usually mean a bad drive sector or a missing part."
                ),
            }
        )

    return {
        "healthy": not any(f["severity"] in ("critical", "warning") for f in findings),
        # Counts, not ratios: a gauge parses to float, and "4.0 objects scanned"
        # is the int-as-float class this line has fixed elsewhere. Only appears
        # once a heal has actually run — verified by taking a node offline.
        "clusterHealthStatus": as_int(first_value(metrics, M_HEALTH_STATUS)),
        "drivesOffline": int(sum_values(metrics, M_DRIVES_OFFLINE) or 0),
        "healBacklogObjects": as_int(backlog),
        "healObjectsScanned": as_int(scanned),
        "healObjectsHealed": as_int(healed),
        "healErrors": as_int(errors),
        "erasureSets": sets,
        "findings": findings,
    }


M_DRIVE_FREE = "minio_node_drive_free_bytes"
M_DRIVE_TOTAL = "minio_node_drive_total_bytes"
M_DRIVE_OFFLINE_NODE = "minio_node_drive_offline_total"
M_DRIVE_ONLINE_NODE = "minio_node_drive_online_total"


def drive_status(conn: Any) -> dict:
    """[READ] Per-drive rows (server, drive, used ratio), fullest first.

    Returns an envelope, not a bare list: an empty list alone cannot say whether
    the server genuinely has no drives or the metrics scrape failed. This
    previously swallowed every scrape error and returned ``[]`` — which is how a
    metric-name mismatch went unnoticed, since "no drives" looked like success.
    """
    try:
        metrics = conn.metrics()
    except Exception as exc:  # noqa: BLE001 — reported, never silently swallowed
        return {"drives": [], "returned": 0, "error": s(exc, 200)}
    total = {}
    free = {}
    for sample in metrics.get(M_DRIVE_TOTAL) or []:
        labels = sample["labels"]
        key = (labels.get("server", "?"), labels.get("drive", labels.get("path", "?")))
        total[key] = sample["value"]
    for sample in metrics.get(M_DRIVE_FREE) or []:
        labels = sample["labels"]
        key = (labels.get("server", "?"), labels.get("drive", labels.get("path", "?")))
        free[key] = sample["value"]
    rows = []
    for key, tot in total.items():
        server, drive = key
        used = tot - free[key] if key in free else None
        rows.append(
            {
                "server": s(server),
                "drive": s(drive),
                "totalBytes": as_int(tot),
                "freeBytes": as_int(free.get(key)),
                "usedRatio": pct(used, tot),
            }
        )
    rows.sort(key=lambda r: -(r["usedRatio"] or 0))
    # ``minio_node_drive_*`` is scraped from ONE server and describes only that
    # server's drives, while ``minio_cluster_drive_online_total`` — from the same
    # scrape — counts the whole deployment. On a real 4-node distributed MinIO
    # this listed 1 drive and said returned: 1, with nothing to distinguish "this
    # server has one drive" from "three more servers were never looked at". The
    # scope marker travels with the rows.
    cluster_drives = as_int(sum_values(metrics, "minio_cluster_drive_online_total"))
    out = {
        "drives": rows,
        "returned": len(rows),
        "scope": "node",
        "clusterDrivesOnline": cluster_drives,
        "error": None,
    }
    if cluster_drives and cluster_drives > len(rows):
        out["note"] = (
            f"Per-drive detail covers only the server queried here ({len(rows)} "
            f"drive(s)); the deployment reports {cluster_drives} drives online. "
            f"Query each server for the rest."
        )
    return out


def node_status(conn: Any) -> dict:
    """[READ] Node-level view: online/offline nodes + per-node drive counts."""
    try:
        metrics = conn.metrics()
    except Exception as exc:  # noqa: BLE001
        return {"error": s(exc, 200)}
    per_node_offline = {}
    per_node_online = {}
    for sample in metrics.get(M_DRIVE_OFFLINE_NODE) or []:
        per_node_offline[sample["labels"].get("server", "?")] = sample["value"]
    for sample in metrics.get(M_DRIVE_ONLINE_NODE) or []:
        per_node_online[sample["labels"].get("server", "?")] = sample["value"]
    servers = sorted(set(per_node_offline) | set(per_node_online))
    # Node and drive counts are counts: as_int, not the float a gauge parses to
    # (a deployment reporting "8.0 nodes online" is the int-as-float class).
    return {
        "nodesOnline": as_int(sum_values(metrics, "minio_cluster_nodes_online_total")),
        "nodesOffline": as_int(sum_values(metrics, "minio_cluster_nodes_offline_total")),
        "nodes": [
            {
                "server": s(server),
                "drivesOnline": as_int(per_node_online.get(server)),
                "drivesOffline": as_int(per_node_offline.get(server)),
            }
            for server in servers
        ],
        # Per-node rows come from this server's own /node scrape, so a
        # distributed deployment lists one row while nodesOnline counts them all.
        "scope": "node",
    }
