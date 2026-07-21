"""Configuration management for MinIO AIops.

Loads MinIO server connection targets from a YAML config file. The secret
(the **secret key** of the access/secret key pair) is NEVER stored in the
config file and never on disk in plaintext: it lives in the encrypted store
``~/.minio-aiops/secrets.enc`` (see :mod:`minio_aiops.secretstore`). For
backward compatibility a legacy plaintext env var (``MINIO_<TARGET>_SECRET_KEY``)
is still honoured as a fallback, with a warning nudging migration to the
encrypted store.

A "target" is one MinIO deployment reached through its S3 API endpoint
(``host:port``, default port ``9000``). Auth is the S3 access/secret key pair
(SigV4, handled by the MinIO SDK in :mod:`minio_aiops.connection`).
``access_key`` lives in the config file (it identifies, like a username);
the secret key lives encrypted.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from minio_aiops.governance.paths import ops_home
from minio_aiops.secretstore import (
    MasterPasswordError,
    SecretStoreError,
    get_secret,
    has_store,
)

CONFIG_DIR = ops_home()
CONFIG_FILE = CONFIG_DIR / "config.yaml"
ENV_FILE = CONFIG_DIR / ".env"

# MinIO S3 API defaults.
DEFAULT_PORT = 9000

# Legacy env-var prefix/suffix; also used by the migration helper.
SECRET_ENV_PREFIX = "MINIO_"  # nosec B105 — env-var name, not a secret
SECRET_ENV_SUFFIX = "_SECRET_KEY"  # nosec B105 — env-var name, not a secret

_log = logging.getLogger("minio-aiops.config")


def _secret_env_key(name: str) -> str:
    """Legacy per-target secret-key env var name, e.g. MINIO_LAB1_SECRET_KEY."""
    return f"{SECRET_ENV_PREFIX}{name.upper().replace('-', '_')}{SECRET_ENV_SUFFIX}"


def _resolve_secret(name: str) -> str:
    """Return a target's secret key: encrypted store first, then legacy env var."""
    if has_store():
        try:
            return get_secret(name)
        except MasterPasswordError:
            # A wrong or missing master password is NOT "this target has no
            # secret". Falling through resurfaced it as "No API key for target
            # X", sending the operator to add a credential that is already
            # there. MasterPasswordError subclasses SecretStoreError, so the
            # broad catch below would swallow it — re-raise first.
            raise
        except SecretStoreError:
            pass  # no secret stored for this target — try the legacy env var
    legacy = os.environ.get(_secret_env_key(name))
    if legacy:
        _log.warning(
            "Using plaintext env var %s. Migrate to the encrypted store with "
            "'minio-aiops secret migrate'.",
            _secret_env_key(name),
        )
        return legacy
    raise OSError(
        f"No secret key for target '{name}'. Add one with "
        f"'minio-aiops secret set {name}' (stored encrypted), or run "
        f"'minio-aiops init'."
    )


@dataclass(frozen=True)
class TargetConfig:
    """A connection target for one MinIO deployment's S3 API endpoint.

    The secret key is sourced from the encrypted secret store (see
    ``secret_key``), never the config file. ``host`` is the server host/FQDN;
    ``port`` defaults to ``9000``; ``access_key`` identifies the account and is
    not treated as a secret. ``secure`` selects http/https; ``verify_ssl``
    controls certificate verification (labs often use self-signed certs).
    ``metrics_public`` marks deployments running with the metrics endpoint
    open (``MINIO_PROMETHEUS_AUTH_TYPE=public``) — otherwise a bearer token is
    derived from the credentials for metrics scrapes.
    """

    name: str
    host: str
    port: int = DEFAULT_PORT
    access_key: str = ""
    secure: bool = True
    verify_ssl: bool = True
    region: str = ""
    metrics_public: bool = False

    @property
    def secret_key(self) -> str:
        return _resolve_secret(self.name)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def base_url(self) -> str:
        scheme = "https" if self.secure else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class AppConfig:
    """Top-level application config."""

    targets: tuple[TargetConfig, ...] = ()

    def get_target(self, name: str) -> TargetConfig:
        for t in self.targets:
            if t.name == name:
                return t
        available = ", ".join(t.name for t in self.targets) or "(none)"
        raise KeyError(f"Target '{name}' not found. Available: {available}")

    @property
    def default_target(self) -> TargetConfig:
        if not self.targets:
            raise ValueError("No targets configured. Check config.yaml")
        return self.targets[0]


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load config from YAML; the secret key comes from the encrypted store."""
    path = config_path or CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Run 'minio-aiops init' to set up a MinIO target and store its "
            f"secret key encrypted, or create {CONFIG_FILE} with a 'targets' list."
        )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    targets = tuple(
        TargetConfig(
            name=t["name"],
            host=t["host"],
            port=t.get("port", DEFAULT_PORT),
            access_key=t.get("access_key", ""),
            secure=t.get("secure", True),
            verify_ssl=t.get("verify_ssl", True),
            region=t.get("region", ""),
            metrics_public=t.get("metrics_public", False),
        )
        for t in raw.get("targets", [])
    )

    return AppConfig(targets=targets)
