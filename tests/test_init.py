"""Tests for the ``minio-aiops init`` wizard.

Driven through typer's CliRunner against an ``isolated_home`` (see conftest);
the master password comes from ``MINIO_AIOPS_MASTER_PASSWORD`` and the hidden
secret-key prompt is fed by patching ``getpass``. The trailing doctor run is
either declined via stdin or patched out.
"""

from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

import minio_aiops.cli.init as init_mod
import minio_aiops.secretstore as ss
from minio_aiops.cli._root import app
from tests.conftest import MASTER_PW

pytestmark = pytest.mark.unit

runner = CliRunner()

SECRET_KEY = "minio-secret-123"  # noqa: S105 — test fixture value

# Prompt order: name, host, port(default), TLS confirm(default=True),
# verify confirm(default=True), region(default ""), access key,
# [getpass patched], metrics-public confirm(default=False),
# add-another(No), doctor(No).
WIZARD_INPUT = "lab1\nminio.example.com\n\n\n\n\nak\n\nn\nn\n"


@pytest.fixture
def hidden_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """getpass reads the TTY, not CliRunner stdin — patch it."""
    monkeypatch.setattr(init_mod.getpass, "getpass", lambda prompt="": SECRET_KEY)


def test_init_writes_config_and_encrypted_secret(isolated_home, hidden_secret):
    result = runner.invoke(app, ["init"], input=WIZARD_INPUT)
    assert result.exit_code == 0, result.output

    config_text = (isolated_home / "config.yaml").read_text("utf-8")
    raw = yaml.safe_load(config_text)
    assert raw["targets"] == [
        {
            "name": "lab1",
            "host": "minio.example.com",
            "port": 9000,
            "access_key": "ak",
            "secure": True,
            "verify_ssl": True,  # TLS verify default=True accepted as-is
            "region": "",
            "metrics_public": False,
        }
    ]

    # The secret lands encrypted in secrets.enc, never in config.yaml.
    secrets_blob = (isolated_home / "secrets.enc").read_text("utf-8")
    assert SECRET_KEY not in config_text
    assert SECRET_KEY not in secrets_blob
    assert ss.SecretStore.unlock(MASTER_PW).get("lab1") == SECRET_KEY


def test_init_seeds_default_policy_rules(isolated_home, hidden_secret):
    result = runner.invoke(app, ["init"], input=WIZARD_INPUT)
    assert result.exit_code == 0, result.output
    rules = (isolated_home / "rules.yaml").read_text("utf-8")
    assert "high-risk-requires-approver" in rules
    assert "tier: dual" in rules


def test_init_does_not_clobber_existing_rules(isolated_home, hidden_secret):
    sentinel = "# operator-authored rules — do not touch\nrisk_tiers: []\n"
    (isolated_home / "rules.yaml").write_text(sentinel, "utf-8")
    result = runner.invoke(app, ["init"], input=WIZARD_INPUT)
    assert result.exit_code == 0, result.output
    assert (isolated_home / "rules.yaml").read_text("utf-8") == sentinel


def test_init_no_tls_skips_verify_prompt(isolated_home, hidden_secret):
    # Answer No at the TLS confirm — the verify prompt is skipped entirely.
    result = runner.invoke(
        app, ["init"], input="lab1\nminio.example.com\n\nn\n\nak\n\nn\nn\n"
    )
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((isolated_home / "config.yaml").read_text("utf-8"))
    assert raw["targets"][0]["secure"] is False
    assert raw["targets"][0]["verify_ssl"] is True  # untouched when TLS is off


def test_init_declines_tls_verification(isolated_home, hidden_secret):
    # Keep TLS, answer No at the verify confirm (self-signed lab cert).
    result = runner.invoke(
        app, ["init"], input="lab1\nminio.example.com\n\n\nn\n\nak\n\nn\nn\n"
    )
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((isolated_home / "config.yaml").read_text("utf-8"))
    assert raw["targets"][0]["secure"] is True
    assert raw["targets"][0]["verify_ssl"] is False


def test_init_marks_metrics_public(isolated_home, hidden_secret):
    # Answer Yes at the metrics-public confirm.
    result = runner.invoke(
        app, ["init"], input="lab1\nminio.example.com\n\n\n\n\nak\ny\nn\nn\n"
    )
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((isolated_home / "config.yaml").read_text("utf-8"))
    assert raw["targets"][0]["metrics_public"] is True


def test_init_appends_to_existing_targets(isolated_home, hidden_secret):
    assert runner.invoke(app, ["init"], input=WIZARD_INPUT).exit_code == 0
    result = runner.invoke(
        app, ["init"], input="lab2\nminio2.example.com\n\n\n\n\nak2\n\nn\nn\n"
    )
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((isolated_home / "config.yaml").read_text("utf-8"))
    assert [t["name"] for t in raw["targets"]] == ["lab1", "lab2"]


def test_init_overwrites_target_on_confirm(isolated_home, hidden_secret):
    assert runner.invoke(app, ["init"], input=WIZARD_INPUT).exit_code == 0
    # Re-add 'lab1': confirm the overwrite, change the host.
    result = runner.invoke(
        app, ["init"], input="lab1\ny\nnew-minio.example.com\n\n\n\n\nak\n\nn\nn\n"
    )
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((isolated_home / "config.yaml").read_text("utf-8"))
    assert len(raw["targets"]) == 1
    assert raw["targets"][0]["host"] == "new-minio.example.com"


def test_init_runs_doctor_when_accepted(isolated_home, hidden_secret, monkeypatch):
    import minio_aiops.doctor as doc

    calls: list[bool] = []

    def fake_doctor(skip_auth: bool = False) -> int:
        calls.append(True)
        return 0

    monkeypatch.setattr(doc, "run_doctor", fake_doctor)
    # Accept the trailing doctor confirm (default=True) with a blank line.
    result = runner.invoke(
        app, ["init"], input="lab1\nminio.example.com\n\n\n\n\nak\n\nn\n\n"
    )
    assert result.exit_code == 0, result.output
    assert calls == [True]
