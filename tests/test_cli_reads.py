"""CLI read commands: each command wires get_connection → ops → JSON output.

get_connection is patched per-module (it is imported into each CLI module's own
namespace) so no real ConnectionManager/network is touched; the mock connection
returns canned metrics so the ops layer runs end-to-end under the command."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from minio_aiops.cli import app
from minio_aiops.connection import MinioApiError

pytestmark = pytest.mark.unit

runner = CliRunner()


def _metrics():
    def m(v):
        return [{"labels": {}, "value": v}]

    return {
        "minio_cluster_capacity_usable_total_bytes": m(1000),
        "minio_cluster_capacity_usable_free_bytes": m(250),
        "minio_cluster_capacity_raw_total_bytes": m(2000),
        "minio_cluster_capacity_raw_free_bytes": m(1200),
        "minio_cluster_nodes_online_total": m(4),
        "minio_cluster_nodes_offline_total": m(0),
        "minio_cluster_drive_online_total": m(16),
        "minio_cluster_drive_offline_total": m(0),
        "minio_cluster_drive_total": m(16),
        "minio_cluster_bucket_total": m(3),
        "minio_cluster_usage_object_total": m(3400),
        "minio_cluster_usage_total_bytes": m(700),
    }


def _mock_conn():
    conn = MagicMock(name="conn")
    conn.metrics.return_value = _metrics()
    conn.list_buckets.return_value = [{"name": "data", "createdAt": "2026-01-01T00:00:00"}]
    conn.health_live.return_value = {"reachable": True, "healthy": True}
    conn.health_ready.return_value = {"reachable": True, "healthy": True}
    conn.health_cluster.return_value = {"reachable": True, "healthy": True}
    conn.get_bucket_policy.return_value = None
    conn.get_bucket_versioning.return_value = "Off"
    conn.get_bucket_lifecycle.return_value = None
    conn.get_bucket_encryption.return_value = None
    conn.get_bucket_quota.return_value = {"quotaBytes": 0}
    conn.get_bucket_tags.return_value = None
    conn.list_objects_page.return_value = []
    conn.list_incomplete_uploads.return_value = []
    return conn


@pytest.fixture
def patch_conn(monkeypatch):
    """Patch get_connection in every CLI read module to return a mock conn."""
    conn = _mock_conn()
    import minio_aiops.cli.bucket as bkt
    import minio_aiops.cli.capacity as cap
    import minio_aiops.cli.heal as heal
    import minio_aiops.cli.health as health
    import minio_aiops.cli.overview as over

    for mod in (bkt, cap, heal, health, over):
        monkeypatch.setattr(mod, "get_connection", lambda target=None, c=conn: (c, None))
    return conn


@pytest.mark.parametrize(
    "argv",
    [
        ["health", "check"],
        ["health", "status"],
        ["capacity", "rca"],
        ["capacity", "usage"],
        ["heal", "status"],
        ["heal", "drives"],
        ["heal", "nodes"],
        ["overview"],
        ["bucket", "ls"],
        ["bucket", "info", "data"],
        ["bucket", "audit"],
        ["bucket", "ilm-gap"],
        ["bucket", "uploads", "data"],
    ],
)
def test_read_command_exits_clean_and_emits_json(patch_conn, argv):
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    # every read command prints a JSON document (object or array)
    assert result.output.strip()[0] in "{["


def test_bucket_ls_output_shape(patch_conn):
    result = runner.invoke(app, ["bucket", "ls"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {
        "buckets": [{"bucket": "data", "createdAt": "2026-01-01T00:00:00"}],
        "returned": 1,
        "limit": 500,
        "truncated": False,
    }


def test_health_check_reports_healthy(patch_conn):
    result = runner.invoke(app, ["health", "check"])
    assert json.loads(result.output)["healthy"] is True


def test_cli_error_translated_to_one_line(monkeypatch):
    import minio_aiops.cli.health as health

    def boom(target=None):
        raise MinioApiError("cannot reach server", op="health")

    monkeypatch.setattr(health, "get_connection", boom)
    result = runner.invoke(app, ["health", "check"])
    assert result.exit_code == 1
    assert "Error: cannot reach server" in result.output


def test_cli_keyerror_gets_env_hint(monkeypatch):
    import minio_aiops.cli.bucket as bkt

    def boom(target=None):
        raise KeyError("MINIO_LAB1_SECRET_KEY")

    monkeypatch.setattr(bkt, "get_connection", boom)
    result = runner.invoke(app, ["bucket", "ls"])
    assert result.exit_code == 1
    assert "Missing required key or environment variable" in result.output


def test_bucket_info_uses_target_option(patch_conn, monkeypatch):
    """The -t/--target value is threaded into get_connection."""
    seen = {}
    import minio_aiops.cli.bucket as bkt

    conn = _mock_conn()

    def capture(target=None):
        seen["target"] = target
        return conn, None

    monkeypatch.setattr(bkt, "get_connection", capture)
    result = runner.invoke(app, ["bucket", "info", "data", "-t", "prod"])
    assert result.exit_code == 0
    assert seen["target"] == "prod"
