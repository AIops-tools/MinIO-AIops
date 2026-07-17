"""Bucket read-ops tests: inventory, info folding, name validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import buckets as ops
from minio_aiops.ops._util import check_bucket_name

pytestmark = pytest.mark.unit


def test_check_bucket_name_accepts_valid_and_rejects_hostile():
    assert check_bucket_name("my-bucket.v2") == "my-bucket.v2"
    for bad in ("../admin", "a/b", "UPPER", "ab", "a" * 64, "-lead", "trail-",
                "double..dot", "10.0.0.1", "", None, "with space", "ctl\x00chr"):
        with pytest.raises(ValueError, match="Invalid bucket name"):
            check_bucket_name(bad)


def test_list_buckets_normalizes():
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": "alpha", "createdAt": "2026-01-01T00:00:00"}]
    assert ops.list_buckets(conn) == [
        {"bucket": "alpha", "createdAt": "2026-01-01T00:00:00"}
    ]


def test_bucket_info_folds_all_probes():
    conn = MagicMock(name="conn")
    conn.get_bucket_policy.return_value = None
    conn.get_bucket_versioning.return_value = "Enabled"
    conn.get_bucket_lifecycle.return_value = [{"ruleId": "r1", "status": "Enabled"}]
    conn.get_bucket_encryption.return_value = {"sseAlgorithm": "AES256"}
    conn.get_bucket_quota.return_value = {"quotaBytes": 0}
    conn.get_bucket_tags.return_value = {"team": "data"}
    out = ops.bucket_info(conn, "data-bkt")
    assert out["policy"] == {"present": False, "publicRead": False, "publicWrite": False}
    assert out["versioning"] == "Enabled"
    assert out["encryption"] == {"sseAlgorithm": "AES256"}
    assert out["quota"] == {"quotaBytes": 0}
    assert "errors" not in out


def test_bucket_info_degrades_per_probe():
    conn = MagicMock(name="conn")
    conn.get_bucket_policy.return_value = None
    conn.get_bucket_versioning.return_value = "Off"
    conn.get_bucket_lifecycle.return_value = None
    conn.get_bucket_encryption.return_value = None
    conn.get_bucket_quota.side_effect = RuntimeError("admin API denied")
    conn.get_bucket_tags.return_value = None
    out = ops.bucket_info(conn, "data-bkt")
    assert out["versioning"] == "Off"
    assert any("quota" in e for e in out["errors"])


def test_reads_validate_bucket_name_before_any_call():
    conn = MagicMock(name="conn")
    with pytest.raises(ValueError):
        ops.bucket_info(conn, "../admin")
    with pytest.raises(ValueError):
        ops.list_objects(conn, "../admin")
    with pytest.raises(ValueError):
        ops.list_incomplete_uploads(conn, "../admin")
    conn.get_bucket_policy.assert_not_called()
    conn.list_objects_page.assert_not_called()
    conn.list_incomplete_uploads.assert_not_called()


def test_list_objects_clamps_limit():
    conn = MagicMock(name="conn")
    conn.list_objects_page.return_value = []
    ops.list_objects(conn, "data-bkt", limit=99999)
    _, kwargs = conn.list_objects_page.call_args
    assert kwargs["limit"] == 1000


def test_server_info_summary():
    conn = MagicMock(name="conn")
    conn.server_info.return_value = {
        "mode": "online", "deploymentID": "dep-1",
        "servers": [{"endpoint": "m1:9000"}], "pools": {"0": {}},
    }
    out = ops.server_info(conn)
    assert out["mode"] == "online" and out["servers"] == 1 and out["pools"] == 1
