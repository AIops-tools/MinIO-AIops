"""Truncation announces itself — and is measured, never guessed.

A bare list cannot say "there is more". The consumer has to infer it from the
length happening to equal the limit, which is exactly where a smaller local
model reports either "that's everything" or "no data was returned". Every
bounded listing therefore returns an envelope carrying ``returned``/``limit``/
``truncated``, and ``truncated`` is derived from a real over-fetch (or, where
the source hands back the complete set, from the full length before slicing).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from minio_aiops.cli import app
from minio_aiops.ops import buckets as bk
from minio_aiops.ops import capacity as cap
from minio_aiops.ops import exposure as exp
from minio_aiops.ops import ilm as ilm_ops

runner = CliRunner()

ENVELOPE_KEYS = {"returned", "limit", "truncated"}


def _objects(n: int) -> list[dict]:
    return [
        {
            "objectName": f"k{i}",
            "sizeBytes": i,
            "lastModified": "2026-01-01T00:00:00",
            "versionId": None,
        }
        for i in range(n)
    ]


# ── object listing (the longest feed there is) ─────────────────────────────


@pytest.mark.unit
def test_object_listing_measures_truncation_by_over_fetching():
    conn = MagicMock(name="conn")
    conn.list_objects_page.return_value = _objects(11)  # limit+1 came back
    result = bk.list_objects(conn, "data-bkt", limit=10)

    _, kwargs = conn.list_objects_page.call_args
    assert kwargs["limit"] == 11, "one extra row must be fetched to MEASURE truncation"
    assert result["returned"] == 10
    assert result["limit"] == 10
    assert result["truncated"] is True
    assert len(result["objects"]) == 10


@pytest.mark.unit
def test_object_listing_exactly_at_limit_is_not_truncated():
    """The length-equals-limit coincidence must NOT be read as truncation."""
    conn = MagicMock(name="conn")
    conn.list_objects_page.return_value = _objects(10)  # the extra row was absent
    result = bk.list_objects(conn, "data-bkt", limit=10)
    assert result["returned"] == 10
    assert result["truncated"] is False


@pytest.mark.unit
def test_empty_object_listing_is_an_explicit_zero_not_a_bare_empty_list():
    conn = MagicMock(name="conn")
    conn.list_objects_page.return_value = []
    result = bk.list_objects(conn, "data-bkt", limit=10)
    assert result == {"objects": [], "returned": 0, "limit": 10, "truncated": False}


# ── bucket listing ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_bucket_listing_truncation_is_measured_against_the_full_list():
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": f"b{i}"} for i in range(7)]
    result = bk.list_buckets(conn, limit=3)
    assert result["returned"] == 3
    assert result["limit"] == 3
    assert result["truncated"] is True
    assert [r["bucket"] for r in result["buckets"]] == ["b0", "b1", "b2"]


@pytest.mark.unit
def test_bucket_listing_within_limit_is_not_truncated():
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": "b0"}, {"name": "b1"}]
    assert bk.list_buckets(conn, limit=2)["truncated"] is False


# ── incomplete uploads ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_upload_listing_envelope_reports_truncation():
    conn = MagicMock(name="conn")
    conn.list_incomplete_uploads.return_value = [
        {"objectName": f"o{i}", "uploadId": f"u{i}", "initiated": None} for i in range(5)
    ]
    result = bk.list_incomplete_uploads(conn, "data-bkt", limit=2)
    assert ENVELOPE_KEYS <= set(result)
    assert result["returned"] == 2
    assert result["truncated"] is True


# ── usage by bucket ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_usage_by_bucket_envelope_reports_truncation():
    conn = MagicMock(name="conn")
    conn.metrics.return_value = {
        "minio_bucket_usage_total_bytes": [
            {"labels": {"bucket": f"b{i}"}, "value": float(i)} for i in range(5)
        ]
    }
    result = cap.usage_by_bucket(conn, limit=2)
    assert result["returned"] == 2
    assert result["truncated"] is True
    result_all = cap.usage_by_bucket(conn, limit=50)
    assert result_all["returned"] == 5
    assert result_all["truncated"] is False


# ── the analyses that cap how many buckets they look at ────────────────────


@pytest.mark.unit
def test_exposure_audit_says_when_it_did_not_look_at_every_bucket():
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": f"bucket-{i}"} for i in range(4)]
    conn.get_bucket_policy.return_value = None
    conn.get_bucket_encryption.return_value = None
    conn.get_bucket_versioning.return_value = "Enabled"
    conn.get_bucket_lifecycle.return_value = None
    result = exp.bucket_exposure_audit(conn, limit=2)
    assert result["bucketsAudited"] == 2
    assert result["bucketsTotal"] == 4
    assert result["truncated"] is True


@pytest.mark.unit
def test_ilm_gap_analysis_says_when_it_did_not_look_at_every_bucket():
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": f"bucket-{i}"} for i in range(4)]
    conn.metrics.return_value = {}
    conn.get_bucket_lifecycle.return_value = None
    conn.get_bucket_versioning.return_value = "Off"
    conn.list_incomplete_uploads.return_value = []
    result = ilm_ops.lifecycle_gap_analysis(conn, limit=2)
    assert result["bucketsAnalyzed"] == 2
    assert result["bucketsTotal"] == 4
    assert result["truncated"] is True


@pytest.mark.unit
def test_capacity_rca_reports_its_inline_drive_cap():
    conn = MagicMock(name="conn")
    conn.metrics.return_value = {
        "minio_node_drive_total_bytes": [
            {"labels": {"server": "s1", "drive": f"/d{i}"}, "value": 100.0}
            for i in range(cap.DRIVE_ROWS_SHOWN + 5)
        ],
        "minio_node_drive_used_bytes": [],
    }
    result = cap.capacity_rca(conn)
    assert result["drivesTotal"] == cap.DRIVE_ROWS_SHOWN + 5
    assert result["drivesReturned"] == cap.DRIVE_ROWS_SHOWN
    assert result["drivesTruncated"] is True


# ── the CLI tells a human, not just the JSON ───────────────────────────────


@pytest.mark.unit
def test_cli_bucket_ls_prints_a_truncation_note(monkeypatch):
    import minio_aiops.cli.bucket as bucket_cli

    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": f"b{i}"} for i in range(5)]
    monkeypatch.setattr(bucket_cli, "get_connection", lambda target=None: (conn, object()))

    result = runner.invoke(app, ["bucket", "ls", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert '"truncated": true' in result.output
    assert "Re-run with a higher --limit" in result.output


@pytest.mark.unit
def test_cli_bucket_objects_prints_a_truncation_note(monkeypatch):
    import minio_aiops.cli.bucket as bucket_cli

    conn = MagicMock(name="conn")
    conn.list_objects_page.return_value = _objects(3)
    monkeypatch.setattr(bucket_cli, "get_connection", lambda target=None: (conn, object()))

    result = runner.invoke(app, ["bucket", "objects", "data-bkt", "--limit", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[: result.output.rindex("}") + 1])
    assert payload["truncated"] is True
    assert "Re-run with a higher --limit" in result.output


@pytest.mark.unit
def test_cli_prints_no_note_when_the_result_is_complete(monkeypatch):
    import minio_aiops.cli.bucket as bucket_cli

    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": "only"}]
    monkeypatch.setattr(bucket_cli, "get_connection", lambda target=None: (conn, object()))

    result = runner.invoke(app, ["bucket", "ls"])
    assert result.exit_code == 0, result.output
    assert '"truncated": false' in result.output
    assert "Re-run with a higher" not in result.output
