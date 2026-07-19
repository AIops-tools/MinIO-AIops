"""Capacity ops tests: RCA thresholds, drive hotspots, per-bucket usage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import capacity as ops

pytestmark = pytest.mark.unit


def _metrics(*, used_ratio=0.5, drives=None, offline=0.0, nodes_offline=0.0,
             bucket_usage=None):
    total = 1_000_000.0
    m = {
        "minio_cluster_capacity_usable_total_bytes": [{"labels": {}, "value": total}],
        "minio_cluster_capacity_usable_free_bytes": [
            {"labels": {}, "value": total * (1 - used_ratio)}
        ],
        "minio_cluster_drive_offline_total": [{"labels": {}, "value": offline}],
        "minio_cluster_nodes_offline_total": [{"labels": {}, "value": nodes_offline}],
    }
    if drives:
        m["minio_node_drive_used_bytes"] = [
            {"labels": {"server": srv, "drive": drv}, "value": used}
            for (srv, drv, used, _tot) in drives
        ]
        m["minio_node_drive_total_bytes"] = [
            {"labels": {"server": srv, "drive": drv}, "value": tot}
            for (srv, drv, _used, tot) in drives
        ]
    if bucket_usage:
        m["minio_bucket_usage_total_bytes"] = [
            {"labels": {"bucket": b}, "value": v} for b, v in bucket_usage.items()
        ]
        m["minio_bucket_usage_object_total"] = [
            {"labels": {"bucket": b}, "value": 10.0} for b in bucket_usage
        ]
    return m


def _conn(metrics):
    conn = MagicMock(name="conn")
    conn.metrics.return_value = metrics
    return conn


def test_healthy_cluster_has_no_findings():
    out = ops.capacity_rca(_conn(_metrics(used_ratio=0.4)))
    assert out["healthy"] is True
    assert out["findings"] == []
    assert out["usedRatio"] == pytest.approx(0.4, abs=0.001)


def test_nodes_offline_is_critical():
    out = ops.capacity_rca(_conn(_metrics(nodes_offline=1.0)))
    issues = {f["issue"]: f["severity"] for f in out["findings"]}
    assert issues.get("NODES_OFFLINE") == "critical"


def test_drive_hotspot_and_imbalance_detected():
    drives = [
        ("m1", "/d1", 95.0, 100.0),  # 95% — hotspot
        ("m1", "/d2", 30.0, 100.0),  # spread 0.65 — imbalance
    ]
    out = ops.capacity_rca(_conn(_metrics(drives=drives)))
    issues = {f["issue"] for f in out["findings"]}
    assert "DRIVE_HOTSPOT" in issues
    assert "DRIVE_IMBALANCE" in issues
    assert out["drives"][0]["drive"] == "m1:/d1"  # fullest first


def test_thresholds_are_named_constants():
    assert 0 < ops.NEARFULL_RATIO < ops.FULL_RATIO <= 1
    assert 0 < ops.DRIVE_NEARFULL_RATIO <= 1


def test_usage_by_bucket_sorted_and_limited():
    conn = _conn(_metrics(bucket_usage={"small": 10.0, "big": 999.0, "mid": 100.0}))
    result = ops.usage_by_bucket(conn, limit=2)
    assert [r["bucket"] for r in result["buckets"]] == ["big", "mid"]
    assert result["returned"] == 2
    assert result["limit"] == 2
    assert result["truncated"] is True
    assert result["buckets"][0]["objects"] == 10.0


def test_usage_by_bucket_resilient():
    conn = MagicMock(name="conn")
    conn.metrics.side_effect = RuntimeError("scrape down")
    assert ops.usage_by_bucket(conn) == {
        "buckets": [],
        "returned": 0,
        "limit": 25,
        "truncated": False,
    }
