"""Healing/erasure-set ops tests: quorum tolerance, backlog, drive/node views."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import healing as ops

pytestmark = pytest.mark.unit


def _set_metrics(sets):
    """sets: list of (pool, set, online, healing, write_q, read_q)."""
    m = {ops.M_SET_ONLINE: [], ops.M_SET_HEALING: [], ops.M_SET_WRITE_QUORUM: [],
         ops.M_SET_READ_QUORUM: []}
    for pool, es, online, healing, wq, rq in sets:
        labels = {"pool": str(pool), "set": str(es)}
        m[ops.M_SET_ONLINE].append({"labels": labels, "value": float(online)})
        m[ops.M_SET_HEALING].append({"labels": labels, "value": float(healing)})
        m[ops.M_SET_WRITE_QUORUM].append({"labels": labels, "value": float(wq)})
        m[ops.M_SET_READ_QUORUM].append({"labels": labels, "value": float(rq)})
    return m


def _conn(metrics):
    conn = MagicMock(name="conn")
    conn.metrics.return_value = metrics
    return conn


def test_healthy_set_reports_tolerance_and_no_findings():
    out = ops.healing_health(_conn(_set_metrics([(0, 0, 4, 0, 2, 2)])))
    assert out["healthy"] is True
    assert out["erasureSets"][0]["failureToleranceRemaining"] == 2
    assert out["findings"] == []


def test_set_at_write_quorum_edge_is_critical():
    out = ops.healing_health(_conn(_set_metrics([(0, 0, 2, 0, 2, 2)])))
    issues = {f["issue"]: f["severity"] for f in out["findings"]}
    assert issues.get("WRITE_QUORUM_AT_EDGE") == "critical"
    finding = out["findings"][0]
    assert finding["cause"] and finding["suggestedAction"]


def test_set_below_quorum_is_lost():
    out = ops.healing_health(_conn(_set_metrics([(0, 0, 1, 0, 2, 2)])))
    issues = {f["issue"] for f in out["findings"]}
    assert "WRITE_QUORUM_LOST" in issues


def test_one_drive_from_edge_is_warning_and_healing_is_info():
    out = ops.healing_health(_conn(_set_metrics([(0, 0, 3, 1, 2, 2)])))
    issues = {f["issue"]: f["severity"] for f in out["findings"]}
    assert issues.get("LOW_FAILURE_TOLERANCE") == "warning"
    assert issues.get("HEALING_IN_PROGRESS") == "info"


def test_heal_backlog_and_errors_reported():
    m = _set_metrics([(0, 0, 4, 0, 2, 2)])
    m[ops.M_HEAL_SCANNED] = [{"labels": {}, "value": 100.0}]
    m[ops.M_HEAL_HEALED] = [{"labels": {}, "value": 60.0}]
    m[ops.M_HEAL_ERRORS] = [{"labels": {}, "value": 3.0}]
    out = ops.healing_health(_conn(m))
    assert out["healBacklogObjects"] == 40
    assert any(f["issue"] == "HEAL_ERRORS" for f in out["findings"])


def test_healing_health_resilient():
    conn = MagicMock(name="conn")
    conn.metrics.side_effect = RuntimeError("scrape down")
    assert "error" in ops.healing_health(conn)


def test_drive_status_fullest_first_and_resilient():
    m = {
        ops.M_DRIVE_TOTAL: [
            {"labels": {"server": "m1", "drive": "/d1"}, "value": 100.0},
            {"labels": {"server": "m1", "drive": "/d2"}, "value": 100.0},
        ],
        ops.M_DRIVE_FREE: [
            {"labels": {"server": "m1", "drive": "/d1"}, "value": 80.0},
            {"labels": {"server": "m1", "drive": "/d2"}, "value": 10.0},
        ],
    }
    out = ops.drive_status(_conn(m))
    rows = out["drives"]
    assert rows[0]["drive"] == "/d2" and rows[0]["usedRatio"] == pytest.approx(0.9)
    assert out["returned"] == 2
    assert out["error"] is None

    # A broken scrape must be distinguishable from "this server has no drives":
    # returning a bare [] for both is how a metric-name mismatch stayed invisible.
    broken = MagicMock(name="conn")
    broken.metrics.side_effect = RuntimeError("down")
    failed = ops.drive_status(broken)
    assert failed["drives"] == []
    assert failed["returned"] == 0
    assert "down" in failed["error"]


def test_node_status_aggregates_per_server():
    m = {
        ops.M_DRIVE_ONLINE_NODE: [{"labels": {"server": "m1"}, "value": 4.0}],
        ops.M_DRIVE_OFFLINE_NODE: [{"labels": {"server": "m1"}, "value": 1.0}],
        "minio_cluster_nodes_online_total": [{"labels": {}, "value": 1.0}],
        "minio_cluster_nodes_offline_total": [{"labels": {}, "value": 0.0}],
    }
    out = ops.node_status(_conn(m))
    assert out["nodes"] == [{"server": "m1", "drivesOnline": 4.0, "drivesOffline": 1.0}]
