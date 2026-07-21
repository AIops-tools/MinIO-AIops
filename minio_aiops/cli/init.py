"""``minio-aiops init`` — a friendly, interactive onboarding wizard.

Walks a new user through connecting their first MinIO target: collects the
non-secret connection details into ``config.yaml`` and the secret key into the
*encrypted* store (never plaintext on disk). Designed to be run on a terminal;
everything it needs is prompted with sensible defaults.
"""

from __future__ import annotations

import getpass

import typer
import yaml

from minio_aiops.cli._common import cli_errors, console
from minio_aiops.config import CONFIG_DIR, CONFIG_FILE, DEFAULT_PORT
from minio_aiops.secretstore import SecretStore, resolve_master_password


def _load_existing_targets() -> list[dict]:
    if not CONFIG_FILE.exists():
        return []
    raw = yaml.safe_load(CONFIG_FILE.read_text("utf-8")) or {}
    return list(raw.get("targets", []))


def _write_targets(targets: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    CONFIG_FILE.write_text(yaml.safe_dump({"targets": targets}, sort_keys=False), "utf-8")


@cli_errors
def init_cmd() -> None:
    """Interactively set up your first MinIO connection."""
    console.print("[bold cyan]MinIO AIops — setup wizard[/]")
    console.print(
        "This collects MinIO server connection details (saved to config.yaml) "
        "and your secret key (saved [bold]encrypted[/] to secrets.enc).\n"
    )

    console.print("[bold]Step 1 — master password[/]")
    console.print(
        "[dim]Encrypts secrets.enc. You'll set it via the "
        "MINIO_AIOPS_MASTER_PASSWORD env var for non-interactive/MCP use.[/]"
    )
    password = resolve_master_password(confirm_if_new=True)
    store = SecretStore.unlock(password)

    targets = _load_existing_targets()
    existing_names = {t.get("name") for t in targets}

    while True:
        console.print("\n[bold]Step 2 — add a MinIO deployment[/]")
        name = typer.prompt("Target name (e.g. lab1)").strip()
        if name in existing_names:
            if not typer.confirm(f"'{name}' already exists — overwrite?", default=False):
                continue
            targets = [t for t in targets if t.get("name") != name]

        host = typer.prompt("Host (IP or FQDN of the MinIO server)").strip()
        port = typer.prompt("S3 API port", default=DEFAULT_PORT, type=int)
        secure = typer.confirm("Use TLS (https)?", default=True)
        verify_ssl = True
        if secure:
            console.print("[dim]Lab / self-signed server certificates: answer No here.[/]")
            verify_ssl = typer.confirm(
                "Verify TLS certificate? (No for self-signed lab certs)", default=True
            )
        region = typer.prompt("Region (optional, blank for none)", default="").strip()
        access_key = typer.prompt("Access key").strip()
        console.print(
            "[dim]Enter the secret key for this access key (input hidden). "
            "Admin features (quota, server info) need admin-capable keys.[/]"
        )
        secret = getpass.getpass(f"Secret key for '{access_key}@{name}' (hidden): ")
        store = store.set(name, secret)
        metrics_public = typer.confirm(
            "Is the metrics endpoint public (server runs "
            "MINIO_PROMETHEUS_AUTH_TYPE=public)?",
            default=False,
        )

        entry = {
            "name": name,
            "host": host,
            "port": port,
            "access_key": access_key,
            "secure": secure,
            "verify_ssl": verify_ssl,
            "region": region,
            "metrics_public": metrics_public,
        }
        targets.append(entry)
        existing_names.add(name)
        _write_targets(targets)
        console.print(f"[green]✓ Saved target '{name}' (secret key stored encrypted).[/]")

        if not typer.confirm("\nAdd another target?", default=False):
            break

    console.print(f"\n[green]✓ Setup complete.[/] Config: {CONFIG_FILE}")
    console.print(
        "[dim]Tip: export MINIO_AIOPS_MASTER_PASSWORD=... in your shell profile "
        "so the MCP server and CLI can unlock secrets non-interactively.[/]"
    )
    if typer.confirm("Run a connectivity check now (minio-aiops doctor)?", default=True):
        from minio_aiops.doctor import run_doctor

        raise typer.Exit(run_doctor())
