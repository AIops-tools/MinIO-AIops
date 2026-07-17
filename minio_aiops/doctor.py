"""Environment and connectivity diagnostics for MinIO AIops."""

from __future__ import annotations

from rich.console import Console

from minio_aiops.config import CONFIG_FILE, ENV_FILE, load_config
from minio_aiops.secretstore import SECRETS_FILE, check_permissions, has_store

_console = Console()


def run_doctor(skip_auth: bool = False) -> int:
    """Check config, secrets, and (optionally) connectivity.

    The connectivity pass probes all four access paths per target: liveness,
    readiness, an authenticated S3 call (list buckets), and the metrics
    endpoint the RCA analyses depend on. Returns a process exit code: 0
    healthy, 1 problems found. Connectivity failures are reported as status,
    never raised as tracebacks (a doctor must survive the thing it diagnoses
    being unhealthy).
    """
    problems = 0

    if not CONFIG_FILE.exists():
        _console.print(f"[red]✗ Config file missing: {CONFIG_FILE}[/]")
        _console.print("[yellow]  Run 'minio-aiops init' to set up your first target.[/]")
        return 1
    _console.print(f"[green]✓ Config file present: {CONFIG_FILE}[/]")

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 — report, do not crash
        _console.print(f"[red]✗ Config load failed: {exc}[/]")
        return 1

    if not config.targets:
        _console.print("[red]✗ No targets configured[/]")
        return 1
    _console.print(f"[green]✓ {len(config.targets)} target(s) configured[/]")

    if has_store():
        _console.print(f"[green]✓ Encrypted secret store present: {SECRETS_FILE}[/]")
        perm_warning = check_permissions()
        if perm_warning:
            _console.print(f"[yellow]! {perm_warning}[/]")
    elif ENV_FILE.exists():
        _console.print(
            f"[yellow]! Using legacy plaintext .env ({ENV_FILE}). Migrate with "
            f"'minio-aiops secret migrate'.[/]"
        )
    else:
        _console.print(
            "[yellow]! No secret store yet. Run 'minio-aiops init' to set up "
            "credentials (stored encrypted).[/]"
        )
        problems += 1

    for target in config.targets:
        try:
            _ = target.secret_key
            _console.print(f"[green]✓ Secret key present for '{target.name}'[/]")
        except OSError as exc:
            _console.print(f"[red]✗ {exc}[/]")
            problems += 1

    if skip_auth:
        _console.print("[dim]Skipping connectivity check (--skip-auth).[/]")
        return 1 if problems else 0

    from minio_aiops.connection import ConnectionManager

    mgr = ConnectionManager(config)
    for target in config.targets:
        label = f"'{target.name}' ({target.host}:{target.port})"
        try:
            conn = mgr.connect(target.name)
        except Exception as exc:  # noqa: BLE001 — connectivity is a status, not a crash
            _console.print(f"[red]✗ Connect to {label} failed: {exc}[/]")
            problems += 1
            continue

        # 1) liveness + readiness (unauthenticated endpoints).
        try:
            live = conn.health_live()
            ready = conn.health_ready()
            if live.get("healthy") and ready.get("healthy"):
                _console.print(f"[green]✓ {label}: server live + ready[/]")
            else:
                _console.print(
                    f"[red]✗ {label}: live={live.get('statusCode')} "
                    f"ready={ready.get('statusCode')} — server unhealthy[/]"
                )
                problems += 1
        except Exception as exc:  # noqa: BLE001
            _console.print(f"[red]✗ {label}: health endpoints unreachable: {exc}[/]")
            problems += 1
            continue

        # 2) authenticated S3 call — proves the key pair works.
        try:
            buckets = conn.list_buckets()
            _console.print(
                f"[green]✓ {label}: S3 API authenticated, {len(buckets)} bucket(s)[/]"
            )
        except Exception as exc:  # noqa: BLE001
            _console.print(f"[red]✗ {label}: S3 auth/list failed: {exc}[/]")
            problems += 1

        # 3) metrics endpoint — the capacity/healing RCAs depend on it.
        try:
            conn.metrics_text()
            _console.print(f"[green]✓ {label}: metrics endpoint reachable[/]")
        except Exception as exc:  # noqa: BLE001
            _console.print(
                f"[red]✗ {label}: metrics endpoint failed: {exc}[/]\n"
                f"[yellow]  Capacity/healing RCAs need it. If the server runs "
                f"MINIO_PROMETHEUS_AUTH_TYPE=public, set metrics_public: true for "
                f"this target in config.yaml.[/]"
            )
            problems += 1

    return 1 if problems else 0
