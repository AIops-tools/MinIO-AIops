"""IAM reads + the exposure analysis (users, groups, policy attachment).

Two facts shape everything here.

**The root credential is not an IAM user.** ``MINIO_ROOT_USER`` never appears in
``user_list``, so an empty user list means "no IAM users are defined", not "no
accounts exist" — and a deployment driven entirely by root has *no* IAM surface
to inspect. Every read says which case it is rather than returning an empty list
that reads as "nothing to see".

**A user with no policy can do nothing.** MinIO denies by default, so an account
with neither a directly attached policy nor one via a group is not a lax
configuration, it is a broken one — and it looks identical to a healthy account
in every listing that only shows names and status. That is the flagship finding.

:func:`diagnose_iam_exposure` is sorted worst-first with an explicit ``rank``,
because it genuinely ranks by risk score.
"""

from __future__ import annotations

from typing import Any

from minio_aiops.ops._util import opt_s, s

#: Canned MinIO policies that confer administrative power. `consoleAdmin` is the
#: MinIO-shipped superuser policy; the others are its documented siblings.
ADMIN_POLICIES = frozenset({"consoleadmin", "diagnostics", "readwrite"})
#: Only `consoleAdmin` is full control; `readwrite` is data-plane wide but not
#: admin, so they are scored differently rather than lumped together.
FULL_ADMIN_POLICIES = frozenset({"consoleadmin"})

SCORE_FULL_ADMIN = 60
SCORE_BROAD_DATA_ACCESS = 25
SCORE_NO_POLICY = 40  # cannot do anything: a broken account, not a lax one
SCORE_DISABLED_WITH_ADMIN = 15
LEVEL_HIGH = 50
LEVEL_MEDIUM = 25


def _policies_of(row: Any) -> list[str]:
    """The policy names on a user/group record, across the shapes MinIO returns."""
    if not isinstance(row, dict):
        return []
    raw = row.get("policyName") or row.get("policy") or row.get("policies") or ""
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    return []


def _user_enabled(row: Any) -> bool | None:
    """A user's enabled state, or None when the record does not say."""
    if not isinstance(row, dict):
        return None
    status = str(row.get("status") or row.get("userStatus") or "").strip().lower()
    if status in ("enabled", "on"):
        return True
    if status in ("disabled", "off"):
        return False
    return None


def _level(score: int) -> str:
    if score >= LEVEL_HIGH:
        return "high"
    if score >= LEVEL_MEDIUM:
        return "medium"
    return "low"


def list_users(conn: Any, limit: int = 200) -> dict:
    """[READ] IAM users with status and attached policies.

    An empty list is reported with a note: the root credential is not an IAM user,
    so "no users" is a real and common state that must not read as a failed probe.
    """
    requested = max(1, int(limit))
    raw = conn.list_users()
    items = list(raw.items()) if isinstance(raw, dict) else []
    truncated = len(items) > requested
    users = [
        {
            "accessKey": s(key, 128),
            "enabled": _user_enabled(value),
            "policies": [s(p, 128) for p in _policies_of(value)],
            "memberOf": [s(g, 128) for g in (value.get("memberOf") or [])]
            if isinstance(value, dict)
            else [],
        }
        for key, value in items[:requested]
    ]
    out = {
        "users": users,
        "returned": len(users),
        "limit": requested,
        "truncated": truncated,
    }
    if not items:
        out["note"] = (
            "No IAM users are defined. The root credential (MINIO_ROOT_USER) is "
            "not an IAM user and never appears here, so a deployment driven "
            "entirely by root reports an empty list — that is a real state, not a "
            "failed probe."
        )
    return out


def list_groups(conn: Any, limit: int = 200) -> dict:
    """[READ] IAM groups with their members and attached policies."""
    requested = max(1, int(limit))
    names = conn.list_groups() or []
    truncated = len(names) > requested
    groups = []
    errors = []
    for name in names[:requested]:
        try:
            info = conn.group_info(str(name))
        except Exception as exc:  # noqa: BLE001 — per-group, reported not swallowed
            errors.append({"group": s(name, 128), "error": s(exc, 200)})
            continue
        groups.append(
            {
                "group": s(name, 128),
                "members": [s(m, 128) for m in (info.get("members") or [])],
                "policies": [s(p, 128) for p in _policies_of(info)],
                "status": opt_s(info.get("status")),
            }
        )
    out = {
        "groups": groups,
        "returned": len(groups),
        "limit": requested,
        "truncated": truncated,
    }
    if errors:
        out["groupErrors"] = errors
        out["note"] = (
            f"{len(errors)} group(s) could not be read and are absent from the "
            f"list — see groupErrors. A short list does not mean few groups."
        )
    return out


def list_policies(conn: Any, limit: int = 200) -> dict:
    """[READ] Canned policy names defined on the deployment."""
    requested = max(1, int(limit))
    raw = conn.list_policies()
    names = sorted(raw.keys()) if isinstance(raw, dict) else sorted(map(str, raw or []))
    truncated = len(names) > requested
    return {
        "policies": [s(n, 128) for n in names[:requested]],
        "returned": min(len(names), requested),
        "limit": requested,
        "truncated": truncated,
    }


