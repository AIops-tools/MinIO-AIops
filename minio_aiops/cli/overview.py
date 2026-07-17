"""``minio-aiops overview`` — one-shot deployment health."""

from __future__ import annotations

import json

from minio_aiops.cli._common import TargetOption, cli_errors, console, get_connection


@cli_errors
def overview_cmd(target: TargetOption = None) -> None:
    """One-shot summary: health + capacity headline + exposure headline."""
    from minio_aiops.ops import overview as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.fleet_overview(conn)))
