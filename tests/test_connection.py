"""MinioConnection tests: SDK/HTTP call boundary, canned-response normalization,
error translation, lazy client construction, and the metrics bearer token.

Every client is injected (client= / admin_client= / http_client=) so nothing
touches a real MinIO — the assertions pin *which* SDK/HTTP call ran with *what*
args and how the canned response is folded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from minio_aiops.config import AppConfig, TargetConfig
from minio_aiops.connection import (
    ConnectionManager,
    MinioApiError,
    MinioConnection,
    _prom_bearer_token,
    _teach,
)

pytestmark = pytest.mark.unit


def mk_target(**over):
    """A plain attribute bag standing in for TargetConfig (no secret store)."""
    base = dict(
        name="lab1",
        host="minio.local",
        port=9000,
        access_key="AKIA",
        secret_key="s3cr3t",
        secure=True,
        verify_ssl=True,
        region="us-east-1",
        metrics_public=False,
        endpoint="minio.local:9000",
        base_url="https://minio.local:9000",
    )
    base.update(over)
    return SimpleNamespace(**base)


def sdk_exc(*, code=None, status=None, message="boom"):
    """Fake minio SDK error carrying .code and .response.status like S3Error."""
    e = Exception(message)
    if code is not None:
        e.code = code
    if status is not None:
        e.response = SimpleNamespace(status=status)
    return e


# ── _teach: exception → teaching MinioApiError ─────────────────────────────


def test_teach_auth_by_code_and_by_status():
    t = mk_target()
    for exc in (sdk_exc(code="AccessDenied"), sdk_exc(status=403), sdk_exc(status=401)):
        err = _teach(exc, "op", t)
        assert isinstance(err, MinioApiError)
        assert "Authentication/authorization failed" in str(err)


def test_teach_not_found_by_code_and_status():
    t = mk_target()
    err = _teach(sdk_exc(code="NoSuchBucket"), "bucket_exists", t)
    assert err.status_code == 404 and "not found" in str(err)
    err2 = _teach(sdk_exc(status=404), "op", t)
    assert err2.status_code == 404


def test_teach_network_error_points_at_endpoint():
    t = mk_target()
    err = _teach(httpx.ConnectError("refused"), "health", t)
    assert t.base_url in str(err) and "Check host/port" in str(err)


def test_teach_generic_fallback_keeps_status():
    t = mk_target()
    err = _teach(sdk_exc(code="SomethingElse", status=500), "op", t)
    assert err.status_code == 500 and "MinIO API error" in str(err)


# ── _prom_bearer_token: HS512 JWT derivation ───────────────────────────────


def test_prom_bearer_token_is_valid_hs512_jwt():
    tok = _prom_bearer_token("AKIA", "s3cr3t", ttl_seconds=3600)
    header_b64, claims_b64, sig_b64 = tok.split(".")

    def unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    assert json.loads(unb64(header_b64)) == {"alg": "HS512", "typ": "JWT"}
    claims = json.loads(unb64(claims_b64))
    assert claims["sub"] == "AKIA" and claims["exp"] - claims["iat"] == 3600
    expected = hmac.new(
        b"s3cr3t", f"{header_b64}.{claims_b64}".encode("ascii"), hashlib.sha512
    ).digest()
    assert unb64(sig_b64) == expected


# ── lazy client construction ───────────────────────────────────────────────


def test_client_built_lazily_with_target_params(monkeypatch):
    import minio

    made = MagicMock(name="Minio")
    monkeypatch.setattr(minio, "Minio", made)
    conn = MinioConnection(mk_target(secure=True, region="us-east-1"))
    c1 = conn.client
    c2 = conn.client  # cached: constructed once
    assert c1 is c2 is made.return_value
    _, kw = made.call_args
    assert made.call_args.args[0] == "minio.local:9000"
    assert kw["access_key"] == "AKIA" and kw["secret_key"] == "s3cr3t"
    assert kw["secure"] is True and kw["region"] == "us-east-1"
    assert kw["http_client"] is not None  # _pool() wired in


def test_pool_cert_reqs_follows_verify_ssl(monkeypatch):
    import minio

    monkeypatch.setattr(minio, "Minio", MagicMock())
    # verify_ssl False must still build a pool (CERT_NONE branch) without error.
    conn = MinioConnection(mk_target(verify_ssl=False, region=""))
    assert conn.client is not None
    _, kw = minio.Minio.call_args
    assert kw["region"] is None  # empty region normalized to None


def test_admin_built_lazily(monkeypatch):
    import minio.credentials as creds
    import minio.minioadmin as ma

    admin = MagicMock(name="MinioAdmin")
    provider = MagicMock(name="StaticProvider")
    monkeypatch.setattr(ma, "MinioAdmin", admin)
    monkeypatch.setattr(creds, "StaticProvider", provider)
    conn = MinioConnection(mk_target())
    a1 = conn.admin
    assert a1 is conn.admin is admin.return_value
    provider.assert_called_once_with("AKIA", "s3cr3t")
    _, kw = admin.call_args
    assert kw["endpoint"] == "minio.local:9000" and kw["secure"] is True


def test_http_built_lazily(monkeypatch):
    fake = MagicMock(name="httpx.Client")
    monkeypatch.setattr(httpx, "Client", fake)
    conn = MinioConnection(mk_target())
    assert conn.http is conn.http is fake.return_value
    _, kw = fake.call_args
    assert kw["base_url"] == "https://minio.local:9000" and kw["timeout"] == 30.0
    assert kw["verify"] is True


def test_injected_clients_short_circuit_construction():
    c, a, h = MagicMock(), MagicMock(), MagicMock()
    conn = MinioConnection(mk_target(), client=c, admin_client=a, http_client=h)
    assert conn.client is c and conn.admin is a and conn.http is h
    assert conn.target.name == "lab1"


# ── S3 reads: which call + normalization ───────────────────────────────────


def test_list_buckets_normalizes_iso():
    c = MagicMock()
    c.list_buckets.return_value = [
        SimpleNamespace(name="a", creation_date=datetime(2026, 1, 2, tzinfo=UTC)),
        SimpleNamespace(name="b", creation_date=None),
    ]
    conn = MinioConnection(mk_target(), client=c)
    assert conn.list_buckets() == [
        {"name": "a", "createdAt": "2026-01-02T00:00:00+00:00"},
        {"name": "b", "createdAt": None},
    ]


def test_list_buckets_translates_error():
    c = MagicMock()
    c.list_buckets.side_effect = sdk_exc(code="AccessDenied")
    conn = MinioConnection(mk_target(), client=c)
    with pytest.raises(MinioApiError, match="Authentication"):
        conn.list_buckets()


def test_bucket_exists_passes_name():
    c = MagicMock()
    c.bucket_exists.return_value = True
    conn = MinioConnection(mk_target(), client=c)
    assert conn.bucket_exists("data") is True
    c.bucket_exists.assert_called_once_with("data")


def test_get_bucket_policy_absent_code_returns_none():
    c = MagicMock()
    c.get_bucket_policy.side_effect = sdk_exc(code="NoSuchBucketPolicy")
    conn = MinioConnection(mk_target(), client=c)
    assert conn.get_bucket_policy("data") is None


def test_get_bucket_policy_real_error_raises():
    c = MagicMock()
    c.get_bucket_policy.side_effect = sdk_exc(code="AccessDenied")
    conn = MinioConnection(mk_target(), client=c)
    with pytest.raises(MinioApiError):
        conn.get_bucket_policy("data")


def test_get_bucket_versioning_status_fallbacks():
    c = MagicMock()
    c.get_bucket_versioning.return_value = SimpleNamespace(status_string="Enabled")
    conn = MinioConnection(mk_target(), client=c)
    assert conn.get_bucket_versioning("d") == "Enabled"
    c.get_bucket_versioning.return_value = SimpleNamespace(status_string=None, status=None)
    assert conn.get_bucket_versioning("d") == "Off"


def test_get_bucket_encryption_folds_and_absent():
    c = MagicMock()
    rule = SimpleNamespace(sse_algorithm="AES256")
    c.get_bucket_encryption.return_value = SimpleNamespace(rule=rule)
    conn = MinioConnection(mk_target(), client=c)
    assert conn.get_bucket_encryption("d") == {"sseAlgorithm": "AES256"}
    c.get_bucket_encryption.return_value = None
    assert conn.get_bucket_encryption("d") is None
    c.get_bucket_encryption.side_effect = sdk_exc(
        code="ServerSideEncryptionConfigurationNotFoundError"
    )
    assert conn.get_bucket_encryption("d") is None


def test_get_bucket_tags_none_and_dict():
    c = MagicMock()
    c.get_bucket_tags.return_value = {"team": "data"}
    conn = MinioConnection(mk_target(), client=c)
    assert conn.get_bucket_tags("d") == {"team": "data"}
    c.get_bucket_tags.side_effect = sdk_exc(code="NoSuchTagSet")
    assert conn.get_bucket_tags("d") is None


def test_get_bucket_lifecycle_maps_rule_fields():
    c = MagicMock()
    rule = SimpleNamespace(
        rule_id="r1",
        status="Enabled",
        expiration=SimpleNamespace(days=30),
        noncurrent_version_expiration=SimpleNamespace(noncurrent_days=7),
        abort_incomplete_multipart_upload=SimpleNamespace(days_after_initiation=3),
        rule_filter=SimpleNamespace(prefix="logs/"),
    )
    c.get_bucket_lifecycle.return_value = SimpleNamespace(rules=[rule])
    conn = MinioConnection(mk_target(), client=c)
    assert conn.get_bucket_lifecycle("d") == [
        {
            "ruleId": "r1",
            "status": "Enabled",
            "prefix": "logs/",
            "expirationDays": 30,
            "noncurrentExpirationDays": 7,
            "abortIncompleteDays": 3,
        }
    ]


def test_get_bucket_lifecycle_absent_and_none():
    c = MagicMock()
    c.get_bucket_lifecycle.side_effect = sdk_exc(code="NoSuchLifecycleConfiguration")
    conn = MinioConnection(mk_target(), client=c)
    assert conn.get_bucket_lifecycle("d") is None
    c.get_bucket_lifecycle.side_effect = None
    c.get_bucket_lifecycle.return_value = None
    assert conn.get_bucket_lifecycle("d") is None


def test_get_bucket_lifecycle_xml_marshals(monkeypatch):
    from minio import xml as mxml

    c = MagicMock()
    c.get_bucket_lifecycle.return_value = SimpleNamespace(rules=[])
    monkeypatch.setattr(mxml, "marshal", lambda cfg: b"<LifecycleConfiguration/>")
    conn = MinioConnection(mk_target(), client=c)
    assert conn.get_bucket_lifecycle_xml("d") == "<LifecycleConfiguration/>"
    c.get_bucket_lifecycle.return_value = None
    assert conn.get_bucket_lifecycle_xml("d") is None


def test_list_objects_page_bounds_and_maps():
    c = MagicMock()
    objs = [
        SimpleNamespace(
            object_name=f"obj{i}",
            size=i,
            last_modified=datetime(2026, 1, 1, tzinfo=UTC),
            is_latest=True,
            version_id=None,
        )
        for i in range(5)
    ]
    c.list_objects.return_value = iter(objs)
    conn = MinioConnection(mk_target(), client=c)
    rows = conn.list_objects_page("d", prefix="p", limit=3, include_versions=True)
    assert len(rows) == 3 and rows[0]["objectName"] == "obj0"
    _, kw = c.list_objects.call_args
    assert kw["prefix"] == "p" and kw["recursive"] is True and kw["include_version"] is True


def test_list_objects_page_empty_prefix_becomes_none():
    c = MagicMock()
    c.list_objects.return_value = iter([])
    conn = MinioConnection(mk_target(), client=c)
    conn.list_objects_page("d", prefix="")
    _, kw = c.list_objects.call_args
    assert kw["prefix"] is None


def test_is_bucket_empty_true_and_false():
    c = MagicMock()
    c.list_objects.return_value = iter([])
    conn = MinioConnection(mk_target(), client=c)
    assert conn.is_bucket_empty("d") is True
    c.list_objects.return_value = iter([SimpleNamespace()])
    assert conn.is_bucket_empty("d") is False


def test_list_incomplete_uploads_pages_and_stops():
    c = MagicMock()
    page1 = SimpleNamespace(
        uploads=[
            SimpleNamespace(
                object_name="o1",
                upload_id="u1",
                initiated_time=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
        is_truncated=True,
        next_key_marker="o1",
        next_upload_id_marker="u1",
    )
    page2 = SimpleNamespace(
        uploads=[SimpleNamespace(object_name="o2", upload_id="u2", initiated_time=None)],
        is_truncated=False,
    )
    c._list_multipart_uploads.side_effect = [page1, page2]
    conn = MinioConnection(mk_target(), client=c)
    uploads = conn.list_incomplete_uploads("d", prefix="x")
    assert [u["objectName"] for u in uploads] == ["o1", "o2"]
    assert uploads[0]["initiated"] == "2026-01-01T00:00:00+00:00"
    assert uploads[1]["initiated"] is None
    assert c._list_multipart_uploads.call_count == 2


def test_list_incomplete_uploads_error_translated():
    c = MagicMock()
    c._list_multipart_uploads.side_effect = sdk_exc(code="NoSuchBucket")
    conn = MinioConnection(mk_target(), client=c)
    with pytest.raises(MinioApiError, match="not found"):
        conn.list_incomplete_uploads("d")


# ── S3 writes ──────────────────────────────────────────────────────────────


def test_set_and_delete_bucket_policy():
    c = MagicMock()
    conn = MinioConnection(mk_target(), client=c)
    conn.set_bucket_policy("d", '{"x":1}')
    c.set_bucket_policy.assert_called_once_with("d", '{"x":1}')
    conn.delete_bucket_policy("d")
    c.delete_bucket_policy.assert_called_once_with("d")


def test_set_bucket_versioning_wraps_config():
    c = MagicMock()
    conn = MinioConnection(mk_target(), client=c)
    conn.set_bucket_versioning("d", "Enabled")
    args = c.set_bucket_versioning.call_args.args
    assert args[0] == "d" and args[1].status == "Enabled"


def test_set_bucket_lifecycle_builds_all_three_rules():
    c = MagicMock()
    conn = MinioConnection(mk_target(), client=c)
    conn.set_bucket_lifecycle(
        "d", expire_days=30, noncurrent_expire_days=7, abort_incomplete_days=3, prefix="p/"
    )
    cfg = c.set_bucket_lifecycle.call_args.args[1]
    assert len(cfg.rules) == 3


def test_set_bucket_lifecycle_needs_at_least_one_knob():
    c = MagicMock()
    conn = MinioConnection(mk_target(), client=c)
    with pytest.raises(ValueError, match="at least one"):
        conn.set_bucket_lifecycle("d")
    c.set_bucket_lifecycle.assert_not_called()


def test_set_bucket_lifecycle_translates_sdk_error():
    c = MagicMock()
    c.set_bucket_lifecycle.side_effect = sdk_exc(code="AccessDenied")
    conn = MinioConnection(mk_target(), client=c)
    with pytest.raises(MinioApiError, match="Authentication"):
        conn.set_bucket_lifecycle("d", expire_days=1)


def test_set_bucket_lifecycle_xml_unmarshals():
    c = MagicMock()
    conn = MinioConnection(mk_target(), client=c)
    xml = (
        "<LifecycleConfiguration><Rule><ID>r</ID><Status>Enabled</Status>"
        "<Expiration><Days>1</Days></Expiration><Filter><Prefix></Prefix></Filter>"
        "</Rule></LifecycleConfiguration>"
    )
    conn.set_bucket_lifecycle_xml("d", xml)
    assert c.set_bucket_lifecycle.call_args.args[0] == "d"


def test_delete_lifecycle_remove_bucket_abort_upload():
    c = MagicMock()
    conn = MinioConnection(mk_target(), client=c)
    conn.delete_bucket_lifecycle("d")
    c.delete_bucket_lifecycle.assert_called_once_with("d")
    conn.remove_bucket("d")
    c.remove_bucket.assert_called_once_with("d")
    conn.abort_incomplete_upload("d", "obj", "uid")
    c._abort_multipart_upload.assert_called_once_with("d", "obj", "uid")


def test_write_errors_translate():
    c = MagicMock()
    c.remove_bucket.side_effect = sdk_exc(status=403)
    conn = MinioConnection(mk_target(), client=c)
    with pytest.raises(MinioApiError, match="Authentication"):
        conn.remove_bucket("d")


# ── admin API ──────────────────────────────────────────────────────────────


def test_get_bucket_quota_parses_json_and_bytes():
    a = MagicMock()
    a.bucket_quota_get.return_value = json.dumps({"quota": 1024})
    conn = MinioConnection(mk_target(), admin_client=a)
    assert conn.get_bucket_quota("d") == {"quotaBytes": 1024}
    a.bucket_quota_get.return_value = {"size": 2048}
    assert conn.get_bucket_quota("d") == {"quotaBytes": 2048}
    a.bucket_quota_get.return_value = {}
    assert conn.get_bucket_quota("d") == {"quotaBytes": 0}


def test_set_bucket_quota_set_vs_clear():
    a = MagicMock()
    conn = MinioConnection(mk_target(), admin_client=a)
    conn.set_bucket_quota("d", 500)
    a.bucket_quota_set.assert_called_once_with("d", 500)
    conn.set_bucket_quota("d", 0)
    a.bucket_quota_clear.assert_called_once_with("d")


def test_get_bucket_quota_error_translated():
    a = MagicMock()
    a.bucket_quota_get.side_effect = sdk_exc(status=403)
    conn = MinioConnection(mk_target(), admin_client=a)
    with pytest.raises(MinioApiError, match="Authentication"):
        conn.get_bucket_quota("d")


def test_server_info_parses_json_and_dict():
    a = MagicMock()
    a.info.return_value = json.dumps({"mode": "online"})
    conn = MinioConnection(mk_target(), admin_client=a)
    assert conn.server_info() == {"mode": "online"}
    a.info.return_value = None
    assert conn.server_info() == {}


# ── health + metrics ───────────────────────────────────────────────────────


def _resp(status=200, headers=None, text=""):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=text)


def test_health_live_ready_fold_status():
    h = MagicMock()
    h.get.return_value = _resp(200)
    conn = MinioConnection(mk_target(), http_client=h)
    assert conn.health_live() == {"reachable": True, "healthy": True, "statusCode": 200}
    h.get.assert_called_with("/minio/health/live")
    h.get.return_value = _resp(503)
    assert conn.health_ready()["healthy"] is False


def test_health_get_network_error_translated():
    h = MagicMock()
    h.get.side_effect = httpx.ConnectError("down")
    conn = MinioConnection(mk_target(), http_client=h)
    with pytest.raises(MinioApiError, match="Could not reach"):
        conn.health_live()


def test_health_cluster_exposes_quorum_headers():
    h = MagicMock()
    h.get.return_value = _resp(
        200, headers={"X-Minio-Write-Quorum": "3", "X-Minio-Server-Status": "ok"}
    )
    conn = MinioConnection(mk_target(), http_client=h)
    out = conn.health_cluster()
    assert out["writeQuorum"] == "3" and out["serverStatus"] == "ok" and out["healthy"]


def test_metrics_text_public_omits_auth_header():
    h = MagicMock()
    h.get.return_value = _resp(200, text="# HELP x\n")
    conn = MinioConnection(mk_target(metrics_public=True), http_client=h)
    assert conn.metrics_text() == "# HELP x\n"
    _, kw = h.get.call_args
    assert kw["headers"] == {}


def test_metrics_text_jwt_adds_bearer_header():
    h = MagicMock()
    h.get.return_value = _resp(200, text="ok")
    conn = MinioConnection(mk_target(metrics_public=False), http_client=h)
    conn.metrics_text()
    _, kw = h.get.call_args
    assert kw["headers"]["Authorization"].startswith("Bearer ")


def test_metrics_text_403_teaches_public_flag():
    h = MagicMock()
    h.get.return_value = _resp(403)
    conn = MinioConnection(mk_target(), http_client=h)
    with pytest.raises(MinioApiError, match="metrics_public") as ei:
        conn.metrics_text()
    assert ei.value.status_code == 403


def test_metrics_text_other_non_2xx_raises():
    h = MagicMock()
    h.get.return_value = _resp(500)
    conn = MinioConnection(mk_target(), http_client=h)
    with pytest.raises(MinioApiError, match="returned 500"):
        conn.metrics_text()


def test_metrics_parses_exposition_text():
    h = MagicMock()
    h.get.return_value = _resp(
        200,
        text=(
            "# HELP minio_cluster_capacity_raw_total_bytes total\n"
            "# TYPE minio_cluster_capacity_raw_total_bytes gauge\n"
            "minio_cluster_capacity_raw_total_bytes 1000\n"
        ),
    )
    conn = MinioConnection(mk_target(metrics_public=True), http_client=h)
    parsed = conn.metrics()
    assert parsed["minio_cluster_capacity_raw_total_bytes"][0]["value"] == 1000.0


def test_close_is_idempotent_and_swallows():
    h = MagicMock()
    conn = MinioConnection(mk_target(), http_client=h)
    conn.close()
    h.close.assert_called_once()
    conn.close()  # already closed → no second call, no error
    assert h.close.call_count == 1
    # a client whose close() raises must not propagate
    h2 = MagicMock()
    h2.close.side_effect = RuntimeError("x")
    conn2 = MinioConnection(mk_target(), http_client=h2)
    conn2.close()


# ── ConnectionManager ──────────────────────────────────────────────────────


def _app_config():
    return AppConfig(
        targets=(
            TargetConfig(name="lab1", host="a", access_key="k"),
            TargetConfig(name="lab2", host="b", access_key="k"),
        )
    )


def test_manager_connect_caches_per_target():
    mgr = ConnectionManager(_app_config())
    c1 = mgr.connect("lab1")
    assert mgr.connect("lab1") is c1
    assert mgr.connect() is c1  # default target is the first
    c2 = mgr.connect("lab2")
    assert c2 is not c1
    assert set(mgr.list_connected()) == {"lab1", "lab2"}
    assert mgr.list_targets() == ["lab1", "lab2"]


def test_manager_disconnect_and_disconnect_all():
    mgr = ConnectionManager(_app_config())
    conn = mgr.connect("lab1")
    conn._http = MagicMock()
    mgr.disconnect("lab1")
    assert mgr.list_connected() == []
    mgr.disconnect("lab1")  # no-op on absent target
    mgr.connect("lab1")
    mgr.connect("lab2")
    mgr.disconnect_all()
    assert mgr.list_connected() == []


def test_manager_from_config_uses_given_config():
    cfg = _app_config()
    mgr = ConnectionManager.from_config(cfg)
    assert mgr.list_targets() == ["lab1", "lab2"]
