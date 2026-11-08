"""The test suite must not write governance state into the operator's home."""

import sqlite3

from minio_aiops.governance import governed_tool


@governed_tool(risk_level="medium")
def _isolated_test_write() -> dict:
    return {"status": "ok"}


def test_governed_write_uses_per_test_audit_home(tmp_path):
    assert _isolated_test_write()["status"] == "ok"
    audit_db = tmp_path / "audit.db"
    assert audit_db.is_file()
    with sqlite3.connect(audit_db) as conn:
        row = conn.execute(
            "SELECT tool, status FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("_isolated_test_write", "ok")
