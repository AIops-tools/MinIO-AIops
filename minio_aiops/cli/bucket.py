"""``minio-aiops bucket`` — bucket reads + guarded writes.

Real writes are delegated to the ``@governed_tool``-wrapped functions in
``mcp_server.tools.bucket_writes`` so that CLI writes are audited +
undo-recorded on the SAME governance path as the MCP tools (the CLI keeps its
own dry-run preview and human double-confirm on top).
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from minio_aiops.cli._common import (
    DryRunOption,
    TargetOption,
    cli_errors,
    console,
    double_confirm,
    dry_run_preview,
    dry_run_print,
    get_connection,
    truncation_note,
)

bucket_app = typer.Typer(
    name="bucket",
    help="Buckets: ls/info/audit/ilm-gap, policy/versioning/lifecycle/quota writes, "
    "delete, purge-uploads.",
    no_args_is_help=True,
)

BucketArg = Annotated[str, typer.Argument(help="Bucket name (from 'bucket ls')")]


# ── reads ────────────────────────────────────────────────────────────────


@bucket_app.command("ls")
@cli_errors
def bucket_ls(
    limit: Annotated[int, typer.Option("--limit", help="Max buckets to list")] = 500,
    target: TargetOption = None,
) -> None:
    """List buckets (name + creation time); says so when the list was cut off."""
    from minio_aiops.ops import buckets as ops

    conn, _ = get_connection(target)
    result = ops.list_buckets(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


@bucket_app.command("info")
@cli_errors
def bucket_info(bucket: BucketArg, target: TargetOption = None) -> None:
    """One bucket's full config: policy/versioning/lifecycle/encryption/quota/tags."""
    from minio_aiops.ops import buckets as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.bucket_info(conn, bucket)))


