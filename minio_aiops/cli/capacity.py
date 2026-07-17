"""``minio-aiops capacity`` — capacity & usage RCA reads."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from minio_aiops.cli._common import TargetOption, cli_errors, console, get_connection

capacity_app = typer.Typer(
    name="capacity",
    help="Capacity & usage: RCA and per-bucket consumption.",
    no_args_is_help=True,
)


@capacity_app.command("rca")
@cli_errors
def capacity_rca(target: TargetOption = None) -> None:
    """Capacity & usage RCA: cause + suggested action per finding."""
    from minio_aiops.ops import capacity as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.capacity_rca(conn)))


@capacity_app.command("usage")
@cli_errors
def capacity_usage(
    limit: Annotated[int, typer.Option("--limit", help="Max rows")] = 25,
    target: TargetOption = None,
) -> None:
    """Per-bucket usage (bytes + objects), biggest first."""
    from minio_aiops.ops import capacity as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.usage_by_bucket(conn, limit=limit)))
