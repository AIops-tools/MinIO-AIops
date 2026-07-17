"""Top-level Typer app: assembles sub-apps and top-level commands."""

from __future__ import annotations

import typer

from minio_aiops.cli._common import cli_errors
from minio_aiops.cli.bucket import bucket_app
from minio_aiops.cli.capacity import capacity_app
from minio_aiops.cli.doctor import doctor_cmd
from minio_aiops.cli.heal import heal_app
from minio_aiops.cli.health import health_app
from minio_aiops.cli.init import init_cmd
from minio_aiops.cli.overview import overview_cmd
from minio_aiops.cli.secret import secret_app
from minio_aiops.cli.undo import undo_app

app = typer.Typer(
    name="minio-aiops",
    help="Governed AI-ops for MinIO object storage: capacity RCA, bucket exposure "
    "audit, ILM gap analysis, healing health, guarded bucket writes.",
    no_args_is_help=True,
)

app.add_typer(health_app, name="health")
app.add_typer(bucket_app, name="bucket")
app.add_typer(capacity_app, name="capacity")
app.add_typer(heal_app, name="heal")
app.add_typer(secret_app, name="secret")
app.add_typer(undo_app, name="undo")
app.command("init")(init_cmd)
app.command("overview")(overview_cmd)
app.command("doctor")(doctor_cmd)


@app.command("mcp")
@cli_errors
def mcp_cmd() -> None:
    """Start the MCP server (stdio transport).

    Single-command entry point for MCP clients (does not go through uvx/PyPI
    resolution at launch):
        minio-aiops mcp
    """
    import sys

    if sys.version_info < (3, 11):
        typer.echo(
            f"ERROR: minio-aiops requires Python >= 3.11 "
            f"(got {sys.version_info.major}.{sys.version_info.minor}).\n"
            f"Fix: uv python install 3.12 && "
            f"uv tool install --python 3.12 --force minio-aiops",
            err=True,
        )
        raise typer.Exit(2)

    from mcp_server.server import main as _mcp_main

    _mcp_main()


if __name__ == "__main__":
    app()
