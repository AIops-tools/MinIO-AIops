"""``minio-aiops lock`` — object lock (WORM) reads + guarded writes.

Every real write is delegated to the ``@governed_tool``-wrapped twin in
``mcp_server.tools.objectlock`` so CLI writes are audited and undo-recorded on
the SAME governance path as the MCP tools. ``retention-set`` additionally
carries a double confirm and prints its irreversibility before asking, because
it is the one write in this tool that no credential can walk back.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from minio_aiops.cli._common import (
    DryRunOption,
    TargetOption,
    checked,
    cli_errors,
    console,
    double_confirm,
    dry_run_preview,
    get_connection,
    truncation_note,
)

lock_app = typer.Typer(
    name="lock",
    help="Object lock (WORM): config/status/gaps, default retention, per-object "
    "retention, legal hold.",
    no_args_is_help=True,
)

BucketArg = Annotated[str, typer.Argument(help="Bucket name (from 'bucket ls')")]
ObjectArg = Annotated[str, typer.Argument(help="Object key (from 'bucket objects')")]
VersionOption = Annotated[
    str | None, typer.Option("--version-id", help="Specific version; omit for current")
]


# ── reads ────────────────────────────────────────────────────────────────


@lock_app.command("config")
@cli_errors
def lock_config(bucket: BucketArg, target: TargetOption = None) -> None:
    """One bucket's object-lock state and default retention rule."""
    from minio_aiops.ops import objectlock as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.bucket_lock_config(conn, bucket)))


@lock_app.command("status")
@cli_errors
def lock_status(
    bucket: BucketArg,
    object_name: ObjectArg,
    version_id: VersionOption = None,
    target: TargetOption = None,
) -> None:
    """Retention + legal hold for one object version, and what blocks deletion."""
    from minio_aiops.ops import objectlock as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(
        ops.object_lock_status(conn, bucket, object_name, version_id=version_id)))