@bucket_app.command("audit")
@cli_errors
def bucket_audit(
    limit: Annotated[int, typer.Option("--limit", help="Max buckets to audit")] = 100,
    target: TargetOption = None,
) -> None:
    """Ranked bucket-exposure findings (riskiest first)."""
    from minio_aiops.ops import exposure as ops

    conn, _ = get_connection(target)
    result = ops.bucket_exposure_audit(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


@bucket_app.command("ilm-gap")
@cli_errors
def bucket_ilm_gap(
    limit: Annotated[int, typer.Option("--limit", help="Max buckets to analyze")] = 100,
    target: TargetOption = None,
) -> None:
    """Lifecycle/ILM gap analysis with a reclaimable estimate."""
    from minio_aiops.ops import ilm as ops

    conn, _ = get_connection(target)
    result = ops.lifecycle_gap_analysis(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


@bucket_app.command("uploads")
@cli_errors
def bucket_uploads(
    bucket: BucketArg,
    limit: Annotated[int, typer.Option("--limit", help="Max uploads to list")] = 200,
    target: TargetOption = None,
) -> None:
    """List incomplete multipart uploads for a bucket."""
    from minio_aiops.ops import buckets as ops

    conn, _ = get_connection(target)
    result = ops.list_incomplete_uploads(conn, bucket, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


@bucket_app.command("objects")
@cli_errors
def bucket_objects(
    bucket: BucketArg,
    prefix: Annotated[str, typer.Option("--prefix", help="Key prefix filter")] = "",
    limit: Annotated[int, typer.Option("--limit", help="Max objects (max 1000)")] = 100,
    target: TargetOption = None,
) -> None:
    """List objects in a bucket (bounded); says so when the page was cut off."""
    from minio_aiops.ops import buckets as ops

    conn, _ = get_connection(target)
    result = ops.list_objects(conn, bucket, prefix=prefix, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


# ── writes (delegated to the governed twins) ─────────────────────────────


@bucket_app.command("versioning-set")
@cli_errors
def bucket_versioning_set(
    bucket: BucketArg,
    status: Annotated[str, typer.Argument(help="Enabled or Suspended")],
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Enable/suspend bucket versioning (reversible; prior state captured)."""
    from mcp_server.tools import bucket_writes as gov

    if dry_run:
        dry_run_print(operation="set_versioning",
                      api_call=f"PUT /{bucket}?versioning",
                      parameters={"status": status})
        return
    console.print_json(json.dumps(
        gov.set_versioning(bucket_name=bucket, status=status, target=target)))


@bucket_app.command("policy-set")
@cli_errors
def bucket_policy_set(
    bucket: BucketArg,
    policy_file: Annotated[
        str, typer.Option("--file", help="Path to the policy JSON document")
    ],
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Replace the bucket policy from a JSON file (reversible; prior captured)."""
    from pathlib import Path

    from mcp_server.tools import bucket_writes as gov

    policy_json = Path(policy_file).read_text("utf-8")
    if dry_run:
        # Through the governed call: set_bucket_policy refuses a policy that
        # denies s3:PutBucketPolicy to this tool's own key, so a preview must
        # report that rather than a green banner.
        dry_run_preview(
            gov.set_bucket_policy(bucket_name=bucket, policy_json=policy_json,
                                  dry_run=True, target=target),
            operation="set_bucket_policy", api_call=f"PUT /{bucket}?policy",
            parameters={"policyChars": len(policy_json)})
        return
    console.print_json(json.dumps(
        gov.set_bucket_policy(bucket_name=bucket, policy_json=policy_json, target=target)))


@bucket_app.command("lifecycle-set")
@cli_errors
def bucket_lifecycle_set(
    bucket: BucketArg,
    expire_days: Annotated[
        int | None, typer.Option("--expire-days", help="Expire current objects after N days")
    ] = None,
    noncurrent_days: Annotated[
        int | None,
        typer.Option("--noncurrent-days", help="Expire noncurrent versions after N days"),
    ] = None,
    abort_days: Annotated[
        int | None,
        typer.Option("--abort-days", help="Abort incomplete uploads after N days"),
    ] = None,
    prefix: Annotated[str, typer.Option("--prefix", help="Key prefix (empty = all)")] = "",
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Replace the bucket lifecycle rules (reversible; prior config captured)."""
    from mcp_server.tools import bucket_writes as gov

    if dry_run:
        dry_run_print(operation="set_lifecycle",
                      api_call=f"PUT /{bucket}?lifecycle",
                      parameters={"expire_days": expire_days,
                                  "noncurrent_days": noncurrent_days,
                                  "abort_days": abort_days, "prefix": prefix})
        return
    console.print_json(json.dumps(gov.set_lifecycle(
        bucket_name=bucket, expire_days=expire_days,
        noncurrent_expire_days=noncurrent_days, abort_incomplete_days=abort_days,
        prefix=prefix, target=target)))


@bucket_app.command("quota-set")
@cli_errors
def bucket_quota_set(
    bucket: BucketArg,
    size_bytes: Annotated[int, typer.Argument(help="Hard quota in bytes (0 clears)")],
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Set (>0) or clear (0) the bucket hard quota (reversible; prior captured)."""
    from mcp_server.tools import bucket_writes as gov

    if dry_run:
        dry_run_print(operation="set_bucket_quota",
                      api_call=f"PUT /minio/admin/v3/set-bucket-quota?bucket={bucket}",
                      parameters={"size_bytes": size_bytes})
        return
    console.print_json(json.dumps(
        gov.set_bucket_quota(bucket_name=bucket, size_bytes=size_bytes, target=target)))


@bucket_app.command("purge-uploads")
@cli_errors
def bucket_purge_uploads(
    bucket: BucketArg,
    older_than_days: Annotated[
        int, typer.Option("--older-than-days", help="Only abort uploads at least this old")
    ] = 7,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Abort abandoned multipart uploads — irreversible (dry-run + double confirm)."""
    from mcp_server.tools import bucket_writes as gov

    if dry_run:
        dry_run_print(operation="remove_incomplete_uploads",
                      api_call=f"DELETE /{bucket}/<object>?uploadId=<id> (per upload)",
                      parameters={"older_than_days": older_than_days})
        return
    double_confirm("purge incomplete uploads in", bucket)
    console.print_json(json.dumps(gov.remove_incomplete_uploads(
        bucket_name=bucket, older_than_days=older_than_days, target=target)))


@bucket_app.command("delete")
@cli_errors
def bucket_delete(
    bucket: BucketArg,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Delete an EMPTY bucket — irreversible (dry-run + double confirm)."""
    from mcp_server.tools import bucket_writes as gov

    if dry_run:
        dry_run_print(operation="bucket_delete", api_call=f"DELETE /{bucket}",
                      parameters={"note": "refused unless empty"})
        return
    double_confirm("delete bucket", bucket)
    console.print_json(json.dumps(gov.bucket_delete(bucket_name=bucket, target=target)))
