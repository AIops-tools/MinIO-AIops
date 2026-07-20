"""Smoke + ops tests for minio-aiops.

Proves: every module imports, the CLI builds and --help works, the MCP server
exposes the expected tools and EVERY tool carries the harness marker
``_is_governed_tool``, the connection layer translates errors and derives the
metrics bearer token, the flagship capacity RCA maps metrics to cause/action,
and the bucket writes capture BEFORE-state, record undo, gate dry-run, and
carry correct risk tiers. No real MinIO server is needed — the connection is a
fake/MagicMock.
"""

import asyncio
import importlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

# Kept in sync with mcp_server/server.py (the full registered tool surface).
EXPECTED_TOOLS = {
    # health
    "health_live", "health_ready", "health_cluster", "cluster_status", "fleet_overview",
    # capacity
    "capacity_rca", "usage_by_bucket",
    # healing
    "healing_health", "drive_status", "node_status",
    # exposure / ilm
    "bucket_exposure_audit", "lifecycle_gap_analysis",
    # buckets (reads)
    "bucket_ls", "bucket_info", "bucket_policy_get", "bucket_lifecycle_get",
    "bucket_versioning_get", "bucket_quota_get", "object_ls", "incomplete_uploads_ls",
    "server_info",
    # writes
    "set_bucket_policy", "delete_bucket_policy", "set_versioning", "set_lifecycle",
    "delete_lifecycle", "set_bucket_quota", "bucket_delete", "remove_incomplete_uploads",
}


@pytest.mark.unit
def test_all_modules_import():
    for name in (
        "minio_aiops", "minio_aiops.config", "minio_aiops.connection",
        "minio_aiops.doctor", "minio_aiops.secretstore", "minio_aiops.prom",
        "minio_aiops.ops.health", "minio_aiops.ops.capacity", "minio_aiops.ops.healing",
        "minio_aiops.ops.exposure", "minio_aiops.ops.ilm", "minio_aiops.ops.buckets",
        "minio_aiops.ops.bucket_writes", "minio_aiops.ops.overview",
        "minio_aiops.cli", "minio_aiops.cli._root", "minio_aiops.cli._common",
        "minio_aiops.cli.init", "minio_aiops.cli.secret", "minio_aiops.cli.health",
        "minio_aiops.cli.bucket", "minio_aiops.cli.capacity", "minio_aiops.cli.heal",
        "minio_aiops.cli.overview", "minio_aiops.cli.doctor",
        "mcp_server.server", "mcp_server._shared",
        "mcp_server.tools.health", "mcp_server.tools.bucket_writes",
    ):
        importlib.import_module(name)


@pytest.mark.unit
def test_version_matches_pyproject():
    """__version__ is single-sourced from package metadata; it must track
    pyproject.toml so a release bump can never ship a stale self-report."""
    import tomllib
    from pathlib import Path

    import minio_aiops

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]
    assert minio_aiops.__version__ == expected


@pytest.mark.unit
def test_cli_app_builds_and_help_works():
    from minio_aiops.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("health", "bucket", "capacity", "heal", "secret", "init",
                "overview", "doctor", "mcp"):
        assert sub in result.output


@pytest.mark.unit
def test_cli_leaf_help_triggers_lazy_imports():
    from minio_aiops.cli import app

    runner = CliRunner()
    for cmd in (
        ["health", "--help"], ["bucket", "--help"], ["capacity", "--help"],
        ["heal", "--help"], ["secret", "--help"],
        ["doctor", "--help"], ["overview", "--help"], ["init", "--help"],
        ["health", "check", "--help"], ["health", "status", "--help"],
        ["capacity", "rca", "--help"], ["capacity", "usage", "--help"],
        ["heal", "status", "--help"], ["heal", "drives", "--help"],
        ["heal", "nodes", "--help"],
        ["bucket", "ls", "--help"], ["bucket", "info", "--help"],
        ["bucket", "audit", "--help"], ["bucket", "ilm-gap", "--help"],
        ["bucket", "uploads", "--help"], ["bucket", "versioning-set", "--help"],
        ["bucket", "policy-set", "--help"], ["bucket", "lifecycle-set", "--help"],
        ["bucket", "quota-set", "--help"], ["bucket", "purge-uploads", "--help"],
        ["bucket", "delete", "--help"],
        ["secret", "list", "--help"], ["secret", "set", "--help"],
    ):
        result = runner.invoke(app, cmd)
        assert result.exit_code == 0, f"{cmd} failed: {result.output}"


