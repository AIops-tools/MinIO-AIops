"""Absent fields come back as null, not as an empty string.

An empty string reads as "this field exists and is empty"; a missing field is a
different fact. Collapsing the two hides information from any consumer, and a
smaller local model will confidently invent the difference. These tests pin the
contract end-to-end: helper, ops layer, and the CLI rendering that has to cope
with a null.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from minio_aiops.cli import app
from minio_aiops.governance import opt_str
from minio_aiops.ops import buckets as bk

runner = CliRunner()


@pytest.mark.unit
def test_opt_str_distinguishes_absent_from_empty():
    assert opt_str(None) is None, "absent must stay absent"
    assert opt_str("") == "", "a genuinely empty value is not the same as absent"
    assert opt_str("data-bucket", 64) == "data-bucket"


@pytest.mark.unit
def test_opt_str_still_sanitizes_and_truncates():
    assert opt_str("a\x00b") == "ab"  # control character stripped
    assert opt_str("abcdef", 3) == "abc"


@pytest.mark.unit
def test_opt_str_accepts_non_string_values():
    assert opt_str(42) == "42"


@pytest.mark.unit
def test_ops_report_absent_bucket_fields_as_none():
    """A bucket row with no creation date reports null, not ''."""
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": "data"}]  # createdAt absent
    row = bk.list_buckets(conn)["buckets"][0]
    assert row["bucket"] == "data"
    assert row["createdAt"] is None


@pytest.mark.unit
def test_ops_keep_empty_string_when_source_is_empty():
    """An explicitly empty upstream value is preserved as '' — not turned into null."""
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": "data", "createdAt": ""}]
    assert bk.list_buckets(conn)["buckets"][0]["createdAt"] == ""


@pytest.mark.unit
def test_ops_never_drop_the_key_itself():
    """Keys are always present; only their value may be null.

    Omitting a key entirely is worse than a null — the consumer cannot tell the
    field was even considered.
    """
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{}]
    row = bk.list_buckets(conn)["buckets"][0]
    for key in ("bucket", "createdAt"):
        assert key in row, f"{key} must be present even when the source omitted it"


@pytest.mark.unit
def test_object_rows_report_absent_timestamps_as_none():
    """An object with no lastModified/versionId reports null, not ''."""
    conn = MagicMock(name="conn")
    conn.list_objects_page.return_value = [
        {"objectName": "a.txt", "sizeBytes": 10, "lastModified": None, "versionId": None}
    ]
    row = bk.list_objects(conn, "data-bkt")["objects"][0]
    assert row["objectName"] == "a.txt"
    assert row["lastModified"] is None
    assert row["versionId"] is None


@pytest.mark.unit
def test_upload_rows_report_absent_initiated_as_none():
    conn = MagicMock(name="conn")
    conn.list_incomplete_uploads.return_value = [
        {"objectName": "big.bin", "uploadId": "u1", "initiated": None}
    ]
    row = bk.list_incomplete_uploads(conn, "data-bkt")["uploads"][0]
    assert row["uploadId"] == "u1"
    assert row["initiated"] is None


@pytest.mark.unit
def test_server_info_reports_absent_mode_as_none():
    conn = MagicMock(name="conn")
    conn.server_info.return_value = {}  # mode / deploymentID absent
    info = bk.server_info(conn)
    assert info["mode"] is None
    assert info["deploymentId"] is None


@pytest.mark.unit
def test_cli_renders_rows_with_null_fields(monkeypatch):
    """The CLI must survive a null field rather than crashing on render."""
    import minio_aiops.cli.bucket as bucket_cli

    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": "data"}]  # createdAt absent
    monkeypatch.setattr(bucket_cli, "get_connection", lambda target=None: (conn, object()))

    result = runner.invoke(app, ["bucket", "ls"])
    assert result.exit_code == 0, result.output
    assert "data" in result.output
    assert "null" in result.output, "the absent createdAt must render as null"


@pytest.mark.unit
def test_undo_list_envelope_measures_truncation(monkeypatch):
    from mcp_server.tools import undo as undo_tools

    rows = [
        {
            "undo_id": f"u{i}",
            "ts": "2026-07-18T00:00:00Z",
            "tool": "some_tool",
            "undo_tool": "some_inverse_tool",
            "note": "",
        }
        for i in range(4)
    ]
    captured = {}

    class _Store:
        def list(self, *, status=None, limit=50):
            captured["limit"] = limit
            return rows[:limit]

    monkeypatch.setattr(undo_tools, "get_undo_store", lambda: _Store())
    result = undo_tools.undo_list(limit=3)
    assert captured["limit"] == 4, "one extra row is fetched to measure truncation"
    assert result["returned"] == 3
    assert result["limit"] == 3
    assert result["truncated"] is True
    assert len(result["undos"]) == 3
