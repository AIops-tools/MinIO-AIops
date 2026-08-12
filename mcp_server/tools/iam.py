"""IAM MCP tools: users, groups, policies, the exposure analysis, and the
guarded user writes.

``create_user`` declares ``sensitive_params=["secret_key"]`` so the secret is
redacted in the audit row and in the undo record's ``orig_params``. Results are
stored verbatim by the harness, so no tool here returns a secret at all.

Every user-targeting write runs ``guard_self_target`` ahead of its ``dry_run``
return: a preview of a call aimed at this tool's own credential reports the
refusal instead of a green banner.
"""

from typing import Any, Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import iam as ops
from minio_aiops.ops import iam_writes as writes


def _has_prior(result: Any) -> bool:
    return isinstance(result, dict) and "priorState" in result


def _create_user_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of create_user: remove the account it created.

    Only when the account did **not** exist before. MinIO's user_add is an upsert,
    so for a pre-existing key this call replaced a secret — removing the account
    would destroy it rather than restore it, and no descriptor can put the old
    secret back.
    """
    if not _has_prior(result):
        return None
    existed = (result.get("priorState") or {}).get("existed")
    if existed is None or existed:
        # None = the existence probe failed, so we do not know. Removing an
        # account that turns out to have existed destroys a credential that
        # remove_user cannot restore, so an unknown prior state suppresses the
        # undo exactly as a known-existing one does.
        return None
    return {
        "tool": "remove_user",
        "params": {"access_key": params.get("access_key")},
        "note": (
            "Remove the account this call created. (Skipped when the access key "
            "already existed: that call replaced a secret, which no undo can "
            "restore.)"
        ),
    }


def _user_status_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of set_user_status: restore the captured prior status."""
    if not _has_prior(result):
        return None
    prior = (result.get("priorState") or {}).get("enabled")
    if prior is None:
        return None  # the server did not say; nothing honest to replay
    return {
        "tool": "set_user_status",
        "params": {"access_key": params.get("access_key"), "enabled": bool(prior)},
        "note": "Restore the user's prior enabled/disabled state.",
    }


def _policy_attach_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of attach_user_policy: detach exactly the policies just attached."""
    if not _has_prior(result):
        return None
    attached = result.get("policies") or []
    if not attached:
        return None
    return {
        "tool": "detach_user_policy",
        "params": {"access_key": params.get("access_key"), "policies": attached},
        "note": (
            "Detach the policies this call attached, leaving any the user already "
            "had in place."
        ),
    }


def _policy_detach_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of detach_user_policy: re-attach exactly what was detached."""
    if not _has_prior(result):
        return None
    detached = result.get("policies") or []
    if not detached:
        return None
    return {
        "tool": "attach_user_policy",
        "params": {"access_key": params.get("access_key"), "policies": detached},
        "note": "Re-attach the policies this call detached.",
    }