@pytest.mark.unit
def test_mcp_list_tools_exposes_expected_tools():
    from mcp_server.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"


@pytest.mark.unit
def test_every_mcp_tool_is_governed_by_harness():
    from mcp_server import _shared

    tool_objs = _shared.mcp._tool_manager._tools
    assert EXPECTED_TOOLS <= set(tool_objs), "tool registry incomplete"
    for name, tool in tool_objs.items():
        fn = getattr(tool, "fn", None)
        assert fn is not None, f"{name} has no fn"
        assert getattr(fn, "_is_governed_tool", False), f"{name} missing @governed_tool"


@pytest.mark.unit
def test_write_tools_have_correct_risk_tiers():
    from mcp_server.tools import bucket_writes as w

    assert w.bucket_delete._risk_level == "high"
    for medium in (w.set_bucket_policy, w.delete_bucket_policy, w.set_versioning,
                   w.set_lifecycle, w.delete_lifecycle, w.set_bucket_quota,
                   w.remove_incomplete_uploads):
        assert medium._risk_level == "medium", medium.__name__


# ── connection: health endpoints + error translation + metrics token ─────


class _Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


@pytest.mark.unit
def test_connection_health_and_metrics_auth(monkeypatch):
    from minio_aiops.config import TargetConfig
    from minio_aiops.connection import MinioConnection

    monkeypatch.setenv("MINIO_LAB1_SECRET_KEY", "sk-test")
    target = TargetConfig(name="lab1", host="minio.local", access_key="ak", secure=False)
    calls = []

    class _Http:
        def get(self, path, headers=None, **k):
            calls.append((path, headers or {}))
            if path == "/minio/health/cluster":
                return _Resp(503, headers={"X-Minio-Write-Quorum": "2"})
            if path == "/minio/v2/metrics/cluster":
                return _Resp(200, text="minio_cluster_bucket_total 3\n")
            return _Resp(200)

        def close(self):
            pass

    conn = MinioConnection(target, http_client=_Http())
    assert conn.health_live()["healthy"] is True
    cluster = conn.health_cluster()
    assert cluster["healthy"] is False and cluster["writeQuorum"] == "2"
    metrics = conn.metrics()
    assert metrics["minio_cluster_bucket_total"][0]["value"] == 3.0
    # metrics_public=False → the scrape must carry a derived bearer token.
    metrics_headers = [h for p, h in calls if p == "/minio/v2/metrics/cluster"][0]
    auth = metrics_headers.get("Authorization", "")
    assert auth.startswith("Bearer ") and auth.count(".") == 2  # JWT shape


@pytest.mark.unit
def test_connection_public_metrics_sends_no_token(monkeypatch):
    from minio_aiops.config import TargetConfig
    from minio_aiops.connection import MinioConnection

    monkeypatch.setenv("MINIO_LAB1_SECRET_KEY", "sk-test")
    target = TargetConfig(name="lab1", host="minio.local", access_key="ak",
                          secure=False, metrics_public=True)
    seen = {}

    class _Http:
        def get(self, path, headers=None, **k):
            seen["headers"] = headers or {}
            return _Resp(200, text="# nothing\n")

        def close(self):
            pass

    MinioConnection(target, http_client=_Http()).metrics_text()
    assert "Authorization" not in seen["headers"]


@pytest.mark.unit
def test_connection_error_translation():
    from minio_aiops.config import TargetConfig
    from minio_aiops.connection import MinioApiError, MinioConnection

    target = TargetConfig(name="lab1", host="minio.local", access_key="ak", secure=False)

    class _S3Error(Exception):
        code = "AccessDenied"
        response = None

    class _Client:
        def list_buckets(self):
            raise _S3Error("access denied")

    conn = MinioConnection(target, client=_Client())
    with pytest.raises(MinioApiError, match="Authentication/authorization"):
        conn.list_buckets()


# ── flagship capacity RCA ─────────────────────────────────────────────────


