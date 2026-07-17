"""service_health / cluster_status / fleet_overview: metric folding, threshold
classification, and resilient per-probe degradation (a health probe must survive
the thing it probes)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import health as h
from minio_aiops.ops import overview as ov

pytestmark = pytest.mark.unit


def test_service_health_all_healthy():
    conn = MagicMock()
    conn.health_live.return_value = {"reachable": True, "healthy": True}
    conn.health_ready.return_value = {"reachable": True, "healthy": True}
    conn.health_cluster.return_value = {"reachable": True, "healthy": True}
    out = h.service_health(conn)
    assert out["healthy"] is True and "errors" not in out


def test_service_health_degrades_failing_probe():
    conn = MagicMock()
    conn.health_live.return_value = {"reachable": True, "healthy": True}
    conn.health_ready.side_effect = RuntimeError("timeout")
    conn.health_cluster.return_value = {"reachable": True, "healthy": True}
    out = h.service_health(conn)
    assert out["ready"] == {"reachable": False, "healthy": False}
    assert out["healthy"] is False
    assert any("ready:" in e for e in out["errors"])


def _metrics(**over):
    def m(v):
        return [{"labels": {}, "value": v}]

    base = {
        "minio_cluster_capacity_usable_total_bytes": m(1000),
        "minio_cluster_capacity_usable_free_bytes": m(250),
        "minio_cluster_capacity_raw_total_bytes": m(2000),
        "minio_cluster_nodes_online_total": m(4),
        "minio_cluster_nodes_offline_total": m(0),
        "minio_cluster_drive_online_total": m(16),
        "minio_cluster_drive_offline_total": m(0),
        "minio_cluster_drive_total": m(16),
        "minio_cluster_bucket_total": m(12),
        "minio_cluster_usage_object_total": m(3400),
        "minio_cluster_usage_total_bytes": m(700),
    }
    base.update(over)
    return base


def test_cluster_status_folds_capacity_and_ratio():
    conn = MagicMock()
    conn.metrics.return_value = _metrics()
    out = h.cluster_status(conn)
    assert out["usableUsedBytes"] == 750  # 1000 - 250
    assert out["usableUsedRatio"] == 0.75
    assert out["nodesOnline"] == 4 and out["drivesTotal"] == 16
    assert out["degraded"] is False


def test_cluster_status_flags_degraded_on_offline():
    conn = MagicMock()
    conn.metrics.return_value = _metrics(
        minio_cluster_drive_offline_total=[{"labels": {}, "value": 2}]
    )
    out = h.cluster_status(conn)
    assert out["drivesOffline"] == 2 and out["degraded"] is True


def test_cluster_status_error_path():
    conn = MagicMock()
    conn.metrics.side_effect = RuntimeError("metrics denied")
    out = h.cluster_status(conn)
    assert "error" in out and "metrics denied" in out["error"]


def test_cluster_status_used_none_when_capacity_missing():
    conn = MagicMock()
    m = _metrics()
    m["minio_cluster_capacity_usable_total_bytes"] = []
    conn.metrics.return_value = m
    out = h.cluster_status(conn)
    assert out["usableUsedBytes"] is None and out["usableUsedRatio"] is None


def test_fleet_overview_folds_all_three():
    conn = MagicMock()
    conn.health_live.return_value = {"reachable": True, "healthy": True}
    conn.health_ready.return_value = {"reachable": True, "healthy": True}
    conn.health_cluster.return_value = {"reachable": True, "healthy": True}
    conn.metrics.return_value = _metrics()
    conn.list_buckets.return_value = []
    out = ov.fleet_overview(conn)
    assert out["healthy"] is True and out["degraded"] is False
    assert out["usableUsedRatio"] == 0.75 and out["buckets"] == 12
    assert out["errors"] == []


def test_fleet_overview_collects_partial_errors():
    conn = MagicMock()
    conn.health_live.side_effect = RuntimeError("x")  # doesn't kill service_health
    conn.health_ready.return_value = {"healthy": True}
    conn.health_cluster.return_value = {"healthy": True}
    conn.metrics.side_effect = RuntimeError("no metrics")  # cluster_status errors
    conn.list_buckets.side_effect = RuntimeError("no audit")  # exposure errors
    out = ov.fleet_overview(conn)
    # cluster_status returns {"error":...} (truthy dict, not raised) so no overview
    # error entry for status; exposure raises → collected.
    assert out["healthy"] is False
    assert out["degraded"] is False
    assert isinstance(out["errors"], list)
