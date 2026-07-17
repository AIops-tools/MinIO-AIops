"""Bucket write tests: BEFORE-state capture, undo construction AND replayability,
dry-run gating, risk tiers, emptiness gate, and the bucket-name injection gate.

Undo replayability is tested for real: each recorded descriptor's params are
fed straight back into the target governed tool (mocked connection) — a
descriptor that doesn't match the tool's signature fails here, not in an
incident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import bucket_writes as ops

pytestmark = pytest.mark.unit

POLICY = '{"Version":"2012-10-17","Statement":[]}'
PRIOR_POLICY = '{"Version":"2012-10-17","Statement":[{"Sid":"old"}]}'
PRIOR_LC_XML = "<LifecycleConfiguration><Rule/></LifecycleConfiguration>"


# ─── ops layer: prior-state capture + input validation ──────────────────────


def test_set_bucket_policy_validates_json_and_captures_prior():
    conn = MagicMock(name="conn")
    conn.get_bucket_policy.return_value = PRIOR_POLICY
    out = ops.set_bucket_policy(conn, "data-bkt", POLICY)
    assert out["priorState"]["policyJson"] == PRIOR_POLICY
    with pytest.raises(ValueError, match="not valid JSON"):
        ops.set_bucket_policy(conn, "data-bkt", "{broken")
    with pytest.raises(ValueError, match="Statement"):
        ops.set_bucket_policy(conn, "data-bkt", '{"foo": 1}')


def test_set_versioning_validates_status_and_captures_prior():
    conn = MagicMock(name="conn")
    conn.get_bucket_versioning.return_value = "Off"
    out = ops.set_versioning(conn, "data-bkt", "enabled")  # case-insensitive input
    assert out["status"] == "Enabled"
    assert out["priorState"]["versioning"] == "Off"
    conn.set_bucket_versioning.assert_called_once_with("data-bkt", "Enabled")
    with pytest.raises(ValueError, match="status must be"):
        ops.set_versioning(conn, "data-bkt", "on")


def test_set_lifecycle_captures_prior_xml_and_validates_days():
    conn = MagicMock(name="conn")
    conn.get_bucket_lifecycle_xml.return_value = PRIOR_LC_XML
    out = ops.set_lifecycle(conn, "data-bkt", noncurrent_expire_days=30)
    assert out["priorState"]["lifecycleXml"] == PRIOR_LC_XML
    _, kwargs = conn.set_bucket_lifecycle.call_args
    assert kwargs["noncurrent_expire_days"] == 30
    with pytest.raises(ValueError, match="positive integer"):
        ops.set_lifecycle(conn, "data-bkt", expire_days=0)


def test_set_lifecycle_verbatim_xml_path():
    conn = MagicMock(name="conn")
    conn.get_bucket_lifecycle_xml.return_value = None
    out = ops.set_lifecycle(conn, "data-bkt", lifecycle_xml=PRIOR_LC_XML)
    conn.set_bucket_lifecycle_xml.assert_called_once_with("data-bkt", PRIOR_LC_XML)
    conn.set_bucket_lifecycle.assert_not_called()
    assert out["priorState"]["lifecycleXml"] is None


def test_set_bucket_quota_captures_prior_and_validates():
    conn = MagicMock(name="conn")
    conn.get_bucket_quota.return_value = {"quotaBytes": 5000}
    out = ops.set_bucket_quota(conn, "data-bkt", 10000)
    assert out["priorState"]["quotaBytes"] == 5000
    conn.set_bucket_quota.assert_called_once_with("data-bkt", 10000)
    with pytest.raises(ValueError, match=">= 0"):
        ops.set_bucket_quota(conn, "data-bkt", -1)


def test_delete_bucket_refuses_non_empty():
    conn = MagicMock(name="conn")
    conn.is_bucket_empty.return_value = False
    with pytest.raises(ValueError, match="not empty"):
        ops.delete_bucket(conn, "full-bkt")
    conn.remove_bucket.assert_not_called()


def test_delete_bucket_captures_prior_meta_when_empty():
    conn = MagicMock(name="conn")
    conn.is_bucket_empty.return_value = True
    conn.get_bucket_versioning.return_value = "Suspended"
    conn.get_bucket_policy.return_value = None
    out = ops.delete_bucket(conn, "old-bkt")
    assert out["priorState"]["versioning"] == "Suspended"
    assert out["priorState"]["policyPresent"] is False
    conn.remove_bucket.assert_called_once_with("old-bkt")


def _upload(name: str, upload_id: str, age_days: float) -> dict:
    initiated = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    return {"objectName": name, "uploadId": upload_id, "initiated": initiated}


def test_remove_incomplete_uploads_age_filter_and_prior_state():
    conn = MagicMock(name="conn")
    conn.list_incomplete_uploads.return_value = [
        _upload("old.iso", "u1", 30), _upload("fresh.bin", "u2", 1),
    ]
    out = ops.remove_incomplete_uploads(conn, "data-bkt", older_than_days=7)
    assert out["aborted"] == 1
    assert out["priorState"]["incompleteUploads"] == 2
    assert out["priorState"]["matchedForPurge"] == 1
    conn.abort_incomplete_upload.assert_called_once_with("data-bkt", "old.iso", "u1")


def test_remove_incomplete_uploads_reports_per_upload_failures():
    conn = MagicMock(name="conn")
    conn.list_incomplete_uploads.return_value = [
        _upload("a", "u1", 30), _upload("b", "u2", 30),
    ]
    conn.abort_incomplete_upload.side_effect = [RuntimeError("boom"), None]
    out = ops.remove_incomplete_uploads(conn, "data-bkt", older_than_days=0)
    assert out["aborted"] == 1
    assert len(out["failures"]) == 1


def test_every_write_validates_bucket_name_first():
    conn = MagicMock(name="conn")
    for call in (
        lambda: ops.set_bucket_policy(conn, "../admin", POLICY),
        lambda: ops.delete_bucket_policy(conn, "../admin"),
        lambda: ops.set_versioning(conn, "../admin", "Enabled"),
        lambda: ops.set_lifecycle(conn, "../admin", expire_days=1),
        lambda: ops.delete_lifecycle(conn, "../admin"),
        lambda: ops.set_bucket_quota(conn, "../admin", 1),
        lambda: ops.delete_bucket(conn, "../admin"),
        lambda: ops.remove_incomplete_uploads(conn, "../admin"),
    ):
        with pytest.raises(ValueError, match="Invalid bucket name"):
            call()
    assert conn.method_calls == []  # nothing reached the connection


# ─── MCP layer: undo descriptors are REPLAYABLE ─────────────────────────────


@pytest.fixture
def recorded(monkeypatch):
    import minio_aiops.governance.undo as undo_mod

    box: dict = {}

    class _Store:
        def record(self, *, skill, tool, undo_descriptor, orig_params):
            box["d"] = undo_descriptor
            return "undo-1"

    monkeypatch.setattr(undo_mod, "get_undo_store", lambda: _Store())
    return box


def _gov_conn(monkeypatch) -> MagicMock:
    from mcp_server.tools import bucket_writes as gov

    conn = MagicMock(name="conn")
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    return conn


def _replay(descriptor: dict, monkeypatch) -> MagicMock:
    """Call the descriptor's target tool with its params — signature must fit."""
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    conn.get_bucket_policy.return_value = None
    conn.get_bucket_versioning.return_value = "Enabled"
    conn.get_bucket_lifecycle_xml.return_value = None
    conn.get_bucket_quota.return_value = {"quotaBytes": 0}
    result = getattr(gov, descriptor["tool"])(**descriptor["params"])
    assert "error" not in result, f"undo replay failed: {result}"
    return conn


