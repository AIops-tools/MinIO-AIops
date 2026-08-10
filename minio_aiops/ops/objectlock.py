"""Object lock / WORM reads + the retention-gap analysis.

S3 object lock is the one control in this tool whose *absence* and whose
*presence* are both worth an operator's attention, and whose failure mode is
silent in both directions:

* A bucket without object lock can never get it — S3 accepts the flag only at
  bucket creation. "We'll turn it on later" is not available.
* A bucket **with** object lock but no default retention rule retains nothing.
  Every dashboard and audit questionnaire reads "object lock: enabled" as
  "data is protected"; an upload that omits the retention header is deletable a
  second later.

:func:`diagnose_retention_gaps` exists for the second case and for the
arithmetic contradiction nobody checks by hand: a lifecycle rule that expires
objects after 30 days on a bucket whose default retention is 365 days will
*never* delete anything, so the capacity it was added to reclaim never comes
back. Both numbers are reported, so the finding is checkable rather than
asserted.

All findings carry severity + cause + action and are sorted worst-first with an
explicit 1-based ``rank``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from minio_aiops.ops._util import check_bucket_name, check_object_name, opt_s, s

#: Severity ordering used for the worst-first sort (higher = worse).
_SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}

#: A COMPLIANCE default at or above this many days is called out on its own:
#: every upload becomes undeletable for that long by anyone, root included.
COMPLIANCE_LONG_DAYS = 365

#: Approximation used only to compare a years-denominated retention against a
#: lifecycle rule expressed in days. Stated here because the comparison it
#: feeds is reported to the caller as a number.
DAYS_PER_YEAR = 365


def _retention_days(default: dict | None) -> int | None:
    """The default rule's duration in days (years converted), or None."""
    if not default:
        return None
    days = default.get("days")
    if isinstance(days, int) and days > 0:
        return days
    years = default.get("years")
    if isinstance(years, int) and years > 0:
        return years * DAYS_PER_YEAR
    return None


def bucket_lock_config(conn: Any, bucket: str) -> dict:
    """[READ] Object-lock state of one bucket, with the two absences separated.

    ``objectLockEnabled: false`` means WORM is impossible on this bucket for the
    rest of its life. ``true`` with ``defaultRetention: null`` means WORM is
    available but nothing is retained unless each upload asks for it.
    """
    check_bucket_name(bucket)
    lock = conn.get_object_lock_config(bucket)
    versioning = conn.get_bucket_versioning(bucket)
    if lock is None:
        return {
            "bucket": s(bucket),
            "objectLockEnabled": False,
            "defaultRetention": None,
            "versioning": versioning,
            "note": (
                "Object lock is not enabled and cannot be enabled on an existing "
                "bucket — S3 accepts the flag only at creation. To get WORM here, "
                "create a new bucket with bucket_create(object_lock=True) and "
                "migrate the objects."
            ),
        }
    default = lock.get("defaultRetention")
    out = {
        "bucket": s(bucket),
        "objectLockEnabled": True,
        "defaultRetention": default,
        "defaultRetentionDays": _retention_days(default),
        "versioning": versioning,
    }
    if default is None:
        out["note"] = (
            "Object lock is enabled but no DEFAULT retention rule is set: an "
            "upload that does not carry its own retention header is retained for "
            "nothing and can be deleted immediately. Set one with "
            "set_default_retention, or put retention on each object explicitly."
        )
    return out


def object_lock_status(
    conn: Any, bucket: str, object_name: str, version_id: str | None = None
) -> dict:
    """[READ] Retention + legal hold for one object version.

    ``retention`` / ``legalHold`` are ``null`` when the bucket has no object
    lock at all — distinct from "lock is available and this object has none",
    which reports ``objectLockEnabled: true`` with a null ``retention``. The
    remedies differ: one needs a new bucket, the other needs a retention call.
    """
    check_bucket_name(bucket)
    check_object_name(object_name)
    lock = conn.get_object_lock_config(bucket)
    if lock is None:
        return {
            "bucket": s(bucket),
            "objectName": s(object_name, 1024),
            "versionId": opt_s(version_id),
            "objectLockEnabled": False,
            "retention": None,
            "legalHold": None,
            "note": (
                "The bucket has no object lock, so neither retention nor a legal "
                "hold is expressible on this object. Not the same as 'this object "
                "is unprotected within a WORM bucket'."
            ),
        }
    retention = conn.get_object_retention(bucket, object_name, version_id=version_id)
    hold = conn.get_object_legal_hold(bucket, object_name, version_id=version_id)
    out: dict[str, Any] = {
        "bucket": s(bucket),
        "objectName": s(object_name, 1024),
        "versionId": opt_s(version_id),
        "objectLockEnabled": True,
        "retention": retention,
        "legalHold": hold,
    }
    if retention and retention.get("retainUntil"):
        out["retentionDaysRemaining"] = _days_until(retention["retainUntil"])
    out["protection"] = _protection(retention, hold, out.get("retentionDaysRemaining"))
    return out