@lock_app.command("gaps")
@cli_errors
def lock_gaps(
    limit: Annotated[int, typer.Option("--limit", help="Max findings to return")] = 50,
    target: TargetOption = None,
) -> None:
    """WORM/retention gap findings across every bucket (worst first)."""
    from minio_aiops.ops import objectlock as ops

    conn, _ = get_connection(target)
    result = ops.diagnose_retention_gaps(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


# ── writes (delegated to the governed twins) ─────────────────────────────


@lock_app.command("bucket-create")
@cli_errors
def lock_bucket_create(
    bucket: BucketArg,
    object_lock: Annotated[
        bool, typer.Option("--object-lock", help="Enable WORM (only possible at creation)")
    ] = False,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Create a bucket, optionally WORM-capable (reversible while it stays empty)."""
    from mcp_server.tools import objectlock as gov

    if dry_run:
        dry_run_preview(
            gov.bucket_create(bucket_name=bucket, object_lock=object_lock,
                              dry_run=True, target=target),
            operation="create_bucket", api_call=f"PUT /{bucket}",
            parameters={"objectLock": object_lock})
        return
    console.print_json(json.dumps(checked(
        gov.bucket_create(bucket_name=bucket, object_lock=object_lock, target=target))))


@lock_app.command("default-set")
@cli_errors
def lock_default_set(
    bucket: BucketArg,
    mode: Annotated[str, typer.Argument(help="GOVERNANCE or COMPLIANCE")],
    days: Annotated[int | None, typer.Option("--days", help="Retention in days")] = None,
    years: Annotated[int | None, typer.Option("--years", help="Retention in years")] = None,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Set the bucket DEFAULT retention (reversible; applies to future uploads)."""
    from mcp_server.tools import objectlock as gov

    if dry_run:
        # Through the governed twin, which refuses a bucket without object lock:
        # a preview must report that refusal rather than a green banner.
        dry_run_preview(
            gov.set_default_retention(bucket_name=bucket, mode=mode, days=days,
                                      years=years, dry_run=True, target=target),
            operation="set_default_retention", api_call=f"PUT /{bucket}?object-lock",
            parameters={"mode": mode, "days": days, "years": years})
        return
    if str(mode).strip().upper() == "COMPLIANCE":
        console.print(
            "[bold yellow]COMPLIANCE default: every object written from now on "
            "becomes undeletable for the full period by everyone, root included, "
            "and its storage cannot be reclaimed early.[/]"
        )
        double_confirm("set a COMPLIANCE default retention on", bucket)
    console.print_json(json.dumps(checked(
        gov.set_default_retention(bucket_name=bucket, mode=mode, days=days,
                                  years=years, target=target))))


@lock_app.command("default-clear")
@cli_errors
def lock_default_clear(
    bucket: BucketArg,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Remove the DEFAULT retention rule (object lock stays enabled; reversible)."""
    from mcp_server.tools import objectlock as gov

    if dry_run:
        dry_run_preview(
            gov.clear_default_retention(bucket_name=bucket, dry_run=True, target=target),
            operation="clear_default_retention", api_call=f"PUT /{bucket}?object-lock",
            parameters={"clear": True})
        return
    console.print_json(json.dumps(checked(
        gov.clear_default_retention(bucket_name=bucket, target=target))))


@lock_app.command("retention-set")
@cli_errors
def lock_retention_set(
    bucket: BucketArg,
    object_name: ObjectArg,
    mode: Annotated[str, typer.Argument(help="GOVERNANCE or COMPLIANCE")],
    days: Annotated[int, typer.Option("--days", help="Retain for N days from now")],
    version_id: VersionOption = None,
    acknowledge_irreversible: Annotated[
        bool,
        typer.Option(
            "--acknowledge-irreversible",
            help="Required for COMPLIANCE mode: no credential can undo it",
        ),
    ] = False,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Put retention on one object version — IRREVERSIBLE (dry-run + double confirm)."""
    from mcp_server.tools import objectlock as gov

    if dry_run:
        # Through the governed twin so every guard (no object lock, weakening,
        # unacknowledged COMPLIANCE) fires in the preview too.
        preview = gov.set_object_retention(
            bucket_name=bucket, object_name=object_name, mode=mode, days=days,
            version_id=version_id,
            acknowledge_irreversible=acknowledge_irreversible,
            dry_run=True, target=target)
        # The banner carries the computed date and the no-undo fact, not just the
        # arguments echoed back. For the one write in this tool that no credential
        # can walk back, "which date does this become undeletable until, and is
        # there a way back" is the whole content of a useful preview — and the MCP
        # caller already gets both. A refusal has no such fields; dry_run_preview
        # prints it and exits non-zero before they are read.
        would = preview.get("wouldSetObjectRetention") or {} if isinstance(preview, dict) else {}
        current = preview.get("currentRetention") or {} if isinstance(preview, dict) else {}
        dry_run_preview(
            preview,
            operation="set_object_retention",
            api_call=f"PUT /{bucket}/{object_name}?retention",
            parameters={"mode": would.get("mode", mode), "days": days,
                        "retainUntil": would.get("retainUntil"),
                        "reversible": False,
                        "currentMode": current.get("mode"),
                        "currentRetainUntil": current.get("retainUntil")})
        return
    console.print(
        "[bold yellow]No undo exists for this write: shortening or removing "
        "retention needs the x-amz-bypass-governance-retention header, which "
        "this SDK never sends"
        + (
            ", and COMPLIANCE retention cannot be lifted by any credential "
            "before the date."
            if str(mode).strip().upper() == "COMPLIANCE"
            else "."
        )
        + "[/]"
    )
    double_confirm(f"set {str(mode).strip().upper()} retention for {days} days on",
                   f"{bucket}/{object_name}")
    console.print_json(json.dumps(checked(
        gov.set_object_retention(bucket_name=bucket, object_name=object_name, mode=mode,
                                 days=days, version_id=version_id,
                                 acknowledge_irreversible=acknowledge_irreversible,
                                 target=target))))


@lock_app.command("legal-hold")
@cli_errors
def lock_legal_hold(
    bucket: BucketArg,
    object_name: ObjectArg,
    state: Annotated[str, typer.Argument(help="on or off")],
    version_id: VersionOption = None,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Place or lift a legal hold on one object version (reversible by design)."""
    from mcp_server.tools import objectlock as gov

    normalized = str(state).strip().lower()
    if normalized not in ("on", "off"):
        console.print(f"[red]Error: state must be 'on' or 'off' (got {state!r}).[/]")
        raise typer.Exit(1)
    hold_on = normalized == "on"
    if dry_run:
        dry_run_preview(
            gov.set_legal_hold(bucket_name=bucket, object_name=object_name,
                               hold_on=hold_on, version_id=version_id,
                               dry_run=True, target=target),
            operation="set_legal_hold",
            api_call=f"PUT /{bucket}/{object_name}?legal-hold",
            parameters={"holdOn": hold_on})
        return
    console.print_json(json.dumps(checked(
        gov.set_legal_hold(bucket_name=bucket, object_name=object_name, hold_on=hold_on,
                           version_id=version_id, target=target))))
