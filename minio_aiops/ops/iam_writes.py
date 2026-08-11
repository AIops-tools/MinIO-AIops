"""Guarded IAM writes (create / enable / disable / remove a user, attach or
detach policies).

**IAM is the surface where this line's "an operation must not destroy its own
reversibility" rule bites hardest.** Every write here can be pointed at the very
credential the tool authenticates with, and four of them would then take effect
and make their own undo impossible:

* disabling that user — the next call, including the undo, is rejected;
* removing it — same, and there is no undo at all;
* detaching its policies — it keeps authenticating and can no longer act;
* attaching a policy that replaces its rights.

So every user-targeting write runs :func:`guard_self_target` first, which is a
purely local comparison against ``target.access_key`` — no round trip, no unknown
identity case, and it therefore fires identically under ``dry_run``. Verified
against a live Keycloak in the sibling identity tool, this class of bug is not
hypothetical: a disable succeeded and the undo came back 403.

**Secrets never enter a return value.** The harness redacts ``sensitive_params``
in the audit row and in the undo record's ``orig_params``, but it stores the
*result* verbatim — so a tool that echoed a new user's secret key would write it
to the audit database in plaintext. ``create_user`` therefore reports only that
the account exists, and the caller keeps the secret it supplied.
"""

from __future__ import annotations

import re
from typing import Any

from minio_aiops.ops._util import s
from minio_aiops.ops.iam import _policies_of, _user_enabled

#: MinIO requires an access key of 3+ characters and a secret of 8+; the server
#: rejects shorter ones, and a local check gives the reason without a round trip.
_ACCESS_KEY_RE = re.compile(r"^[A-Za-z0-9._=+-]{3,128}$")
MIN_SECRET_LENGTH = 8


class SelfTargeted(ValueError):  # noqa: N818 — reads as a statement
    """Refused: the operation targets the credential this tool authenticates with."""


def check_access_key(value: Any) -> str:
    """Validate an agent-supplied access key; returns it or raises ValueError."""
    text = str(value or "")
    if not _ACCESS_KEY_RE.match(text):
        raise ValueError(
            f"Invalid access key {s(text, 64)!r}: 3-128 characters of letters, "
            f"digits, and . _ = + - only."
        )
    return text


def guard_self_target(conn: Any, access_key: str, operation: str) -> None:
    """Refuse an IAM write aimed at this tool's own credential. A pure local check.

    Shared by every write *and* by the MCP wrappers ahead of their ``dry_run``
    return, so a preview of a self-targeting call reports the refusal rather than
    a green ``wouldDisableUser``.
    """
    configured = getattr(getattr(conn, "target", None), "access_key", "")
    own = configured if isinstance(configured, str) else ""
    if own and str(access_key) == own:
        raise SelfTargeted(
            f"Refusing to {operation} '{s(access_key, 64)}': that is the access key "
            f"this tool authenticates with. The change would take effect and then "
            f"reject every following call — including the undo that reverses it — "
            f"leaving no way back in through this tool. Run it with a different "
            f"administrative credential (or `mc admin`), which keeps a route back."
        )


def create_user(conn: Any, access_key: str, secret_key: str) -> dict:
    """[WRITE][medium] Create or reset an IAM user. Reversible → remove the user.

    The secret is **not** echoed back: the harness stores a tool's result verbatim
    in the audit database, so returning it would persist a live credential in
    plaintext. The caller supplied it and keeps it.

    Note MinIO treats this as an upsert — an existing access key has its secret
    replaced. ``priorState.existed`` records which happened, so an undo that
    removes a user who already existed is not silently destructive.
    """
    check_access_key(access_key)
    if not isinstance(secret_key, str) or len(secret_key) < MIN_SECRET_LENGTH:
        raise ValueError(
            f"secret_key must be a string of at least {MIN_SECRET_LENGTH} "
            f"characters (MinIO rejects shorter secrets)."
        )
    existed = False
    prior_policies: list[str] = []
    try:
        info = conn.user_info(access_key)
        existed = bool(info)
        prior_policies = _policies_of(info)
    except Exception:  # noqa: BLE001 — "not found" is the expected case here
        existed = False
    conn.add_user(access_key, secret_key)
    result = {
        "action": "create_user",
        "accessKey": s(access_key, 128),
        "secretReturned": False,
        "priorState": {"existed": existed, "policies": prior_policies},
        "note": (
            "The secret is deliberately not returned: results are written to the "
            "audit database verbatim, so echoing it would persist a live "
            "credential in plaintext. A new user has NO policy yet and can do "
            "nothing until one is attached."
        ),
    }
    if existed:
        result["note"] += (
            " This access key already existed, so its secret was REPLACED — the "
            "undo removes the account entirely, which is not a restore. Recreate "
            "it with the original secret if that was not intended."
        )
    return result