def test_set_bucket_policy_undo_restores_prior_and_replays(monkeypatch, recorded):
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    conn.get_bucket_policy.return_value = PRIOR_POLICY
    result = gov.set_bucket_policy(bucket_name="data-bkt", policy_json=POLICY)
    assert result["priorState"]["policyJson"] == PRIOR_POLICY
    d = recorded["d"]
    assert d["tool"] == "set_bucket_policy"
    assert d["params"]["policy_json"] == PRIOR_POLICY
    replay_conn = _replay(d, monkeypatch)
    replay_conn.set_bucket_policy.assert_called_once_with("data-bkt", PRIOR_POLICY)


def test_set_bucket_policy_undo_is_delete_when_no_prior(monkeypatch, recorded):
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    conn.get_bucket_policy.return_value = None
    gov.set_bucket_policy(bucket_name="data-bkt", policy_json=POLICY)
    d = recorded["d"]
    assert d["tool"] == "delete_bucket_policy"
    replay_conn = _replay(d, monkeypatch)
    replay_conn.delete_bucket_policy.assert_called_once_with("data-bkt")


def test_set_lifecycle_undo_restores_prior_xml_and_replays(monkeypatch, recorded):
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    conn.get_bucket_lifecycle_xml.return_value = PRIOR_LC_XML
    gov.set_lifecycle(bucket_name="data-bkt", expire_days=30)
    d = recorded["d"]
    assert d["tool"] == "set_lifecycle"
    assert d["params"]["lifecycle_xml"] == PRIOR_LC_XML
    replay_conn = _replay(d, monkeypatch)
    replay_conn.set_bucket_lifecycle_xml.assert_called_once_with("data-bkt", PRIOR_LC_XML)


