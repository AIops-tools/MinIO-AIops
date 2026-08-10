"""Guarded object-lock (WORM) writes: bucket creation, default retention,
per-object retention, legal hold.

The honesty problem this module exists to solve: **three of these four writes
are reversible and one is not, and the irreversible one looks exactly like the
others from a call site.**

* ``create_bucket`` — reversible while the bucket is still empty.
* ``set_default_retention`` — reversible: the rule is restored on undo. Objects
  written while it was in force keep the retention they were given, and if that
  was COMPLIANCE they keep it permanently. The rule comes back; the consequences
  do not.
* ``set_legal_hold`` — reversible by design; a hold is meant to be lifted.
* ``set_object_retention`` — **irreversible, and not merely by policy.** S3
  forbids shortening or removing retention without
  ``x-amz-bypass-governance-retention``, and the ``minio`` SDK never sends that
  header (verified in its source), so no credential can undo it through this
  tool. COMPLIANCE mode cannot be shortened by anyone at all, bypass header or
  not, root or not, until the date passes.

So ``set_object_retention`` records ``priorState`` and **no undo token**. An
undo token here would be a token whose replay is guaranteed to fail while the
audit row claims the write was reversible — worse than saying plainly that it
is not. It additionally requires ``acknowledge_irreversible=True`` for
COMPLIANCE, and refuses any call that would weaken existing retention, so the
refusal arrives before the round trip and shows up under ``dry_run`` too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from minio_aiops.ops._util import check_bucket_name, check_object_name, opt_s, s
from minio_aiops.ops.objectlock import _days_until, _retention_days

RETENTION_MODES = ("GOVERNANCE", "COMPLIANCE")
DURATION_UNITS = ("Days", "Years")

#: The bypass header S3 requires to shorten or remove retention. Named here
#: because every refusal message below turns on this one fact.
BYPASS_HEADER = "x-amz-bypass-governance-retention"

#: The out-of-band remedy quoted in refusal messages. Deliberately without a
#: ``--bypass`` flag: ``mc retention clear`` does not define one (measured — it
#: errors with "flag provided but not defined"), yet it does clear GOVERNANCE
#: retention for a privileged credential, and is refused for COMPLIANCE. An
#: error message that hands the operator a flag the command rejects sends them
#: to debug the wrong thing.
CLEAR_COMMAND = "mc retention clear <alias>/<bucket>/<key>"


class ObjectLockUnavailable(ValueError):  # noqa: N818 — reads as a statement
    """Refused: the bucket has no object lock, so retention is not expressible."""


class IrreversibleRetention(ValueError):  # noqa: N818 — reads as a statement
    """Refused: the write cannot be undone and was not explicitly acknowledged."""


class RetentionWeakened(ValueError):  # noqa: N818 — reads as a statement
    """Refused: the write would shorten or downgrade retention already in force."""


def _normalize_mode(mode: Any) -> str:
    normalized = str(mode or "").strip().upper()
    if normalized not in RETENTION_MODES:
        raise ValueError(
            f"mode must be one of {RETENTION_MODES} (got {s(mode, 40)!r}). "
            f"GOVERNANCE can be lifted by a caller with "
            f"s3:BypassGovernanceRetention; COMPLIANCE can be lifted by nobody "
            f"until the retain-until date passes."
        )
    return normalized


def _require_lock(conn: Any, bucket: str) -> dict:
    """Return the bucket's lock config, or raise the teaching refusal."""
    lock = conn.get_object_lock_config(bucket)
    if lock is None:
        raise ObjectLockUnavailable(
            f"Bucket '{s(bucket, 80)}' does not have object lock enabled, so "
            f"retention and legal holds cannot be set on its objects. S3 accepts "
            f"the object-lock flag only at bucket creation, so this cannot be "
            f"fixed in place: create a new bucket with "
            f"bucket_create(object_lock=True) and migrate the objects into it."
        )
    return lock


def _retain_until(days: int) -> datetime:
    """The retain-until instant for a duration in days, in UTC."""
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError(f"days must be a positive integer (got {days!r}).")
    return datetime.now(tz=UTC) + timedelta(days=days)


