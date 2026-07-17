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
def test_cli_bucket_delete_dry_run_makes_no_call_and_no_audit(gov_home, monkeypatch):
    from minio_aiops.cli import app

    conn = _mock_conn(monkeypatch)
    result = CliRunner().invoke(app, ["bucket", "delete", "old-bkt", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    conn.is_bucket_empty.assert_not_called()
    conn.remove_bucket.assert_not_called()
    assert not (gov_home / "audit.db").exists()


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
