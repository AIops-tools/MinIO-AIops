"""Lifecycle/ILM gap analysis tests: version growth, incomplete uploads, estimates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import ilm as ops

pytestmark = pytest.mark.unit


def _old_iso(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _metrics(bucket: str, *, usage=1000.0, objects=10.0, versions=10.0):
    return {
        ops.M_BUCKET_USAGE: [{"labels": {"bucket": bucket}, "value": usage}],
        ops.M_BUCKET_OBJECTS: [{"labels": {"bucket": bucket}, "value": objects}],
        ops.M_BUCKET_VERSIONS: [{"labels": {"bucket": bucket}, "value": versions}],
    }


def _conn(*, bucket="data-bkt", versioning="Enabled", rules=None, uploads=None,
          metrics=None):
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": bucket}]
    conn.get_bucket_versioning.return_value = versioning
    conn.get_bucket_lifecycle.return_value = rules
    conn.list_incomplete_uploads.return_value = uploads or []
    conn.metrics.return_value = metrics if metrics is not None else {}
    return conn


def test_versioned_bucket_without_noncurrent_expiry_estimates_reclaim():
    conn = _conn(metrics=_metrics("data-bkt", usage=1000.0, objects=10.0, versions=40.0))
    out = ops.lifecycle_gap_analysis(conn)
    assert out["bucketsWithGaps"] == 1
    gap = out["findings"][0]["gaps"][0]
    assert gap["gap"] == "NONCURRENT_VERSIONS_UNBOUNDED"
    # 30 of 40 versions are noncurrent → 750 of 1000 bytes reclaimable.
    assert gap["reclaimableEstimateBytes"] == 750
    assert out["totalReclaimableEstimateBytes"] == 750
    assert gap["cause"] and gap["suggestedAction"]


def test_noncurrent_rule_present_silences_version_gap():
    rules = [{"ruleId": "r1", "status": "Enabled", "noncurrentExpirationDays": 30}]
    conn = _conn(rules=rules, metrics=_metrics("data-bkt", versions=40.0))
    out = ops.lifecycle_gap_analysis(conn)
    gaps = [g["gap"] for f in out["findings"] for g in f["gaps"]]
    assert "NONCURRENT_VERSIONS_UNBOUNDED" not in gaps


def test_incomplete_uploads_without_abort_rule_flagged_with_ages():
    uploads = [
        {"objectName": "big.iso", "uploadId": "u1", "initiated": _old_iso(30)},
        {"objectName": "new.bin", "uploadId": "u2", "initiated": _old_iso(1)},
    ]
    conn = _conn(versioning="Off", uploads=uploads)
    out = ops.lifecycle_gap_analysis(conn)
    gap = next(g for f in out["findings"] for g in f["gaps"]
               if g["gap"] == "INCOMPLETE_UPLOADS_NO_ABORT_RULE")
    assert gap["incompleteUploads"] == 2
    assert gap["abandonedUploads"] == 1  # only the 30-day-old one


def test_abort_rule_present_skips_upload_listing():
    rules = [{"ruleId": "r1", "status": "Enabled", "abortIncompleteDays": 7}]
    conn = _conn(versioning="Off", rules=rules)
    ops.lifecycle_gap_analysis(conn)
    conn.list_incomplete_uploads.assert_not_called()


def test_large_bucket_with_no_lifecycle_is_informational():
    conn = _conn(versioning="Off",
                 metrics=_metrics("data-bkt", usage=float(ops.LARGE_BUCKET_BYTES + 1)))
    out = ops.lifecycle_gap_analysis(conn)
    gaps = [g["gap"] for f in out["findings"] for g in f["gaps"]]
    assert "NO_LIFECYCLE_ON_LARGE_BUCKET" in gaps


def test_metrics_failure_degrades_to_structural_analysis():
    conn = _conn(metrics=None)
    conn.metrics.side_effect = RuntimeError("scrape down")
    out = ops.lifecycle_gap_analysis(conn)
    assert "findings" in out  # no raise; sizes simply unknown


def test_list_failure_is_error_not_raise():
    conn = MagicMock(name="conn")
    conn.list_buckets.side_effect = RuntimeError("down")
    assert "error" in ops.lifecycle_gap_analysis(conn)
