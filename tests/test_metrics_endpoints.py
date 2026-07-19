"""Metrics must be scraped from every endpoint that carries what we consume.

Regression from live verification against a real 4-drive erasure set: MinIO
splits its exposition across /cluster, /node and /bucket, and 12 of the 30
metric names this package consumes are absent from /cluster. Scraping only
/cluster made every per-drive and per-bucket reader silently return nothing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minio_aiops.connection import (
    _METRICS_BUCKET,
    _METRICS_CLUSTER,
    _METRICS_NODE,
    MinioConnection,
)

_TEXT = {
    _METRICS_CLUSTER: 'minio_cluster_drive_online_total 4\n',
    _METRICS_NODE: 'minio_node_drive_total_bytes{server="s1",drive="/d1"} 100\n',
    _METRICS_BUCKET: 'minio_bucket_usage_total_bytes{bucket="b1"} 42\n',
}


def _conn() -> MinioConnection:
    conn = MinioConnection.__new__(MinioConnection)
    conn._scrape = lambda ep: _TEXT[ep]  # type: ignore[method-assign]
    return conn


@pytest.mark.unit
def test_metrics_merges_cluster_node_and_bucket():
    m = _conn().metrics()
    assert "minio_cluster_drive_online_total" in m, "cluster metrics missing"
    assert "minio_node_drive_total_bytes" in m, "per-drive metrics missing — /node not scraped"
    assert "minio_bucket_usage_total_bytes" in m, "per-bucket metrics missing — /bucket not scraped"


@pytest.mark.unit
def test_bucket_endpoint_is_best_effort():
    """A deployment may disable /bucket; that must not fail the whole call."""
    from minio_aiops.connection import MinioApiError

    conn = MinioConnection.__new__(MinioConnection)

    def scrape(ep):
        if ep == _METRICS_BUCKET:
            raise MinioApiError("disabled", status_code=404, op=ep)
        return _TEXT[ep]

    conn._scrape = scrape  # type: ignore[method-assign]
    m = conn.metrics()
    assert "minio_node_drive_total_bytes" in m
    assert "minio_bucket_usage_total_bytes" not in m


@pytest.mark.unit
def test_drive_status_reports_scrape_failure_not_empty_list():
    """An empty drive list must not be indistinguishable from a broken scrape."""
    from minio_aiops.ops import healing

    broken = MagicMock(name="conn")
    broken.metrics.side_effect = RuntimeError("scrape exploded")
    out = healing.drive_status(broken)
    assert out["drives"] == []
    assert out["returned"] == 0
    assert "scrape exploded" in out["error"]