# ── bucket creation (the only way to get object lock at all) ───────────────


def create_bucket(conn: Any, bucket: str, object_lock: bool = False) -> dict:
    """[WRITE][medium] Create a bucket. Reversible → delete while still empty.

    ``object_lock=True`` is the only route to a WORM-capable bucket: S3 has no
    call that enables object lock on an existing one. The server force-enables
    versioning as a consequence, which the result reports as observed rather
    than assumed.
    """
    check_bucket_name(bucket)
    conn.make_bucket(bucket, object_lock=bool(object_lock))
    observed_lock = None
    observed_versioning = None
    read_back_error = None
    try:
        observed_lock = conn.get_object_lock_config(bucket) is not None
        observed_versioning = conn.get_bucket_versioning(bucket)
    except Exception as exc:  # noqa: BLE001 — the bucket exists; read-back may not
        # Not swallowed: a null objectLockEnabled would otherwise be
        # indistinguishable from "the server said no lock", which is the whole
        # question this call exists to answer, and object lock cannot be added
        # afterwards if the answer turns out to be no.
        read_back_error = s(exc, 200)
    result = {
        "action": "create_bucket",
        "bucket": s(bucket),
        "requestedObjectLock": bool(object_lock),
        "objectLockEnabled": observed_lock,
        "versioning": observed_versioning,
        "priorState": {"existed": False},
        "note": (
            "Undo deletes the bucket, and only while it is still empty — the "
            "delete is refused once anything has been written into it."
        ),
    }
    if read_back_error:
        result["readBackError"] = read_back_error
        result["note"] += (
            " The bucket was created, but reading its lock/versioning state back "
            "failed, so objectLockEnabled is unknown rather than false — check it "
            "with bucket_lock_config before relying on WORM here."
        )
    return result


# ── bucket default retention ───────────────────────────────────────────────


def guard_set_default_retention(conn: Any, bucket: str, mode: Any) -> None:
    """Raise what ``set_default_retention`` would raise, without writing.

    Called by the write *and* by the MCP wrapper ahead of its ``dry_run``
    return, so a preview against a bucket with no object lock reports the
    refusal instead of a green ``wouldSetDefaultRetention``.
    """
    check_bucket_name(bucket)
    _normalize_mode(mode)
    _require_lock(conn, bucket)


def set_default_retention(
    conn: Any,
    bucket: str,
    mode: str,
    *,
    days: int | None = None,
    years: int | None = None,
) -> dict:
    """[WRITE][high] Set the bucket's DEFAULT retention. Reversible → prior rule.

    The default applies to objects written from now on; objects already stored
    are untouched. Undo restores the previous rule (or clears it), but any object
    written in the meantime keeps the retention it was given — and a COMPLIANCE
    default means those objects are undeletable until their own dates pass, undo
    or no undo.
    """
    normalized = _normalize_mode(mode)
    guard_set_default_retention(conn, bucket, normalized)
    if (days is None) == (years is None):
        raise ValueError("Pass exactly one of days or years.")
    duration = days if days is not None else years
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1:
        raise ValueError(f"Retention duration must be a positive integer (got {duration!r}).")
    unit = "Days" if days is not None else "Years"

    prior = conn.get_object_lock_config(bucket) or {}
    prior_default = prior.get("defaultRetention")
    conn.set_object_lock_config(bucket, normalized, duration, unit)
    return {
        "action": "set_default_retention",
        "bucket": s(bucket),
        "applied": {"mode": normalized, "days": days, "years": years},
        "priorState": {"defaultRetention": prior_default},
        "note": (
            "Applies to objects written from now on. Undo restores the prior "
            "default rule; objects written while this rule was in force keep "
            "their own retention"
            + (
                " — and COMPLIANCE retention cannot be shortened by anyone, so "
                "those objects stay undeletable until their dates pass."
                if normalized == "COMPLIANCE"
                else "."
            )
        ),
    }


