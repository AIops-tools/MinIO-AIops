"""``minio-aiops heal`` — healing & erasure-set health reads."""

from __future__ import annotations

import json

import typer

from minio_aiops.cli._common import TargetOption, cli_errors, console, get_connection

heal_app = typer.Typer(
    name="heal",
    help="Healing backlog, erasure-set quorum risk, drive/node status.",
    no_args_is_help=True,
)


@heal_app.command("status")
@cli_errors
def heal_status(target: TargetOption = None) -> None:
    """Healing backlog + per-erasure-set write-quorum risk, cause + action."""
    from minio_aiops.ops import healing as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.healing_health(conn)))


@heal_app.command("drives")
@cli_errors
def heal_drives(target: TargetOption = None) -> None:
    """Per-drive rows (server, drive, used ratio), fullest first."""
    from minio_aiops.ops import healing as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.drive_status(conn)))


@heal_app.command("nodes")
@cli_errors
def heal_nodes(target: TargetOption = None) -> None:
    """Node-level view: online/offline nodes + per-node drive counts."""
    from minio_aiops.ops import healing as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.node_status(conn)))
