"""Bucket exposure audit (read-only) — flagship analysis #2.

Walks every bucket and scores its exposure surface: anonymous/public policy
statements (read and — far worse — write), missing default encryption,
versioning off, and no lifecycle. Produces **ranked** findings so an agent (or
a human) fixes the riskiest bucket first, each with cause + suggested action.

Scores and level cutoffs are named constants — the ranking policy is meant to
be read and tuned, not reverse-engineered.
"""

from __future__ import annotations

import json
from typing import Any

from minio_aiops.ops._util import s

# ── scoring policy ─────────────────────────────────────────────────────────
SCORE_PUBLIC_WRITE = 60  # anonymous principals may write/delete
SCORE_PUBLIC_READ = 40  # anonymous principals may read/list
SCORE_NO_ENCRYPTION = 20  # no default (SSE) encryption configured
SCORE_VERSIONING_OFF = 15  # deletes/overwrites are unrecoverable
SCORE_NO_LIFECYCLE = 10  # garbage accrues forever
LEVEL_HIGH = 50
LEVEL_MEDIUM = 25

_WRITE_ACTION_HINTS = ("put", "delete", "abort", "*")


def _anonymous_statements(policy_json: str | None) -> tuple[bool, bool]:
    """(public_read, public_write) from a bucket policy JSON string."""
    if not policy_json:
        return False, False
    try:
        policy = json.loads(policy_json)
    except (TypeError, ValueError):
        return False, False
    public_read = public_write = False
    statements = policy.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    for stmt in statements:
        if not isinstance(stmt, dict) or stmt.get("Effect") != "Allow":
            continue
        principal = stmt.get("Principal")
        anon = principal == "*" or (
            isinstance(principal, dict)
            and "*" in (lambda v: v if isinstance(v, list) else [v])(principal.get("AWS", []))
        )
        if not anon:
            continue
        actions = stmt.get("Action") or []
        if isinstance(actions, str):
            actions = [actions]
        for action in actions:
            lowered = str(action).lower()
            if any(hint in lowered for hint in _WRITE_ACTION_HINTS):
                public_write = True
            if "get" in lowered or "list" in lowered or lowered.endswith("*"):
                public_read = True
    return public_read, public_write


def _level(score: int) -> str:
    if score >= LEVEL_HIGH:
        return "high"
    if score >= LEVEL_MEDIUM:
        return "medium"
    return "low"


def audit_bucket(conn: Any, bucket: str) -> dict:
    """Score one bucket's exposure; resilient to per-probe failures."""
    reasons: list[dict] = []
    score = 0
    errors: list[str] = []

    try:
        policy_json = conn.get_bucket_policy(bucket)
        public_read, public_write = _anonymous_statements(policy_json)
        if public_write:
            score += SCORE_PUBLIC_WRITE
            reasons.append(
                {
                    "reason": "PUBLIC_WRITE_POLICY",
                    "cause": "The bucket policy allows anonymous principals to write "
                    "or delete objects.",
                    "suggestedAction": "Remove the anonymous Allow statement "
                    "(set_bucket_policy with a restricted policy, or "
                    "delete_bucket_policy) unless this is an intentional drop-box.",
                }
            )
        elif public_read:
            score += SCORE_PUBLIC_READ
            reasons.append(
                {
                    "reason": "PUBLIC_READ_POLICY",
                    "cause": "The bucket policy allows anonymous principals to read/list "
                    "objects.",
                    "suggestedAction": "Confirm the bucket is meant to be public "
                    "(static assets?); otherwise restrict it via set_bucket_policy.",
                }
            )
    except Exception as exc:  # noqa: BLE001 — collect, keep scoring the rest
        errors.append(f"policy: {s(exc, 120)}")

    try:
        if conn.get_bucket_encryption(bucket) is None:
            score += SCORE_NO_ENCRYPTION
            reasons.append(
                {
                    "reason": "NO_DEFAULT_ENCRYPTION",
                    "cause": "No default server-side encryption is configured.",
                    "suggestedAction": "Enable SSE-S3/SSE-KMS default encryption on the "
                    "bucket (server console/admin tooling) for at-rest protection.",
                }
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"encryption: {s(exc, 120)}")

    try:
        if conn.get_bucket_versioning(bucket) != "Enabled":
            score += SCORE_VERSIONING_OFF
            reasons.append(
                {
                    "reason": "VERSIONING_OFF",
                    "cause": "Versioning is off/suspended — overwrites and deletes are "
                    "unrecoverable.",
                    "suggestedAction": "Enable it with set_versioning (pair with a "
                    "noncurrent-expiry lifecycle rule to cap version growth).",
                }
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"versioning: {s(exc, 120)}")

    try:
        if conn.get_bucket_lifecycle(bucket) is None:
            score += SCORE_NO_LIFECYCLE
            reasons.append(
                {
                    "reason": "NO_LIFECYCLE",
                    "cause": "No lifecycle rules — old versions and abandoned multipart "
                    "uploads accumulate forever.",
                    "suggestedAction": "Add expiry rules with set_lifecycle (see "
                    "lifecycle_gap_analysis for what would be reclaimed).",
                }
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"lifecycle: {s(exc, 120)}")

    out = {
        "bucket": s(bucket),
        "riskScore": score,
        "riskLevel": _level(score),
        "reasons": reasons,
    }
    if errors:
        out["errors"] = errors
    return out


def bucket_exposure_audit(conn: Any, limit: int = 100) -> dict:
    """[READ] Ranked exposure findings across all buckets (riskiest first)."""
    try:
        buckets = conn.list_buckets()
    except Exception as exc:  # noqa: BLE001 — an audit must survive a broken target
        return {"error": s(exc, 200)}
    rows = [audit_bucket(conn, b["name"]) for b in buckets[:limit]]
    rows.sort(key=lambda r: -r["riskScore"])
    return {
        "bucketsAudited": len(rows),
        "atRisk": sum(1 for r in rows if r["riskScore"] > 0),
        "highRisk": sum(1 for r in rows if r["riskLevel"] == "high"),
        "findings": [r for r in rows if r["riskScore"] > 0],
    }