def clear_default_retention(conn: Any, bucket: str) -> dict:
    """[WRITE][medium] Remove the DEFAULT retention rule. Reversible → prior rule.

    This clears the rule only. Object lock stays enabled on the bucket (S3 has
    no call that disables it) and objects already carrying retention keep it —
    so this makes *future* uploads unprotected and changes nothing about what is
    already stored.
    """
    check_bucket_name(bucket)
    _require_lock(conn, bucket)
    prior = conn.get_object_lock_config(bucket) or {}
    prior_default = prior.get("defaultRetention")
    conn.set_object_lock_config(bucket, None, None, None)
    return {
        "action": "clear_default_retention",
        "bucket": s(bucket),
        "priorState": {"defaultRetention": prior_default},
        "note": (
            "Object lock remains ENABLED on the bucket and existing objects keep "
            "their retention; only uploads from now on are unprotected."
        ),
    }


# ── per-object retention (irreversible) ────────────────────────────────────


def guard_set_object_retention(
    conn: Any,
    bucket: str,
    object_name: str,
    mode: Any,
    days: Any,
    version_id: str | None = None,
    acknowledge_irreversible: bool = False,
) -> dict:
    """Raise every refusal ``set_object_retention`` would raise, without writing.

    Returns the current retention it read, so the caller (write path or preview)
    reports the same before-state from the same read. Reads, never writes — the
    line's rule is that a dry run may read but must not write, and a preview
    that cannot read cannot answer the only question worth asking here: would
    this be refused?
    """
    check_bucket_name(bucket)
    check_object_name(object_name)
    normalized = _normalize_mode(mode)
    _require_lock(conn, bucket)
    new_until = _retain_until(days)

    current = conn.get_object_retention(bucket, object_name, version_id=version_id) or {}
    current_mode = str(current.get("mode") or "").upper() or None
    current_until = current.get("retainUntil")
    current_remaining = _days_until(current_until)

    if current_mode:
        weakening = None
        if current_mode == "COMPLIANCE" and normalized == "GOVERNANCE":
            weakening = (
                "it downgrades COMPLIANCE to GOVERNANCE. COMPLIANCE retention "
                "cannot be downgraded by any credential, including root"
            )
        elif isinstance(current_remaining, (int, float)) and current_remaining > 0:
            new_remaining = _days_until(new_until.isoformat())
            if isinstance(new_remaining, (int, float)) and new_remaining < current_remaining:
                weakening = (
                    f"it shortens the retain-until date from {current_until} "
                    f"({current_remaining} days out) to "
                    f"{new_until.isoformat()} ({new_remaining} days out)"
                )
        if weakening:
            raise RetentionWeakened(
                f"Refusing to write retention on '{s(object_name, 120)}' because "
                f"{weakening}. Shortening or removing retention requires the "
                f"{BYPASS_HEADER} header, which the minio SDK this tool uses never "
                f"sends — so the server would reject it regardless of whether the "
                f"credential holds s3:BypassGovernanceRetention. Extend the date "
                f"instead, or — if the mode in force is GOVERNANCE — lift it out "
                f"of band with `{CLEAR_COMMAND}` from a credential holding that "
                f"permission. Measured against a live MinIO: that clears "
                f"GOVERNANCE and is refused for COMPLIANCE."
            )

    if normalized == "COMPLIANCE" and not acknowledge_irreversible:
        raise IrreversibleRetention(
            f"Refusing COMPLIANCE retention on '{s(object_name, 120)}' until "
            f"{new_until.isoformat()} without acknowledge_irreversible=True. "
            f"COMPLIANCE retention cannot be shortened or removed by anyone — not "
            f"this tool, not mc with the bypass header, not the root credential — "
            f"until that date passes, and the object's storage cannot be reclaimed "
            f"before then. There is no undo for this write and none will be "
            f"recorded. Use GOVERNANCE if an administrator should be able to lift "
            f"it, or pass acknowledge_irreversible=True if permanence is the point."
        )
    return {
        "mode": current_mode,
        "retainUntil": current_until,
        "daysRemaining": current_remaining,
        "newRetainUntil": new_until.isoformat(),
    }


