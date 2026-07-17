"""Tests for ``minio_aiops.doctor.run_doctor``.

Everything runs against an ``isolated_home`` (see conftest) — no real
``~/.minio-aiops`` and no network: the connectivity check is exercised by
patching ``ConnectionManager`` at the connection-module boundary.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
import yaml
from rich.console import Console

import minio_aiops.doctor as doc
import minio_aiops.secretstore as ss
from tests.conftest import MASTER_PW

pytestmark = pytest.mark.unit


@pytest.fixture
def doctor_out(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture doctor output on a wide console (no line-wrapping surprises)."""
    buf = io.StringIO()
    monkeypatch.setattr(doc, "_console", Console(file=buf, width=200))
    return buf


def _write_config(home, targets: list[dict]) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump({"targets": targets}), "utf-8")


def _seed_secret(name: str, value: str) -> None:
    ss.SecretStore.unlock(MASTER_PW).set(name, value)


LAB1 = {"name": "lab1", "host": "minio.example.com", "port": 9000, "access_key": "ak"}


# ─── broken-environment paths ───────────────────────────────────────────────


def test_missing_config_file(isolated_home, doctor_out):
    assert doc.run_doctor() == 1
    out = doctor_out.getvalue()
    assert "✗ Config file missing" in out
    assert "minio-aiops init" in out


def test_config_load_failure(isolated_home, doctor_out):
    (isolated_home / "config.yaml").write_text("targets: [ {name: broken", "utf-8")
    assert doc.run_doctor() == 1
    assert "✗ Config load failed" in doctor_out.getvalue()


def test_no_targets_configured(isolated_home, doctor_out):
    _write_config(isolated_home, [])
    assert doc.run_doctor() == 1
    assert "✗ No targets configured" in doctor_out.getvalue()


def test_no_secret_store_and_no_secret_key(isolated_home, doctor_out):
    _write_config(isolated_home, [LAB1])
    assert doc.run_doctor(skip_auth=True) == 1
    out = doctor_out.getvalue()
    assert "! No secret store yet" in out
    assert "✗ No secret key for target 'lab1'" in out


def test_legacy_env_file_warns_but_works(isolated_home, doctor_out, monkeypatch):
    _write_config(isolated_home, [LAB1])
    (isolated_home / ".env").write_text("MINIO_LAB1_SECRET_KEY=legacy\n", "utf-8")
    monkeypatch.setenv("MINIO_LAB1_SECRET_KEY", "legacy")
    assert doc.run_doctor(skip_auth=True) == 0
    out = doctor_out.getvalue()
    assert "legacy plaintext .env" in out
    assert "secret migrate" in out
    assert "✓ Secret key present for 'lab1'" in out


def test_world_readable_secrets_warns(isolated_home, doctor_out):
    _write_config(isolated_home, [LAB1])
    _seed_secret("lab1", "s3cret")
    (isolated_home / "secrets.enc").chmod(0o644)
    assert doc.run_doctor(skip_auth=True) == 0  # warning, not a failure
    assert "should be 600" in doctor_out.getvalue()


# ─── healthy paths ───────────────────────────────────────────────────────────


def test_healthy_skip_auth(isolated_home, doctor_out):
    _write_config(isolated_home, [LAB1])
    _seed_secret("lab1", "s3cret")
    assert doc.run_doctor(skip_auth=True) == 0
    out = doctor_out.getvalue()
    assert "✓ Config file present" in out
    assert "✓ 1 target(s) configured" in out
    assert "✓ Encrypted secret store present" in out
    assert "✓ Secret key present for 'lab1'" in out
    assert "Skipping connectivity check" in out


class _FakeConn:
    """Canned per-probe results: each value is a return or an Exception."""

    def __init__(self, live: Any = None, ready: Any = None,
                 buckets: Any = None, metrics: Any = None) -> None:
        self._live = live if live is not None else {"healthy": True, "statusCode": 200}
        self._ready = ready if ready is not None else {"healthy": True, "statusCode": 200}
        self._buckets = buckets if buckets is not None else [{"name": "b1"}]
        self._metrics = metrics if metrics is not None else "# metrics\n"

    @staticmethod
    def _ret(value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    def health_live(self) -> dict:
        return self._ret(self._live)

    def health_ready(self) -> dict:
        return self._ret(self._ready)

    def list_buckets(self) -> list:
        return self._ret(self._buckets)

    def metrics_text(self) -> str:
        return self._ret(self._metrics)


class _FakeMgr:
    """Stands in for ConnectionManager; per-target canned results."""

    results: dict[str, Any] = {}

    def __init__(self, config: Any) -> None:
        self._config = config

    def connect(self, name: str) -> _FakeConn:
        result = self.results[name]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_mgr(monkeypatch: pytest.MonkeyPatch) -> type[_FakeMgr]:
    import minio_aiops.connection as conn_mod

    _FakeMgr.results = {}
    monkeypatch.setattr(conn_mod, "ConnectionManager", _FakeMgr)
    return _FakeMgr


def test_healthy_end_to_end(isolated_home, doctor_out, fake_mgr):
    _write_config(isolated_home, [LAB1])
    _seed_secret("lab1", "s3cret")
    fake_mgr.results["lab1"] = _FakeConn()
    assert doc.run_doctor() == 0
    out = doctor_out.getvalue()
    assert "server live + ready" in out
    assert "S3 API authenticated, 1 bucket(s)" in out
    assert "metrics endpoint reachable" in out


def test_connect_failure_is_status_not_crash(isolated_home, doctor_out, fake_mgr):
    _write_config(isolated_home, [LAB1])
    _seed_secret("lab1", "s3cret")
    fake_mgr.results["lab1"] = ConnectionError("connection refused")
    assert doc.run_doctor() == 1
    assert "✗ Connect to 'lab1'" in doctor_out.getvalue()


def test_auth_failure_reported_as_problem(isolated_home, doctor_out, fake_mgr):
    _write_config(isolated_home, [LAB1])
    _seed_secret("lab1", "s3cret")
    fake_mgr.results["lab1"] = _FakeConn(buckets=PermissionError("403 bad key"))
    assert doc.run_doctor() == 1
    assert "✗ 'lab1' (minio.example.com:9000): S3 auth/list failed" in doctor_out.getvalue()


def test_metrics_failure_gives_auth_type_hint(isolated_home, doctor_out, fake_mgr):
    _write_config(isolated_home, [LAB1])
    _seed_secret("lab1", "s3cret")
    fake_mgr.results["lab1"] = _FakeConn(metrics=RuntimeError("403 forbidden"))
    assert doc.run_doctor() == 1
    out = doctor_out.getvalue()
    assert "metrics endpoint failed" in out
    assert "MINIO_PROMETHEUS_AUTH_TYPE=public" in out


def test_mixed_fleet_one_bad_target_fails_overall(isolated_home, doctor_out, fake_mgr):
    lab2 = {**LAB1, "name": "lab2", "host": "minio2.example.com"}
    _write_config(isolated_home, [LAB1, lab2])
    _seed_secret("lab1", "s1")
    _seed_secret("lab2", "s2")
    fake_mgr.results["lab1"] = _FakeConn()
    fake_mgr.results["lab2"] = _FakeConn(live=ConnectionError("refused"))
    assert doc.run_doctor() == 1
    out = doctor_out.getvalue()
    assert "S3 API authenticated" in out
    assert "health endpoints unreachable" in out