def _effective_policies(user: dict, groups_by_name: dict[str, dict]) -> list[str]:
    """A user's own policies plus any inherited from the groups they belong to.

    Judging "this account has no permissions" on direct attachment alone would
    flag every correctly group-managed user, which is the normal way to run MinIO.
    """
    effective = list(user.get("policies") or [])
    for group in user.get("memberOf") or []:
        effective.extend((groups_by_name.get(group) or {}).get("policies") or [])
    return effective


def diagnose_iam_exposure(conn: Any, limit: int = 50) -> dict:
    """[READ] Ranked IAM findings: admin sprawl and accounts that cannot work.

    Scored worst-first with an explicit ``rank``. Group-inherited policies are
    resolved first, so a correctly group-managed user is not reported as having
    no permissions.
    """
    requested = max(1, int(limit))
    users = list_users(conn, limit=1000)
    groups = list_groups(conn, limit=1000)
    groups_by_name = {g["group"]: g for g in groups["groups"]}

    findings: list[dict] = []
    for user in users["users"]:
        access_key = user["accessKey"]
        effective = _effective_policies(user, groups_by_name)
        lowered = {p.lower() for p in effective}
        reasons: list[dict] = []
        score = 0

        if not effective:
            score += SCORE_NO_POLICY
            reasons.append(
                {
                    "code": "NO_EFFECTIVE_POLICY",
                    "cause": (
                        "The account has no policy attached directly or through a "
                        "group. MinIO denies by default, so this account can do "
                        "nothing at all — a broken account, not a lax one, and it "
                        "looks identical to a working one in any listing that shows "
                        "only name and status."
                    ),
                    "action": (
                        "Attach a policy (attach_user_policy) or add the user to a "
                        "group that has one — or remove the account if it is a "
                        "leftover."
                    ),
                }
            )
        if lowered & FULL_ADMIN_POLICIES:
            score += SCORE_FULL_ADMIN
            reasons.append(
                {
                    "code": "FULL_ADMIN_POLICY",
                    "cause": (
                        f"Effective policies include consoleAdmin, which is full "
                        f"administrative control of the deployment "
                        f"(policies: {', '.join(sorted(effective))})."
                    ),
                    "action": (
                        "Confirm this account needs administrative rights; if it is "
                        "an application credential, attach a bucket-scoped policy "
                        "instead."
                    ),
                }
            )
        elif lowered & ADMIN_POLICIES:
            score += SCORE_BROAD_DATA_ACCESS
            reasons.append(
                {
                    "code": "BROAD_DATA_POLICY",
                    "cause": (
                        f"Effective policies include a deployment-wide data policy "
                        f"({', '.join(sorted(effective))}) — every bucket, not a "
                        f"scoped subset."
                    ),
                    "action": "Scope the policy to the buckets this account uses.",
                }
            )
        if user["enabled"] is False and (lowered & ADMIN_POLICIES):
            score += SCORE_DISABLED_WITH_ADMIN
            reasons.append(
                {
                    "code": "DISABLED_BUT_PRIVILEGED",
                    "cause": (
                        "The account is disabled but still carries privileged "
                        "policies, so re-enabling it silently restores them."
                    ),
                    "action": (
                        "Detach the policies as well, or remove the account "
                        "outright."
                    ),
                }
            )

        if reasons:
            findings.append(
                {
                    "accessKey": access_key,
                    "enabled": user["enabled"],
                    "effectivePolicies": sorted(effective),
                    "riskScore": score,
                    "riskLevel": _level(score),
                    "reasons": reasons,
                }
            )

    orphan_groups = [
        g["group"] for g in groups["groups"] if not g["members"] and g["policies"]
    ]

    findings.sort(key=lambda f: f["riskScore"], reverse=True)
    truncated = len(findings) > requested
    shown = findings[:requested]
    for index, finding in enumerate(shown, start=1):
        finding["rank"] = index

    out = {
        "usersScanned": users["returned"],
        "groupsScanned": groups["returned"],
        "findings": shown,
        "returned": len(shown),
        "limit": requested,
        "truncated": truncated,
    }
    if orphan_groups:
        out["groupsWithPoliciesButNoMembers"] = [s(g, 128) for g in orphan_groups]
    notes = []
    if users["returned"] == 0:
        notes.append(
            "No IAM users exist, so there is nothing to score. The root credential "
            "is not an IAM user; a root-only deployment has no IAM surface."
        )
    if users.get("truncated") or groups.get("truncated"):
        notes.append(
            "The user or group listing was capped, so some accounts were not "
            "scored."
        )
    for key in ("note",):
        if groups.get(key):
            notes.append(groups[key])
    if notes:
        out["note"] = " ".join(notes)
    return out
