"""Connection management for a MinIO deployment.

Three access paths, all behind one ``MinioConnection`` per target:

  * **S3 API** via the official ``minio`` SDK (``Minio`` client, SigV4-signed):
    bucket inventory, policies, versioning, lifecycle, encryption, objects,
    multipart uploads. All SDK/network failures are translated centrally into
    ``MinioApiError`` with a teaching message.
  * **Admin API** via the SDK's ``MinioAdmin`` (bucket quota, server info) —
    requires credentials with admin rights; a 403 is translated into a hint.
  * **health + metrics endpoints** via a plain httpx client:
    ``/minio/health/live|ready|cluster`` (unauthenticated by design) and the
    metrics endpoint ``/minio/v2/metrics/cluster``. Metrics auth follows
    the server's ``MINIO_PROMETHEUS_AUTH_TYPE``: ``public`` needs no header;
    the default (``jwt``) accepts a bearer token which this connection derives
    from the target's credentials (same HS512 construction the ``mc admin``
    tooling uses) — no extra secret to manage.

Every client is injectable for tests: pass ``client=`` / ``admin_client=`` /
``http_client=`` to substitute fakes; nothing touches the network at
construction time (all three are built lazily on first use).
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from minio_aiops.config import AppConfig, TargetConfig, load_config

_TIMEOUT = 30.0

_HEALTH_LIVE = "/minio/health/live"
_HEALTH_READY = "/minio/health/ready"
_HEALTH_CLUSTER = "/minio/health/cluster"
_METRICS_CLUSTER = "/minio/v2/metrics/cluster"

# S3 error codes that mean "the sub-resource simply isn't configured" — reads
# translate these to None rather than raising.
_ABSENT_CODES = {
    "NoSuchBucketPolicy",
    "NoSuchLifecycleConfiguration",
    "ServerSideEncryptionConfigurationNotFoundError",
    "NoSuchTagSet",
    "NoSuchTagSetError",
}


class MinioApiError(Exception):
    """A MinIO call failed; carries a teaching message + optional status code."""

    def __init__(self, message: str, *, status_code: int | None = None, op: str = "") -> None:
        self.status_code = status_code
        self.op = op
        super().__init__(message)


def _teach(exc: Exception, op: str, target: TargetConfig) -> MinioApiError:
    """Translate an SDK/network exception into an actionable MinioApiError."""
    code = getattr(exc, "code", None)
    status = getattr(getattr(exc, "response", None), "status", None)
    detail = str(exc)[:200]
    if code in ("AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId") or status in (
        401,
        403,
    ):
        return MinioApiError(
            f"Authentication/authorization failed on {op} against {target.endpoint}. "
            f"Check the access/secret key pair (admin operations additionally need "
            f"admin-capable credentials). {detail}",
            status_code=status,
            op=op,
        )
    if code == "NoSuchBucket" or status == 404:
        return MinioApiError(
            f"Bucket or resource not found on {op}. The name may be stale — "
            f"list buckets first (bucket_ls). {detail}",
            status_code=status or 404,
            op=op,
        )
    if isinstance(exc, (httpx.HTTPError, OSError)) and not code:
        return MinioApiError(
            f"Could not reach the MinIO server at {target.base_url} ({op}): {detail}. "
            f"Check host/port, TLS settings (secure/verify_ssl), and that the "
            f"server is running.",
            op=op,
        )
    return MinioApiError(f"MinIO API error on {op}: {detail}", status_code=status, op=op)


def _prom_bearer_token(access_key: str, secret_key: str, ttl_seconds: int = 3600) -> str:
    """Derive the metrics bearer JWT from the credentials (HS512).

    The MinIO server (in its default ``jwt`` metrics-auth mode) validates a
    JWT signed with the secret key, subject = access key — the same token
    construction the official admin tooling generates for scrape configs.
    """

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64url(json.dumps({"alg": "HS512", "typ": "JWT"}).encode())
    now = int(time.time())
    claims = b64url(
        json.dumps(
            {"sub": access_key, "iss": "prometheus", "iat": now, "exp": now + ttl_seconds}
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    sig = hmac.new(secret_key.encode(), signing_input, hashlib.sha512).digest()
    return f"{header}.{claims}.{b64url(sig)}"


class MinioConnection:
    """A single session against one MinIO deployment (S3 + admin + health/metrics)."""

    def __init__(
        self,
        target: TargetConfig,
        client: Any | None = None,
        admin_client: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._target = target
        self._client = client
        self._admin = admin_client
        self._http = http_client

    @property
    def target(self) -> TargetConfig:
        return self._target

    # ── lazy clients ─────────────────────────────────────────────────────
    def _pool(self) -> Any:
        """A urllib3 pool with the tool-wide 30s timeout (the SDK default read
        timeout is minutes — too long for an interactive agent)."""
        import urllib3

        return urllib3.PoolManager(
            timeout=urllib3.util.Timeout(connect=_TIMEOUT, read=_TIMEOUT),
            retries=urllib3.util.Retry(total=2, backoff_factor=0.5),
            cert_reqs="CERT_REQUIRED" if self._target.verify_ssl else "CERT_NONE",
        )

    @property
    def client(self) -> Any:
        """The S3 SDK client (constructed on first use; injectable for tests)."""
        if self._client is None:
            from minio import Minio

            t = self._target
            self._client = Minio(
                t.endpoint,
                access_key=t.access_key,
                secret_key=t.secret_key,
                secure=t.secure,
                region=t.region or None,
                http_client=self._pool(),
            )
        return self._client

    @property
    def admin(self) -> Any:
        """The admin SDK client (bucket quota, server info)."""
        if self._admin is None:
            from minio.credentials import StaticProvider
            from minio.minioadmin import MinioAdmin

            t = self._target
            self._admin = MinioAdmin(
                endpoint=t.endpoint,
                credentials=StaticProvider(t.access_key, t.secret_key),
                region=t.region,
                secure=t.secure,
                http_client=self._pool(),
            )
        return self._admin

    @property
    def http(self) -> Any:
        """Plain HTTP client for the health + metrics endpoints."""
        if self._http is None:
            self._http = httpx.Client(
                base_url=self._target.base_url,
                verify=self._target.verify_ssl,
                timeout=_TIMEOUT,
            )
        return self._http

    # ── S3 reads ─────────────────────────────────────────────────────────
    def list_buckets(self) -> list[dict]:
        """All buckets: name + creation time (ISO)."""
        try:
            buckets = self.client.list_buckets()
        except Exception as exc:  # noqa: BLE001 — translated centrally
            raise _teach(exc, "list_buckets", self._target) from exc
        return [
            {
                "name": b.name,
                "createdAt": b.creation_date.isoformat() if b.creation_date else None,
            }
            for b in buckets
        ]

    def bucket_exists(self, bucket: str) -> bool:
        try:
            return bool(self.client.bucket_exists(bucket))
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"bucket_exists({bucket})", self._target) from exc

    def get_bucket_policy(self, bucket: str) -> str | None:
        """The bucket's policy JSON string, or None when no policy is set."""
        try:
            return self.client.get_bucket_policy(bucket)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) in _ABSENT_CODES:
                return None
            raise _teach(exc, f"get_bucket_policy({bucket})", self._target) from exc

    def get_bucket_versioning(self, bucket: str) -> str:
        """Versioning state: 'Enabled', 'Suspended', or 'Off' (never configured)."""
        try:
            cfg = self.client.get_bucket_versioning(bucket)
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"get_bucket_versioning({bucket})", self._target) from exc
        return getattr(cfg, "status_string", None) or getattr(cfg, "status", None) or "Off"

    def get_bucket_encryption(self, bucket: str) -> dict | None:
        """Default-encryption summary ({'sseAlgorithm': ...}) or None when unset."""
        try:
            cfg = self.client.get_bucket_encryption(bucket)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) in _ABSENT_CODES:
                return None
            raise _teach(exc, f"get_bucket_encryption({bucket})", self._target) from exc
        if cfg is None:
            return None
        rule = (getattr(cfg, "rule", None)) or None
        algorithm = getattr(rule, "sse_algorithm", None) if rule else None
        return {"sseAlgorithm": algorithm or "unknown"}

    def get_bucket_tags(self, bucket: str) -> dict | None:
        try:
            tags = self.client.get_bucket_tags(bucket)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) in _ABSENT_CODES:
                return None
            raise _teach(exc, f"get_bucket_tags({bucket})", self._target) from exc
        return dict(tags) if tags else None

    def get_bucket_lifecycle(self, bucket: str) -> list[dict] | None:
        """Lifecycle rules as plain dicts, or None when no lifecycle is set."""
        try:
            cfg = self.client.get_bucket_lifecycle(bucket)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) in _ABSENT_CODES:
                return None
            raise _teach(exc, f"get_bucket_lifecycle({bucket})", self._target) from exc
        if cfg is None:
            return None
        rules: list[dict] = []
        for r in getattr(cfg, "rules", []) or []:
            exp = getattr(r, "expiration", None)
            nve = getattr(r, "noncurrent_version_expiration", None)
            abort = getattr(r, "abort_incomplete_multipart_upload", None)
            rule_filter = getattr(r, "rule_filter", None)
            rules.append(
                {
                    "ruleId": getattr(r, "rule_id", None),
                    "status": getattr(r, "status", None),
                    "prefix": getattr(rule_filter, "prefix", None) if rule_filter else None,
                    "expirationDays": getattr(exp, "days", None) if exp else None,
                    "noncurrentExpirationDays": (
                        getattr(nve, "noncurrent_days", None) if nve else None
                    ),
                    "abortIncompleteDays": (
                        getattr(abort, "days_after_initiation", None) if abort else None
                    ),
                }
            )
        return rules

    def get_bucket_lifecycle_xml(self, bucket: str) -> str | None:
        """Lifecycle config serialised to XML (faithful undo capture), or None."""
        try:
            cfg = self.client.get_bucket_lifecycle(bucket)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) in _ABSENT_CODES:
                return None
            raise _teach(exc, f"get_bucket_lifecycle({bucket})", self._target) from exc
        if cfg is None:
            return None
        from minio import xml as mxml

        raw = mxml.marshal(cfg)
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def list_objects_page(
        self, bucket: str, prefix: str = "", limit: int = 100, include_versions: bool = False
    ) -> list[dict]:
        """First ``limit`` objects under ``prefix`` (bounded — never a full walk)."""
        try:
            rows: list[dict] = []
            for obj in self.client.list_objects(
                bucket, prefix=prefix or None, recursive=True, include_version=include_versions
            ):
                rows.append(
                    {
                        "objectName": obj.object_name,
                        "sizeBytes": obj.size,
                        "lastModified": (
                            obj.last_modified.isoformat() if obj.last_modified else None
                        ),
                        "isLatest": getattr(obj, "is_latest", None),
                        "versionId": getattr(obj, "version_id", None),
                    }
                )
                if len(rows) >= limit:
                    break
            return rows
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"list_objects({bucket})", self._target) from exc

    def is_bucket_empty(self, bucket: str) -> bool:
        """True when the bucket holds no objects, versions, or delete markers."""
        try:
            it = self.client.list_objects(bucket, recursive=True, include_version=True)
            return next(iter(it), None) is None
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"is_bucket_empty({bucket})", self._target) from exc

    def list_incomplete_uploads(self, bucket: str, prefix: str = "") -> list[dict]:
        """In-flight/abandoned multipart uploads: object, uploadId, initiated time.

        Uses the SDK's core ListMultipartUploads call (page-walked). This is the
        one place the tool relies on an SDK-internal method — the public alias
        was dropped from the SDK, and re-implementing SigV4 here would be worse.
        """
        uploads: list[dict] = []
        key_marker: str | None = None
        upload_id_marker: str | None = None
        try:
            while True:
                result = self.client._list_multipart_uploads(  # noqa: SLF001
                    bucket,
                    prefix=prefix or None,
                    key_marker=key_marker,
                    upload_id_marker=upload_id_marker,
                )
                for u in getattr(result, "uploads", []) or []:
                    uploads.append(
                        {
                            "objectName": u.object_name,
                            "uploadId": u.upload_id,
                            "initiated": (
                                u.initiated_time.isoformat() if u.initiated_time else None
                            ),
                        }
                    )
                if not getattr(result, "is_truncated", False):
                    break
                key_marker = getattr(result, "next_key_marker", None)
                upload_id_marker = getattr(result, "next_upload_id_marker", None)
                if not key_marker and not upload_id_marker:
                    break
            return uploads
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"list_incomplete_uploads({bucket})", self._target) from exc

    # ── S3 writes ────────────────────────────────────────────────────────
    def set_bucket_policy(self, bucket: str, policy_json: str) -> None:
        try:
            self.client.set_bucket_policy(bucket, policy_json)
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"set_bucket_policy({bucket})", self._target) from exc

    def delete_bucket_policy(self, bucket: str) -> None:
        try:
            self.client.delete_bucket_policy(bucket)
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"delete_bucket_policy({bucket})", self._target) from exc

    def set_bucket_versioning(self, bucket: str, status: str) -> None:
        """Set versioning: status is 'Enabled' or 'Suspended' (S3 semantics)."""
        try:
            from minio.versioningconfig import VersioningConfig

            self.client.set_bucket_versioning(bucket, VersioningConfig(status))
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"set_bucket_versioning({bucket})", self._target) from exc

    def set_bucket_lifecycle(
        self,
        bucket: str,
        *,
        expire_days: int | None = None,
        noncurrent_expire_days: int | None = None,
        abort_incomplete_days: int | None = None,
        prefix: str = "",
    ) -> None:
        """Replace the bucket lifecycle with rules built from the given knobs."""
        try:
            from minio.commonconfig import ENABLED, Filter
            from minio.lifecycleconfig import (
                AbortIncompleteMultipartUpload,
                Expiration,
                LifecycleConfig,
                NoncurrentVersionExpiration,
                Rule,
            )

            rules = []
            if expire_days is not None:
                rules.append(
                    Rule(
                        ENABLED,
                        rule_filter=Filter(prefix=prefix),
                        rule_id="minio-aiops-expire",
                        expiration=Expiration(days=expire_days),
                    )
                )
            if noncurrent_expire_days is not None:
                rules.append(
                    Rule(
                        ENABLED,
                        rule_filter=Filter(prefix=prefix),
                        rule_id="minio-aiops-noncurrent-expire",
                        noncurrent_version_expiration=NoncurrentVersionExpiration(
                            noncurrent_days=noncurrent_expire_days
                        ),
                    )
                )
            if abort_incomplete_days is not None:
                rules.append(
                    Rule(
                        ENABLED,
                        rule_filter=Filter(prefix=prefix),
                        rule_id="minio-aiops-abort-incomplete",
                        abort_incomplete_multipart_upload=AbortIncompleteMultipartUpload(
                            days_after_initiation=abort_incomplete_days
                        ),
                    )
                )
            if not rules:
                raise ValueError(
                    "set_bucket_lifecycle needs at least one of expire_days, "
                    "noncurrent_expire_days, abort_incomplete_days."
                )
            self.client.set_bucket_lifecycle(bucket, LifecycleConfig(rules))
        except MinioApiError:
            raise
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"set_bucket_lifecycle({bucket})", self._target) from exc

    def set_bucket_lifecycle_xml(self, bucket: str, lifecycle_xml: str) -> None:
        """Apply a lifecycle config verbatim from XML (undo-restore path)."""
        try:
            from minio import xml as mxml
            from minio.lifecycleconfig import LifecycleConfig

            self.client.set_bucket_lifecycle(
                bucket, mxml.unmarshal(LifecycleConfig, lifecycle_xml)
            )
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"set_bucket_lifecycle_xml({bucket})", self._target) from exc

    def delete_bucket_lifecycle(self, bucket: str) -> None:
        try:
            self.client.delete_bucket_lifecycle(bucket)
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"delete_bucket_lifecycle({bucket})", self._target) from exc

    def remove_bucket(self, bucket: str) -> None:
        try:
            self.client.remove_bucket(bucket)
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"remove_bucket({bucket})", self._target) from exc

    def abort_incomplete_upload(self, bucket: str, object_name: str, upload_id: str) -> None:
        """Abort one multipart upload (frees its stored parts)."""
        try:
            self.client._abort_multipart_upload(bucket, object_name, upload_id)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"abort_incomplete_upload({bucket})", self._target) from exc

    # ── admin API ────────────────────────────────────────────────────────
    def get_bucket_quota(self, bucket: str) -> dict:
        """The bucket's hard quota ({'quotaBytes': n} — 0/absent = unlimited)."""
        try:
            raw = self.admin.bucket_quota_get(bucket)
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"get_bucket_quota({bucket})", self._target) from exc
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
        quota = data.get("quota") or data.get("size") or 0
        return {"quotaBytes": int(quota) if quota else 0}

    def set_bucket_quota(self, bucket: str, size_bytes: int) -> None:
        """Set (size_bytes > 0) or clear (size_bytes == 0) the hard quota."""
        try:
            if size_bytes > 0:
                self.admin.bucket_quota_set(bucket, size_bytes)
            else:
                self.admin.bucket_quota_clear(bucket)
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, f"set_bucket_quota({bucket})", self._target) from exc

    def server_info(self) -> dict:
        """Admin server info (mode, pools, drives) as parsed JSON."""
        try:
            raw = self.admin.info()
        except Exception as exc:  # noqa: BLE001
            raise _teach(exc, "server_info", self._target) from exc
        return json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})

    # ── health + metrics ─────────────────────────────────────────────────
    def _health_get(self, path: str) -> dict:
        try:
            resp = self.http.get(path)
        except httpx.HTTPError as exc:
            raise _teach(exc, path, self._target) from exc
        return {
            "reachable": True,
            "healthy": 200 <= resp.status_code < 300,
            "statusCode": resp.status_code,
        }

    def health_live(self) -> dict:
        """Node liveness (unauthenticated `/minio/health/live`)."""
        return self._health_get(_HEALTH_LIVE)

    def health_ready(self) -> dict:
        """Node readiness (unauthenticated `/minio/health/ready`)."""
        return self._health_get(_HEALTH_READY)

    def health_cluster(self) -> dict:
        """Cluster write-quorum health (`/minio/health/cluster`, 503 = degraded)."""
        try:
            resp = self.http.get(_HEALTH_CLUSTER)
        except httpx.HTTPError as exc:
            raise _teach(exc, _HEALTH_CLUSTER, self._target) from exc
        headers = getattr(resp, "headers", {}) or {}
        return {
            "reachable": True,
            "healthy": 200 <= resp.status_code < 300,
            "statusCode": resp.status_code,
            "writeQuorum": headers.get("X-Minio-Write-Quorum"),
            "serverStatus": headers.get("X-Minio-Server-Status"),
        }

    def metrics_text(self) -> str:
        """Raw cluster metrics exposition text (bearer-token or public auth)."""
        headers = {}
        if not self._target.metrics_public:
            token = _prom_bearer_token(self._target.access_key, self._target.secret_key)
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self.http.get(_METRICS_CLUSTER, headers=headers)
        except httpx.HTTPError as exc:
            raise _teach(exc, _METRICS_CLUSTER, self._target) from exc
        if resp.status_code == 403:
            raise MinioApiError(
                f"Metrics endpoint denied access (403) at {_METRICS_CLUSTER}. If the "
                f"server runs with MINIO_PROMETHEUS_AUTH_TYPE=public, set "
                f"metrics_public: true for this target; otherwise the bearer token "
                f"derived from the credentials was rejected — check the key pair.",
                status_code=403,
                op=_METRICS_CLUSTER,
            )
        if not (200 <= resp.status_code < 300):
            raise MinioApiError(
                f"Metrics endpoint returned {resp.status_code} at {_METRICS_CLUSTER}.",
                status_code=resp.status_code,
                op=_METRICS_CLUSTER,
            )
        return resp.text

    def metrics(self) -> dict[str, list[dict]]:
        """Parsed cluster metrics: {metricName: [{labels, value}, ...]}."""
        from minio_aiops.prom import parse_metrics_text

        return parse_metrics_text(self.metrics_text())

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            self._http = None


class ConnectionManager:
    """Manages connections to multiple MinIO targets with session reuse."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._connections: dict[str, MinioConnection] = {}
        # Close any cached httpx clients on interpreter exit so long-running
        # processes (MCP server) do not leak sockets. Idempotent: disconnect_all
        # on an already-empty manager is a no-op.
        atexit.register(self.disconnect_all)

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> ConnectionManager:
        cfg = config or load_config()
        return cls(cfg)

    def connect(self, target_name: str | None = None) -> MinioConnection:
        """Connect to a target by name, or the default target."""
        target = (
            self._config.get_target(target_name)
            if target_name
            else self._config.default_target
        )
        cached = self._connections.get(target.name)
        if cached is not None:
            return cached
        conn = MinioConnection(target)
        self._connections[target.name] = conn
        return conn

    def disconnect(self, target_name: str) -> None:
        conn = self._connections.pop(target_name, None)
        if conn is not None:
            conn.close()

    def disconnect_all(self) -> None:
        for name in list(self._connections):
            self.disconnect(name)

    def list_targets(self) -> list[str]:
        return [t.name for t in self._config.targets]

    def list_connected(self) -> list[str]:
        return list(self._connections.keys())
