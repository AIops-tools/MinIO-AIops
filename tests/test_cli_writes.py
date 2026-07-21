"""CLI confirmed-write path — past dry-run, through governance, onto disk.

The CLI write commands delegate real execution to the ``@governed_tool``
functions in ``mcp_server.tools``. These tests drive a write command PAST the
dry-run branch and the double-confirm prompts and assert the call really went
through the governed path (audit row on disk) — the regression test for the
"CLI writes were unaudited" line-wide fix.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import minio_aiops.governance.audit as audit_mod
import minio_aiops.governance.policy as policy_mod
import minio_aiops.governance.undo as undo_mod


@pytest.fixture
def gov_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MINIO_AIOPS_HOME", str(tmp_path))
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()
    yield tmp_path
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()


def _audit_tools(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT tool FROM audit_log ORDER BY id")]
    finally:
        conn.close()


def _now_iso() -> str:
    """An upload initiated just now — too fresh for a 7-day purge."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _mock_conn(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the governed module's connection with one empty, deletable bucket."""
    import mcp_server.tools.bucket_writes as gov

    conn = MagicMock(name="conn")
    conn.is_bucket_empty.return_value = True
    conn.get_bucket_versioning.return_value = "Off"
    conn.get_bucket_policy.return_value = None
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    return conn


@pytest.mark.unit
def test_cli_bucket_delete_dry_run_reads_and_audits_but_never_writes(gov_home, monkeypatch):
    """The invariant: a dry_run MAY read; it must never write.

    The old name asserted the abandoned rule ("makes no call and no audit"). It
    was never the rule the MCP path followed — ``@governed_tool`` wraps the
    function whether or not ``dry_run`` is set, so an MCP preview has always
    written its audit row; skipping it on the CLI made the audit trail depend on
    which door the operator came through. And the emptiness read is what lets
    the preview answer the question a delete preview exists for.
    """
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    result = CliRunner().invoke(app, ["bucket", "delete", "old-bkt", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    conn.is_bucket_empty.assert_called_once_with("old-bkt")
    conn.remove_bucket.assert_not_called(), "the one thing a dry-run may never do"
    assert _audit_tools(gov_home / "audit.db") == ["bucket_delete"]


@pytest.mark.unit
def test_cli_bucket_delete_dry_run_of_a_non_empty_bucket_is_refused(gov_home, monkeypatch):
    """A preview whose answer is "this would be refused" must refuse, not preview.

    The preview used to print a green banner carrying the note "refused unless
    empty" — an instruction to the reader to go find out, in the exact place the
    tool could simply have found out. A weak model reads the refusal that
    follows as transient and retries it.
    """
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    conn.is_bucket_empty.return_value = False
    result = CliRunner().invoke(app, ["bucket", "delete", "full-bkt", "--dry-run"])
    assert result.exit_code == 1, result.output
    assert "DRY-RUN" not in result.output
    assert "is not empty" in result.output
    conn.remove_bucket.assert_not_called()


@pytest.mark.unit
def test_cli_purge_uploads_dry_run_reports_the_real_candidate_count(gov_home, monkeypatch):
    """The preview counts what the purge would actually abort.

    An irreversible purge previewed as a restatement of the flag you just typed
    tells you nothing; "matched 1 of 2" is the number worth having, and it comes
    from the same selection the purge itself uses.
    """
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    conn.list_incomplete_uploads.return_value = [
        {"objectName": "old", "uploadId": "u1", "initiated": "2020-01-01T00:00:00Z"},
        {"objectName": "fresh", "uploadId": "u2", "initiated": _now_iso()},
    ]
    result = CliRunner().invoke(
        app, ["bucket", "purge-uploads", "data-bkt", "--older-than-days", "7", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "incompleteUploads = 2" in result.output
    assert "matchedForPurge = 1" in result.output
    conn.abort_incomplete_upload.assert_not_called()


@pytest.mark.unit
def test_cli_bucket_delete_confirmed_goes_through_governance(gov_home, monkeypatch):
    """Confirmed CLI write must execute via the governed twin: the API call runs
    AND an audit row lands in audit.db (this is what the reroute fix bought)."""
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    result = CliRunner().invoke(app, ["bucket", "delete", "old-bkt"], input="y\ny\n")
    assert result.exit_code == 0, result.output
    conn.remove_bucket.assert_called_once_with("old-bkt")
    assert _audit_tools(gov_home / "audit.db") == ["bucket_delete"]


@pytest.mark.unit
def test_cli_bucket_delete_aborts_without_double_confirm(gov_home, monkeypatch):
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    result = CliRunner().invoke(app, ["bucket", "delete", "old-bkt"], input="y\nn\n")
    assert result.exit_code != 0
    conn.remove_bucket.assert_not_called()
    assert not (gov_home / "audit.db").exists()


@pytest.mark.unit
def test_cli_versioning_set_goes_through_governance(gov_home, monkeypatch):
    """A medium (no-confirm) CLI write is still audited via the governed twin."""
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    conn.get_bucket_versioning.return_value = "Suspended"
    result = CliRunner().invoke(app, ["bucket", "versioning-set", "data-bkt", "Enabled"])
    assert result.exit_code == 0, result.output
    conn.set_bucket_versioning.assert_called_once_with("data-bkt", "Enabled")
    assert _audit_tools(gov_home / "audit.db") == ["set_versioning"]


@pytest.mark.unit
def test_cli_policy_set_dry_run_reports_a_self_denying_policy(gov_home, monkeypatch, tmp_path):
    """The CLI preview must refuse what the CLI write would refuse.

    policy-set is self-lockout guarded, so its preview routes through the
    governed twin to find out whether the real call would be refused. Printing a
    green DRY-RUN banner for a policy that is about to be rejected is the
    preview being wrong, not merely incomplete.
    """
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    conn.target.access_key = "aiops-svc"
    policy_file = tmp_path / "deny.json"
    policy_file.write_text(
        '{"Version":"2012-10-17","Statement":[{"Effect":"Deny",'
        '"Principal":{"AWS":"aiops-svc"},"Action":"s3:PutBucketPolicy",'
        '"Resource":"arn:aws:s3:::data-bkt/*"}]}'
    )
    result = CliRunner().invoke(
        app, ["bucket", "policy-set", "data-bkt", "--file", str(policy_file), "--dry-run"]
    )
    assert result.exit_code == 1, result.output
    assert "DRY-RUN" not in result.output
    assert "explicit Deny" in result.output
    conn.set_bucket_policy.assert_not_called()


@pytest.mark.unit
def test_cli_policy_set_dry_run_still_previews_an_ordinary_policy(gov_home, monkeypatch,
                                                                 tmp_path):
    """Exactness on the CLI path: a normal policy still gets its green banner."""
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    conn.target.access_key = "aiops-svc"
    policy_file = tmp_path / "allow.json"
    policy_file.write_text(
        '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*",'
        '"Action":"s3:GetObject","Resource":"arn:aws:s3:::data-bkt/*"}]}'
    )
    result = CliRunner().invoke(
        app, ["bucket", "policy-set", "data-bkt", "--file", str(policy_file), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    conn.set_bucket_policy.assert_not_called()  # a dry-run must never write