def set_user_status(conn: Any, access_key: str, enabled: bool) -> dict:
    """[WRITE][medium] Enable or disable an IAM user. Reversible → prior status.

    Refused when it targets this tool's own credential: disabling that account
    makes the undo itself unauthorized.
    """
    check_access_key(access_key)
    guard_self_target(
        conn, access_key, "enable" if enabled else "disable the user"
    )
    prior = _user_enabled(conn.user_info(access_key))
    conn.set_user_status(access_key, bool(enabled))
    return {
        "action": "set_user_status",
        "accessKey": s(access_key, 128),
        "enabled": bool(enabled),
        "priorState": {"enabled": prior},
    }


def remove_user(conn: Any, access_key: str) -> dict:
    """[WRITE][high] Delete an IAM user. **Irreversible** — the secret is gone.

    ``priorState`` records the status and policy attachments for the audit trail,
    but no undo is recorded: MinIO does not store the secret in a recoverable
    form, so recreating the account requires a secret only its owner has. An undo
    descriptor here would be a token whose replay cannot restore what was lost.
    """
    check_access_key(access_key)
    guard_self_target(conn, access_key, "remove the user")
    prior: dict[str, Any] = {"accessKey": s(access_key, 128)}
    try:
        info = conn.user_info(access_key)
        prior["enabled"] = _user_enabled(info)
        prior["policies"] = _policies_of(info)
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        prior["readError"] = s(exc, 200)
    conn.remove_user(access_key)
    return {
        "action": "remove_user",
        "accessKey": s(access_key, 128),
        "reversible": False,
        "priorState": prior,
        "note": (
            "No undo was recorded and none is possible: MinIO keeps no recoverable "
            "copy of the secret, so the account cannot be restored — only recreated "
            "with a secret supplied again. The prior policy attachments are in "
            "priorState so the same rights can be rebuilt."
        ),
    }


def _normalize_policies(policies: Any) -> list[str]:
    if isinstance(policies, str):
        items = [p.strip() for p in policies.split(",")]
    elif isinstance(policies, (list, tuple)):
        items = [str(p).strip() for p in policies]
    else:
        raise ValueError("policies must be a policy name or a list of names.")
    names = [p for p in items if p]
    if not names:
        raise ValueError("policies must name at least one policy.")
    return names


def set_user_policies(
    conn: Any, access_key: str, policies: Any, attach: bool
) -> dict:
    """[WRITE][medium] Attach or detach canned policies for a user. Reversible.

    Detaching is refused for this tool's own credential — the account keeps
    authenticating and loses the rights the undo would need. Attaching to self is
    refused too: MinIO's canned policies replace rather than add to what the
    credential can do, so it can silently narrow this tool's own access.
    """
    check_access_key(access_key)
    names = _normalize_policies(policies)
    guard_self_target(
        conn,
        access_key,
        "attach policies to" if attach else "detach policies from",
    )
    prior = _policies_of(conn.user_info(access_key))
    conn.set_user_policies(access_key, names, bool(attach))
    return {
        "action": "attach_user_policy" if attach else "detach_user_policy",
        "accessKey": s(access_key, 128),
        "policies": [s(p, 128) for p in names],
        "priorState": {"policies": prior},
    }