def test_delete_lifecycle_undo_reapplies_prior_and_replays(monkeypatch, recorded):
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    conn.get_bucket_lifecycle_xml.return_value = PRIOR_LC_XML
    gov.delete_lifecycle(bucket_name="data-bkt")
    d = recorded["d"]
    assert d["tool"] == "set_lifecycle"
    _replay(d, monkeypatch)


def test_versioning_undo_off_prior_becomes_suspended(monkeypatch, recorded):
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    conn.get_bucket_versioning.return_value = "Off"
    gov.set_versioning(bucket_name="data-bkt", status="Enabled")
    d = recorded["d"]
    assert d["params"]["status"] == "Suspended"  # S3 can't return to Off — honest note
    assert "cannot return" in d["note"]
    replay_conn = _replay(d, monkeypatch)
    replay_conn.set_bucket_versioning.assert_called_once_with("data-bkt", "Suspended")


def test_quota_undo_restores_prior_and_replays(monkeypatch, recorded):
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    conn.get_bucket_quota.return_value = {"quotaBytes": 5000}
    gov.set_bucket_quota(bucket_name="data-bkt", size_bytes=0)
    d = recorded["d"]
    assert d["tool"] == "set_bucket_quota"
    assert d["params"]["size_bytes"] == 5000
    replay_conn = _replay(d, monkeypatch)
    replay_conn.set_bucket_quota.assert_called_once_with("data-bkt", 5000)


def test_all_write_dry_runs_make_no_client_calls(monkeypatch):
    from mcp_server.tools import bucket_writes as gov

    conn = _gov_conn(monkeypatch)
    for call in (
        lambda: gov.set_bucket_policy(bucket_name="b-1", policy_json=POLICY, dry_run=True),
        lambda: gov.delete_bucket_policy(bucket_name="b-1", dry_run=True),
        lambda: gov.set_versioning(bucket_name="b-1", status="Enabled", dry_run=True),
        lambda: gov.set_lifecycle(bucket_name="b-1", expire_days=1, dry_run=True),
        lambda: gov.delete_lifecycle(bucket_name="b-1", dry_run=True),
        lambda: gov.set_bucket_quota(bucket_name="b-1", size_bytes=1, dry_run=True),
        lambda: gov.bucket_delete(bucket_name="b-1", dry_run=True),
        lambda: gov.remove_incomplete_uploads(bucket_name="b-1", dry_run=True),
    ):
        result = call()
        assert result.get("dryRun") is True, result
        assert "_undo_id" not in result
    assert conn.method_calls == []
