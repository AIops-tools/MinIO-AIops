"""``minio-aiops health`` — service health + cluster status reads."""

from __future__ import annotations

import json

import typer

from minio_aiops.cli._common import TargetOption, cli_errors, console, get_connection

health_app = typer.Typer(
    name="health",
    help="Service health (live/ready/cluster quorum) and cluster status.",
    no_args_is_help=True,
)


@health_app.command("check")
@cli_errors
def health_check(target: TargetOption = None) -> None:
    """Liveness + readiness + cluster write-quorum health in one shot."""
    from minio_aiops.ops import health as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.service_health(conn)))


@health_app.command("status")
@cli_errors
def health_status(target: TargetOption = None) -> None:
    """Dashboard-header summary: nodes, drives, capacity, buckets, objects."""
    from minio_aiops.ops import health as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.cluster_status(conn)))