def _days_until(iso_timestamp: str | None) -> float | None:
    """Days from now until ``iso_timestamp`` (negative = already elapsed)."""
    if not iso_timestamp:
        return None
    try:
        when = datetime.fromisoformat(str(iso_timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return round((when - datetime.now(tz=UTC)).total_seconds() / 86400.0, 2)


def _protection(retention: dict | None, hold: bool | None, days_remaining: Any) -> dict:
    """What object lock does and does not stop for this version.

    Deliberately ``versionDestroyable`` rather than a flat ``deletable``.
    Measured against a real MinIO: a plain ``DELETE`` on a retained key **always
    succeeds** on a versioned bucket — it writes a delete marker, the key stops
    appearing in listings, and the retained version is untouched. Only deleting
    that *version* is refused ("is WORM protected and cannot be overwritten").
    So "deletable: false" would tell a caller the object cannot be deleted when
    anyone can make it disappear from view a second later; what retention buys is
    that the bytes survive.

    The two blockers stack independently, which is the part operators get wrong:
    lapsed retention does not help while a legal hold is on, and lifting the hold
    does not help while retention runs.
    """
    blockers: list[str] = []
    if hold:
        blockers.append("legalHold")
    mode = (retention or {}).get("mode")
    if mode and isinstance(days_remaining, (int, float)) and days_remaining > 0:
        blockers.append(f"retention:{mode}")
    protection = {
        "versionDestroyable": not blockers,
        "blockedBy": blockers,
        # GOVERNANCE alone yields to `mc rm --bypass` with
        # s3:BypassGovernanceRetention (verified on a live server). A legal hold
        # does not, and neither does COMPLIANCE.
        "bypassable": bool(blockers) and blockers == ["retention:GOVERNANCE"],
    }
    if blockers:
        protection["deleteMarkerStillPossible"] = True
        protection["note"] = (
            "This version's bytes are protected, but a plain DELETE on the key "
            "still succeeds and writes a delete marker: the object stops showing "
            "up in listings while the retained version survives underneath. "
            "Object lock protects the data, not the key's visibility."
        )
    return protection


def _finding(code: str, severity: str, bucket: str, cause: str, action: str, **extra) -> dict:
    finding = {
        "code": code,
        "severity": severity,
        "bucket": s(bucket),
        "cause": cause,
        "action": action,
    }
    finding.update(extra)
    return finding


def _bucket_findings(
    conn: Any, bucket: str, lifecycle: list[dict] | None
) -> tuple[bool, list[dict]]:
    """``(lock_enabled, findings)`` for one bucket.

    The flag is returned rather than re-derived by a second probe: asking twice
    would double the calls and force a swallowed failure into the counter.
    """
    lock = conn.get_object_lock_config(bucket)
    findings: list[dict] = []
    if lock is None:
        return False, findings  # a bucket with no lock is inventory, not a defect

    default = lock.get("defaultRetention")
    retention_days = _retention_days(default)
    versioning = conn.get_bucket_versioning(bucket)

    if versioning != "Enabled":
        findings.append(
            _finding(
                "LOCK_WITHOUT_ACTIVE_VERSIONING",
                "high",
                bucket,
                f"Object lock is enabled but versioning reports {versioning!r}. "
                f"Object lock is implemented on top of versions, so retention on "
                f"a bucket whose versioning is not active cannot be relied on.",
                "Re-enable versioning: set_versioning(bucket, 'Enabled').",
                versioning=versioning,
            )
        )

    if default is None:
        findings.append(
            _finding(
                "LOCK_ENABLED_NO_DEFAULT_RETENTION",
                "high",
                bucket,
                "Object lock is enabled but there is no default retention rule, "
                "so any upload that omits an explicit retention header is stored "
                "with no retention at all and can be deleted immediately. The "
                "bucket still advertises itself as lock-enabled.",
                "Set a default: set_default_retention(bucket, mode, days=N). Objects "
                "already stored are unaffected — check them with object_lock_status.",
            )
        )
    elif str(default.get("mode")) == "COMPLIANCE" and (retention_days or 0) >= (
        COMPLIANCE_LONG_DAYS
    ):
        findings.append(
            _finding(
                "COMPLIANCE_DEFAULT_LONG",
                "medium",
                bucket,
                f"The default retention mode is COMPLIANCE for {retention_days} days. "
                f"Every object written to this bucket — including one written by "
                f"mistake — becomes undeletable for that long by everyone, the root "
                f"credential included. Storage for it cannot be reclaimed early.",
                "Confirm this matches the regulatory requirement. If the intent was "
                "an internal policy that an administrator can override, GOVERNANCE "
                "is the mode that allows it.",
                defaultRetentionDays=retention_days,
                mode="COMPLIANCE",
            )
        )
    elif str(default.get("mode")) == "GOVERNANCE":
        findings.append(
            _finding(
                "GOVERNANCE_DEFAULT_BYPASSABLE",
                "low",
                bucket,
                f"The default retention mode is GOVERNANCE for {retention_days} days. "
                f"A caller holding s3:BypassGovernanceRetention can delete these "
                f"objects before the date, so this protects against accident, not "
                f"against a privileged actor.",
                "Adequate for internal retention policy. If an auditor requires "
                "retention no one can lift, COMPLIANCE is the mode — note it cannot "
                "be shortened afterwards.",
                defaultRetentionDays=retention_days,
                mode="GOVERNANCE",
            )
        )

    # The arithmetic contradiction: a lifecycle expiry that retention outlives.
    for rule in lifecycle or []:
        expire_days = rule.get("expirationDays")
        if not isinstance(expire_days, int) or not retention_days:
            continue
        if expire_days < retention_days:
            findings.append(
                _finding(
                    "LIFECYCLE_CANNOT_EXPIRE_UNDER_RETENTION",
                    "high",
                    bucket,
                    f"Lifecycle rule {rule.get('ruleId')!r} expires objects after "
                    f"{expire_days} days, but the default retention holds them for "
                    f"{retention_days} days. The rule cannot delete them; it will "
                    f"keep trying and the capacity it was added to reclaim is never "
                    f"returned.",
                    f"Either raise the lifecycle expiry above {retention_days} days, "
                    f"or lower the default retention if the retention period was the "
                    f"mistake (that only affects objects written from now on).",
                    ruleId=opt_s(rule.get("ruleId")),
                    expirationDays=expire_days,
                    defaultRetentionDays=retention_days,
                    shortfallDays=retention_days - expire_days,
                )
            )
    return True, findings


def diagnose_retention_gaps(conn: Any, limit: int = 50) -> dict:
    """[READ] WORM/retention gaps across every bucket, worst-first.

    Reports the contradictions rather than a preference: lock enabled with
    nothing retained, retention that outlives the lifecycle rule meant to
    reclaim the space, lock on a bucket whose versioning is not active, and the
    mode choice (COMPLIANCE is unliftable, GOVERNANCE is bypassable) with the
    measured day counts on both sides.

    A bucket whose probes fail is listed in ``bucketErrors`` rather than skipped:
    "no findings" must not be the same payload as "could not look".
    """
    requested = max(1, int(limit))
    buckets = conn.list_buckets()
    findings: list[dict] = []
    errors: list[dict] = []
    locked = 0
    for bucket in buckets:
        name = bucket.get("name")
        if not name:
            continue
        try:
            lifecycle = conn.get_bucket_lifecycle(name)
            lock_enabled, bucket_findings = _bucket_findings(conn, name, lifecycle)
        except Exception as exc:  # noqa: BLE001 — reported per bucket, never swallowed
            errors.append({"bucket": s(name), "error": s(exc, 200)})
            continue
        if lock_enabled:
            locked += 1
        findings.extend(bucket_findings)

    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f.get("severity", "low"), 0), reverse=True)
    truncated = len(findings) > requested
    shown = findings[:requested]
    for index, finding in enumerate(shown, start=1):
        finding["rank"] = index
    out = {
        "bucketsScanned": len(buckets),
        "lockEnabledBuckets": locked,
        "findings": shown,
        "returned": len(shown),
        "limit": requested,
        "truncated": truncated,
    }
    if errors:
        out["bucketErrors"] = errors
        out["note"] = (
            f"{len(errors)} bucket(s) could not be probed and contribute no "
            f"findings — see bucketErrors. A clean 'findings' list does not mean "
            f"those buckets are clean."
        )
    return out