def set_object_retention(
    conn: Any,
    bucket: str,
    object_name: str,
    mode: str,
    days: int,
    version_id: str | None = None,
    acknowledge_irreversible: bool = False,
) -> dict:
    """[WRITE][critical] Put retention on one object version. **No undo exists.**

    Extend-only: any call that would shorten or downgrade retention already in
    force is refused, because the bypass header S3 requires for that is one the
    SDK never sends. COMPLIANCE additionally requires
    ``acknowledge_irreversible=True``.

    ``priorState`` records what was there before for the audit trail; no undo
    token is recorded, because replaying one is impossible by construction.
    """
    preview = guard_set_object_retention(
        conn, bucket, object_name, mode, days,
        version_id=version_id,
        acknowledge_irreversible=acknowledge_irreversible,
    )
    normalized = _normalize_mode(mode)
    retain_until = datetime.fromisoformat(preview["newRetainUntil"])
    conn.set_object_retention(
        bucket, object_name, normalized, retain_until, version_id=version_id
    )
    observed = conn.get_object_retention(bucket, object_name, version_id=version_id) or {}
    return {
        "action": "set_object_retention",
        "bucket": s(bucket),
        "objectName": s(object_name, 1024),
        "versionId": opt_s(version_id),
        "applied": {
            "mode": normalized,
            "days": days,
            "retainUntil": preview["newRetainUntil"],
        },
        "observed": {
            "mode": observed.get("mode"),
            "retainUntil": observed.get("retainUntil"),
        },
        "priorState": {
            "mode": preview["mode"],
            "retainUntil": preview["retainUntil"],
        },
        "reversible": False,
        "note": (
            "No undo was recorded and none is possible: removing or shortening "
            f"retention needs the {BYPASS_HEADER} header, which this SDK does not "
            "send"
            + (
                ", and COMPLIANCE retention is refused even for the root "
                "credential — verified against a live MinIO, which rejected a "
                "clear, a downgrade and a version delete all with 'Object is WORM "
                "protected'."
                if normalized == "COMPLIANCE"
                else f". GOVERNANCE mode can still be lifted out of band with "
                f"`{CLEAR_COMMAND}` by a credential holding "
                f"s3:BypassGovernanceRetention."
            )
        ),
    }


# ── legal hold (reversible by design) ──────────────────────────────────────


def guard_set_legal_hold(conn: Any, bucket: str, object_name: str) -> bool | None:
    """Raise what ``set_legal_hold`` would raise; returns the current hold state."""
    check_bucket_name(bucket)
    check_object_name(object_name)
    _require_lock(conn, bucket)
    return conn.get_object_legal_hold(bucket, object_name)


def set_legal_hold(
    conn: Any,
    bucket: str,
    object_name: str,
    hold_on: bool,
    version_id: str | None = None,
) -> dict:
    """[WRITE][medium] Turn a legal hold on/off. Reversible → prior hold state.

    A legal hold blocks deletion with no date attached and is lifted by turning
    it off — the one WORM control that is reversible by design. It stacks with
    retention: lifting the hold does not make an object deletable while its
    retention is still running.
    """
    prior = guard_set_legal_hold(conn, bucket, object_name)
    conn.set_object_legal_hold(bucket, object_name, bool(hold_on), version_id=version_id)
    return {
        "action": "set_legal_hold",
        "bucket": s(bucket),
        "objectName": s(object_name, 1024),
        "versionId": opt_s(version_id),
        "holdOn": bool(hold_on),
        "priorState": {"legalHold": prior},
        "note": (
            "A legal hold is independent of retention: with retention still "
            "running, turning the hold off does not make the object deletable."
        ),
    }


def summarize_lock(conn: Any, bucket: str) -> dict:
    """Lock config + default retention days for previews. A read; never writes."""
    check_bucket_name(bucket)
    lock = conn.get_object_lock_config(bucket)
    if lock is None:
        return {"objectLockEnabled": False, "defaultRetentionDays": None}
    return {
        "objectLockEnabled": True,
        "defaultRetentionDays": _retention_days(lock.get("defaultRetention")),
    }