# ── reads ────────────────────────────────────────────────────────────────


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def iam_users(limit: int = 200, target: Optional[str] = None) -> dict:
    """[READ][risk=low] IAM users with status, attached policies, group membership.

    An empty list carries a note: the root credential (MINIO_ROOT_USER) is not an
    IAM user and never appears, so a root-only deployment legitimately reports no
    users — not a failed probe.

    Args:
        limit: Maximum users to return (envelope reports truncation).
        target: MinIO target name from config; omit for the default.
    """
    return ops.list_users(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def iam_groups(limit: int = 200, target: Optional[str] = None) -> dict:
    """[READ][risk=low] IAM groups with members and attached policies.

    Groups whose read fails are listed in groupErrors rather than dropped.

    Args:
        limit: Maximum groups to return (envelope reports truncation).
        target: MinIO target name from config; omit for the default.
    """
    return ops.list_groups(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def iam_policies(limit: int = 200, target: Optional[str] = None) -> dict:
    """[READ][risk=low] Canned policy names defined on the deployment.

    Args:
        limit: Maximum policy names to return (envelope reports truncation).
        target: MinIO target name from config; omit for the default.
    """
    return ops.list_policies(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def diagnose_iam_exposure(limit: int = 50, target: Optional[str] = None) -> dict:
    """[READ][risk=low] Ranked IAM findings: admin sprawl and unusable accounts.

    Resolves group-inherited policies first, so a correctly group-managed user is
    not reported as having no permissions. Findings: NO_EFFECTIVE_POLICY (MinIO
    denies by default, so the account can do nothing — a broken account that looks
    identical to a working one in any name-and-status listing), FULL_ADMIN_POLICY,
    BROAD_DATA_POLICY, DISABLED_BUT_PRIVILEGED. Sorted worst-first with rank.

    Args:
        limit: Maximum findings to return (envelope reports truncation).
        target: MinIO target name from config; omit for the default.
    """
    return ops.diagnose_iam_exposure(_get_connection(target), limit=limit)


# ── writes ───────────────────────────────────────────────────────────────


@mcp.tool()
@governed_tool(
    risk_level="medium", undo=_create_user_undo, sensitive_params=["secret_key"]
)
@tool_errors("dict")
def create_user(access_key: str, secret_key: str, dry_run: bool = False,
                target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Create or reset an IAM user. Reversible → remove.

    The secret is redacted in the audit row and is NOT returned: the harness
    stores a result verbatim, so echoing it would persist a live credential in
    plaintext. A new user has no policy and can do nothing until one is attached.

    MinIO treats this as an upsert — an existing access key has its secret
    REPLACED, and no undo can restore the old one, so the undo is recorded only
    for a genuinely new account.

    Args:
        access_key: The new user's access key (3-128 chars, letters/digits/._=+-).
        secret_key: The new user's secret (8+ chars). Redacted in the audit log.
        dry_run: If True, preview without creating.
        target: MinIO target name from config; omit for the default.
    """
    if dry_run:
        return {"dryRun": True,
                "wouldCreateUser": {"accessKey": access_key, "secretProvided": True}}
    return writes.create_user(_get_connection(target), access_key, secret_key)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_user_status_undo)
@tool_errors("dict")
def set_user_status(access_key: str, enabled: bool, dry_run: bool = False,
                    target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Enable or disable an IAM user. Reversible → prior status.

    Refused when it targets the access key this tool authenticates with: the
    change would take effect and then reject every following call, the undo
    included. Enforced under dry_run too.

    Args:
        access_key: The user's access key (from iam_users).
        enabled: True enables the account, False disables it.
        dry_run: If True, preview without changing the status.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    writes.guard_self_target(conn, access_key,
                             "enable" if enabled else "disable the user")
    if dry_run:
        return {"dryRun": True,
                "wouldSetUserStatus": {"accessKey": access_key, "enabled": enabled}}
    return writes.set_user_status(conn, access_key, enabled)


@mcp.tool()
@governed_tool(risk_level="high")
@tool_errors("dict")
def remove_user(access_key: str, dry_run: bool = False,
                target: Optional[str] = None) -> dict:
    """[WRITE][risk=high] Delete an IAM user. IRREVERSIBLE — no undo is recorded.

    MinIO keeps no recoverable copy of the secret, so the account cannot be
    restored, only recreated with a secret supplied again. priorState captures the
    status and policy attachments so the same rights can be rebuilt. Refused when
    it targets this tool's own credential.

    Args:
        access_key: The user's access key (from iam_users).
        dry_run: If True, run the guards and report without deleting.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    writes.guard_self_target(conn, access_key, "remove the user")
    if dry_run:
        return {"dryRun": True,
                "wouldRemoveUser": {"accessKey": access_key},
                "reversible": False}
    return writes.remove_user(conn, access_key)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_policy_attach_undo)
@tool_errors("dict")
def attach_user_policy(access_key: str, policies: list[str], dry_run: bool = False,
                       target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Attach canned policies to a user. Reversible → detach.

    Refused for this tool's own credential: MinIO's canned policies replace what a
    credential may do rather than only adding to it, so this can silently narrow
    the tool's own access.

    Args:
        access_key: The user's access key (from iam_users).
        policies: Policy names to attach (from iam_policies).
        dry_run: If True, preview without attaching.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    writes.guard_self_target(conn, access_key, "attach policies to")
    if dry_run:
        return {"dryRun": True,
                "wouldAttachPolicies": {"accessKey": access_key, "policies": policies}}
    return writes.set_user_policies(conn, access_key, policies, attach=True)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_policy_detach_undo)
@tool_errors("dict")
def detach_user_policy(access_key: str, policies: list[str], dry_run: bool = False,
                       target: Optional[str] = None) -> dict:
    """[WRITE][risk=medium] Detach canned policies from a user. Reversible → attach.

    Refused for this tool's own credential: the account keeps authenticating and
    loses exactly the rights the undo would need to put them back.

    Args:
        access_key: The user's access key (from iam_users).
        policies: Policy names to detach (from iam_users).
        dry_run: If True, preview without detaching.
        target: MinIO target name from config; omit for the default.
    """
    conn = _get_connection(target)
    writes.guard_self_target(conn, access_key, "detach policies from")
    if dry_run:
        return {"dryRun": True,
                "wouldDetachPolicies": {"accessKey": access_key, "policies": policies}}
    return writes.set_user_policies(conn, access_key, policies, attach=False)
