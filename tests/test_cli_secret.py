"""``minio-aiops secret`` CLI: set/list/rm/migrate/rotate-password against a
real (tmp-dir) encrypted store. Master password comes from the env
(MINIO_AIOPS_MASTER_PASSWORD, set by the isolated_home fixture) so nothing
prompts; assertions confirm secrets round-trip encrypted and are never printed.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import minio_aiops.secretstore as ss
from minio_aiops.cli import app

pytestmark = pytest.mark.unit

runner = CliRunner()


def test_secret_set_then_list(isolated_home):
    r = runner.invoke(app, ["secret", "set", "lab1", "--value", "topsecret"])
    assert r.exit_code == 0, r.output
    assert "Stored encrypted" in r.output
    assert "topsecret" not in r.output  # value never echoed

    r2 = runner.invoke(app, ["secret", "list"])
    assert r2.exit_code == 0
    assert "lab1" in r2.output

    # actually encrypted on disk
    blob = (isolated_home / "secrets.enc").read_text()
    assert "topsecret" not in blob


def test_secret_list_empty(isolated_home):
    r = runner.invoke(app, ["secret", "list"])
    assert r.exit_code == 0
    assert "No secrets stored yet" in r.output


def test_secret_rm(isolated_home):
    runner.invoke(app, ["secret", "set", "lab1", "--value", "v1"])
    r = runner.invoke(app, ["secret", "rm", "lab1"])
    assert r.exit_code == 0 and "Deleted" in r.output
    assert "lab1" not in runner.invoke(app, ["secret", "list"]).output


def test_secret_stored_value_round_trips(isolated_home):
    runner.invoke(app, ["secret", "set", "lab1", "--value", "round-trip-val"])
    store = ss.SecretStore.unlock()
    assert store.get("lab1") == "round-trip-val"


def test_secret_migrate_imports_legacy_env(isolated_home, monkeypatch):
    # write a legacy plaintext .env the migrate command should ingest
    (isolated_home / ".env").write_text("MINIO_LAB1_SECRET_KEY=legacy-secret\n")
    r = runner.invoke(app, ["secret", "migrate"])
    assert r.exit_code == 0, r.output
    assert "Imported 1 secret" in r.output and "lab1" in r.output
    assert ss.SecretStore.unlock().get("lab1") == "legacy-secret"


def test_secret_migrate_nothing_to_do(isolated_home):
    r = runner.invoke(app, ["secret", "migrate"])
    assert r.exit_code == 0
    assert "Nothing to migrate" in r.output


def test_secret_rotate_password_mismatch_aborts(isolated_home, monkeypatch):
    runner.invoke(app, ["secret", "set", "lab1", "--value", "v1"])
    # getpass returns new then a non-matching confirm
    pwds = iter(["new-pw", "different"])
    monkeypatch.setattr("minio_aiops.cli.secret.getpass.getpass", lambda *a, **k: next(pwds))
    r = runner.invoke(app, ["secret", "rotate-password"])
    assert r.exit_code == 1
    assert "did not match" in r.output


def test_secret_rotate_password_success(isolated_home, monkeypatch):
    runner.invoke(app, ["secret", "set", "lab1", "--value", "v1"])
    pwds = iter(["new-master", "new-master"])
    monkeypatch.setattr("minio_aiops.cli.secret.getpass.getpass", lambda *a, **k: next(pwds))
    r = runner.invoke(app, ["secret", "rotate-password"])
    assert r.exit_code == 0, r.output
    assert "rotated" in r.output
    # store now opens under the new password and keeps the secret
    assert ss.SecretStore.unlock("new-master").get("lab1") == "v1"
