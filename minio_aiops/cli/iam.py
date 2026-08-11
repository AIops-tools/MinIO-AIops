"""``minio-aiops iam`` — IAM reads + guarded user writes.

Real writes are delegated to the ``@governed_tool``-wrapped twins in
``mcp_server.tools.iam`` so CLI writes are audited and undo-recorded on the SAME
governance path as the MCP tools.

The secret for ``user-create`` is read from an environment variable, never a
command-line argument: an argv value is visible in ``ps`` output and lands in the
shell history file of whoever ran it.
"""

from __future__ import annotations

import json
import os
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

iam_app = typer.Typer(
    name="iam",
    help="IAM: users/groups/policies, exposure findings, guarded user writes.",
    no_args_is_help=True,
)

#: Where ``user-create`` reads the new user's secret from.
SECRET_ENV = "MINIO_NEW_USER_SECRET"

KeyArg = Annotated[str, typer.Argument(help="Access key (from 'iam users')")]
PolicyOption = Annotated[
    list[str], typer.Option("--policy", help="Policy name (repeatable)")
]


# ── reads ────────────────────────────────────────────────────────────────


@iam_app.command("users")
@cli_errors
def iam_users(
    limit: Annotated[int, typer.Option("--limit", help="Max users")] = 200,
    target: TargetOption = None,
) -> None:
    """List IAM users with status, policies, and group membership."""
    from minio_aiops.ops import iam as ops

    conn, _ = get_connection(target)
    result = ops.list_users(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


@iam_app.command("groups")
@cli_errors
def iam_groups(
    limit: Annotated[int, typer.Option("--limit", help="Max groups")] = 200,
    target: TargetOption = None,
) -> None:
    """List IAM groups with members and attached policies."""
    from minio_aiops.ops import iam as ops

    conn, _ = get_connection(target)
    result = ops.list_groups(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


@iam_app.command("policies")
@cli_errors
def iam_policies(
    limit: Annotated[int, typer.Option("--limit", help="Max policy names")] = 200,
    target: TargetOption = None,
) -> None:
    """List the canned policy names defined on the deployment."""
    from minio_aiops.ops import iam as ops

    conn, _ = get_connection(target)
    result = ops.list_policies(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


@iam_app.command("audit")
@cli_errors
def iam_audit(
    limit: Annotated[int, typer.Option("--limit", help="Max findings")] = 50,
    target: TargetOption = None,
) -> None:
    """Ranked IAM findings: admin sprawl and accounts that cannot do anything."""
    from minio_aiops.ops import iam as ops

    conn, _ = get_connection(target)
    result = ops.diagnose_iam_exposure(conn, limit=limit)
    console.print_json(json.dumps(result))
    truncation_note(result)


# ── writes (delegated to the governed twins) ─────────────────────────────


@iam_app.command("user-create")
@cli_errors
def iam_user_create(
    access_key: KeyArg,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Create or reset an IAM user; the secret comes from $MINIO_NEW_USER_SECRET.

    Deliberately not a command-line option: an argv value shows up in `ps` and in
    the shell history of whoever ran it. The secret is redacted in the audit row
    and is never printed back.
    """
    from mcp_server.tools import iam as gov

    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        console.print(
            f"[red]Error:[/] set {SECRET_ENV} to the new user's secret first, e.g.\n"
            f"  read -rs {SECRET_ENV} && export {SECRET_ENV}\n"
            f"It is not accepted as an argument because argv is visible in `ps` "
            f"output and in shell history."
        )
        raise typer.Exit(1)
    if dry_run:
        dry_run_preview(
            gov.create_user(access_key=access_key, secret_key=secret,
                            dry_run=True, target=target),
            operation="create_user", api_call="PUT /minio/admin/v3/add-user",
            parameters={"accessKey": access_key, "secretProvided": True})
        return
    console.print_json(json.dumps(checked(
        gov.create_user(access_key=access_key, secret_key=secret, target=target))))


@iam_app.command("user-status")
@cli_errors
def iam_user_status(
    access_key: KeyArg,
    state: Annotated[str, typer.Argument(help="enable or disable")],
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Enable or disable an IAM user (reversible; refused for our own key)."""
    from mcp_server.tools import iam as gov

    normalized = str(state).strip().lower()
    if normalized not in ("enable", "disable"):
        console.print(f"[red]Error:[/] state must be 'enable' or 'disable' (got {state!r}).")
        raise typer.Exit(1)
    enabled = normalized == "enable"
    if dry_run:
        # Through the governed twin, which refuses a self-targeting call.
        dry_run_preview(
            gov.set_user_status(access_key=access_key, enabled=enabled,
                                dry_run=True, target=target),
            operation="set_user_status",
            api_call="PUT /minio/admin/v3/set-user-status",
            parameters={"accessKey": access_key, "enabled": enabled})
        return
    console.print_json(json.dumps(checked(
        gov.set_user_status(access_key=access_key, enabled=enabled, target=target))))


@iam_app.command("user-remove")
@cli_errors
def iam_user_remove(
    access_key: KeyArg,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Delete an IAM user — IRREVERSIBLE (dry-run + double confirm)."""
    from mcp_server.tools import iam as gov

    if dry_run:
        dry_run_preview(
            gov.remove_user(access_key=access_key, dry_run=True, target=target),
            operation="remove_user", api_call="DELETE /minio/admin/v3/remove-user",
            parameters={"accessKey": access_key, "reversible": False})
        return
    console.print(
        "[bold yellow]No undo exists: MinIO keeps no recoverable copy of the "
        "secret, so the account can only be recreated with a secret supplied "
        "again. The prior policy attachments are captured in the audit trail.[/]"
    )
    double_confirm("remove IAM user", access_key)
    console.print_json(json.dumps(checked(
        gov.remove_user(access_key=access_key, target=target))))


@iam_app.command("policy-attach")
@cli_errors
def iam_policy_attach(
    access_key: KeyArg,
    policy: PolicyOption,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Attach canned policies to a user (reversible; refused for our own key)."""
    from mcp_server.tools import iam as gov

    if dry_run:
        dry_run_preview(
            gov.attach_user_policy(access_key=access_key, policies=list(policy),
                                   dry_run=True, target=target),
            operation="attach_user_policy",
            api_call="POST /minio/admin/v3/idp/builtin/policy/attach",
            parameters={"accessKey": access_key, "policies": list(policy)})
        return
    console.print_json(json.dumps(checked(
        gov.attach_user_policy(access_key=access_key, policies=list(policy),
                               target=target))))


@iam_app.command("policy-detach")
@cli_errors
def iam_policy_detach(
    access_key: KeyArg,
    policy: PolicyOption,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Detach canned policies from a user (reversible; refused for our own key)."""
    from mcp_server.tools import iam as gov

    if dry_run:
        dry_run_preview(
            gov.detach_user_policy(access_key=access_key, policies=list(policy),
                                   dry_run=True, target=target),
            operation="detach_user_policy",
            api_call="POST /minio/admin/v3/idp/builtin/policy/detach",
            parameters={"accessKey": access_key, "policies": list(policy)})
        return
    console.print_json(json.dumps(checked(
        gov.detach_user_policy(access_key=access_key, policies=list(policy),
                               target=target))))