def _capacity_metrics(used_ratio: float, offline_drives: int = 0):
    total = 1000.0
    return {
        "minio_cluster_capacity_usable_total_bytes": [{"labels": {}, "value": total}],
        "minio_cluster_capacity_usable_free_bytes": [
            {"labels": {}, "value": total * (1 - used_ratio)}
        ],
        "minio_cluster_drive_offline_total": [{"labels": {}, "value": float(offline_drives)}],
        "minio_cluster_drive_online_total": [{"labels": {}, "value": 4.0}],
    }


@pytest.mark.unit
def test_capacity_rca_maps_nearfull_to_cause_and_action():
    from minio_aiops.ops import capacity as ops

    conn = MagicMock(name="conn")
    conn.metrics.return_value = _capacity_metrics(0.90)
    out = ops.capacity_rca(conn)
    assert out["healthy"] is False
    issues = {f["issue"] for f in out["findings"]}
    assert "CLUSTER_NEARFULL" in issues
    finding = next(f for f in out["findings"] if f["issue"] == "CLUSTER_NEARFULL")
    assert finding["cause"] and finding["suggestedAction"]


@pytest.mark.unit
def test_capacity_rca_full_and_offline_drives_are_critical():
    from minio_aiops.ops import capacity as ops

    conn = MagicMock(name="conn")
    conn.metrics.return_value = _capacity_metrics(0.97, offline_drives=2)
    out = ops.capacity_rca(conn)
    issues = {f["issue"]: f["severity"] for f in out["findings"]}
    assert issues.get("CLUSTER_FULL") == "critical"
    assert issues.get("DRIVES_OFFLINE") == "critical"


@pytest.mark.unit
def test_capacity_rca_resilient_to_failure():
    from minio_aiops.ops import capacity as ops

    conn = MagicMock(name="conn")
    conn.metrics.side_effect = RuntimeError("scrape failed")
    assert "error" in ops.capacity_rca(conn)


# ── bucket writes: undo, before-state, dry-run ────────────────────────────


@pytest.mark.unit
def test_set_versioning_captures_prior_and_records_undo(monkeypatch):
    import minio_aiops.governance.undo as undo_mod
    from mcp_server.tools import bucket_writes as w

    conn = MagicMock(name="conn")
    conn.get_bucket_versioning.return_value = "Suspended"
    monkeypatch.setattr(w, "_get_connection", lambda target=None: conn)

    recorded = {}

    class _Store:
        def record(self, *, skill, tool, undo_descriptor, orig_params, effect_verified=True):
            recorded["d"] = undo_descriptor
            return "undo-1"

    monkeypatch.setattr(undo_mod, "get_undo_store", lambda: _Store())

    result = w.set_versioning(bucket_name="data-bkt", status="Enabled")
    assert result["priorState"]["versioning"] == "Suspended"
    assert recorded["d"]["tool"] == "set_versioning"
    assert recorded["d"]["params"] == {"bucket_name": "data-bkt", "status": "Suspended"}
    assert result.get("_undo_id") == "undo-1"


@pytest.mark.unit
def test_bucket_delete_dry_run_does_not_mutate(monkeypatch):
    from mcp_server.tools import bucket_writes as w

    conn = MagicMock(name="conn")
    monkeypatch.setattr(w, "_get_connection", lambda target=None: conn)
    result = w.bucket_delete(bucket_name="data-bkt", dry_run=True)
    assert result["dryRun"] is True
    conn.remove_bucket.assert_not_called()
    conn.is_bucket_empty.assert_not_called()  # dry-run makes no client call at all


@pytest.mark.unit
def test_cli_bucket_delete_dry_run_gates():
    from minio_aiops.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["bucket", "delete", "data-bkt", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output


@pytest.mark.unit
def test_set_bucket_policy_captures_prior():
    from minio_aiops.ops import bucket_writes as ops

    conn = MagicMock(name="conn")
    conn.get_bucket_policy.return_value = '{"Version":"2012-10-17","Statement":[]}'
    out = ops.set_bucket_policy(conn, "data-bkt", '{"Statement": []}')
    assert out["action"] == "set_bucket_policy"
    assert out["priorState"]["policyJson"].startswith('{"Version"')
    conn.set_bucket_policy.assert_called_once_with("data-bkt", '{"Statement": []}')
