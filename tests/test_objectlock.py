"""Object lock (WORM) tests: the two absences stay distinct, the guards refuse
before the round trip, retention records NO undo, and the reversible three record
undo descriptors that actually replay.

The load-bearing assertions here are the negative ones. A test that only checks
"retention was set" would pass just as happily against an implementation that
also handed out an undo token no credential could ever replay, or one that
reported a bucket with no default rule as protected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import lock_writes as writes
from minio_aiops.ops import objectlock as ops

pytestmark = pytest.mark.unit

LOCK_NO_DEFAULT = {"objectLockEnabled": True, "defaultRetention": None}
LOCK_GOV_30 = {
    "objectLockEnabled": True,
    "defaultRetention": {"mode": "GOVERNANCE", "days": 30, "years": None},
}
LOCK_COMPLIANCE_2Y = {
    "objectLockEnabled": True,
    "defaultRetention": {"mode": "COMPLIANCE", "days": None, "years": 2},
}


def _iso_in(days: float) -> str:
    return (datetime.now(tz=UTC) + timedelta(days=days)).isoformat()


# ─── reads: the two absences are NOT the same value ────────────────────────


def test_no_object_lock_is_distinct_from_lock_without_default_rule():
    """The false-safety case. Both are 'no retention applies', and collapsing
    them to one falsy value is what makes an audit call an unprotected bucket
    protected — the remedies are a new bucket vs. one retention call."""
    unlocked = MagicMock(name="conn")
    unlocked.get_object_lock_config.return_value = None
    unlocked.get_bucket_versioning.return_value = "Off"
    absent = ops.bucket_lock_config(unlocked, "plain-bkt")

    locked = MagicMock(name="conn")
    locked.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    locked.get_bucket_versioning.return_value = "Enabled"
    no_rule = ops.bucket_lock_config(locked, "worm-bkt")

    assert absent["objectLockEnabled"] is False
    assert no_rule["objectLockEnabled"] is True
    assert absent["defaultRetention"] is None and no_rule["defaultRetention"] is None
    # Same null rule, different reachable states — the payloads must differ.
    assert absent != no_rule
    assert "only at creation" in absent["note"]
    assert "no DEFAULT retention" in no_rule["note"]


def test_years_default_is_reported_in_days_for_comparison():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_COMPLIANCE_2Y
    conn.get_bucket_versioning.return_value = "Enabled"
    out = ops.bucket_lock_config(conn, "worm-bkt")
    assert out["defaultRetentionDays"] == 2 * ops.DAYS_PER_YEAR
    assert out["defaultRetention"]["years"] == 2


def test_object_status_separates_no_lock_from_no_retention():
    unlocked = MagicMock(name="conn")
    unlocked.get_object_lock_config.return_value = None
    out = ops.object_lock_status(unlocked, "plain-bkt", "k.txt")
    assert out["objectLockEnabled"] is False
    assert out["retention"] is None and out["legalHold"] is None
    # The connection must not have been asked about a hold it cannot have.
    assert not unlocked.get_object_legal_hold.called

    locked = MagicMock(name="conn")
    locked.get_object_lock_config.return_value = LOCK_GOV_30
    locked.get_object_retention.return_value = None
    locked.get_object_legal_hold.return_value = False
    out = ops.object_lock_status(locked, "worm-bkt", "k.txt")
    assert out["objectLockEnabled"] is True
    assert out["retention"] is None
    assert out["legalHold"] is False  # measured, not inferred from a missing lock
    assert out["protection"]["versionDestroyable"] is True


def test_retention_and_legal_hold_block_deletion_independently():
    """Operators conflate these: lifting the hold does nothing while retention
    runs, and lapsed retention does nothing while the hold is on."""
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_GOV_30
    conn.get_object_retention.return_value = {
        "mode": "GOVERNANCE", "retainUntil": _iso_in(10)
    }
    conn.get_object_legal_hold.return_value = True
    both = ops.object_lock_status(conn, "worm-bkt", "k.txt")
    assert both["protection"]["versionDestroyable"] is False
    assert set(both["protection"]["blockedBy"]) == {"legalHold", "retention:GOVERNANCE"}
    assert both["protection"]["bypassable"] is False  # the hold is not bypassable

    conn.get_object_legal_hold.return_value = False
    only_retention = ops.object_lock_status(conn, "worm-bkt", "k.txt")
    assert only_retention["protection"]["blockedBy"] == ["retention:GOVERNANCE"]
    assert only_retention["protection"]["bypassable"] is True

    conn.get_object_retention.return_value = {
        "mode": "COMPLIANCE", "retainUntil": _iso_in(10)
    }
    compliance = ops.object_lock_status(conn, "worm-bkt", "k.txt")
    assert compliance["protection"]["bypassable"] is False


def test_elapsed_retention_no_longer_blocks():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_GOV_30
    conn.get_object_retention.return_value = {
        "mode": "GOVERNANCE", "retainUntil": _iso_in(-3)
    }
    conn.get_object_legal_hold.return_value = False
    out = ops.object_lock_status(conn, "worm-bkt", "k.txt")
    assert out["retentionDaysRemaining"] < 0
    assert out["protection"]["versionDestroyable"] is True


# ─── the retention-gap analysis ────────────────────────────────────────────


def _scan_conn(lock, lifecycle=None, versioning="Enabled", buckets=("worm-bkt",)):
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": b} for b in buckets]
    conn.get_object_lock_config.return_value = lock
    conn.get_bucket_lifecycle.return_value = lifecycle
    conn.get_bucket_versioning.return_value = versioning
    return conn


def test_lifecycle_that_retention_outlives_is_reported_with_both_numbers():
    """The arithmetic contradiction: a 30-day expiry under 365-day retention can
    never delete, so the capacity it was added to reclaim never returns."""
    conn = _scan_conn(
        {"objectLockEnabled": True,
         "defaultRetention": {"mode": "GOVERNANCE", "days": 365, "years": None}},
        lifecycle=[{"ruleId": "expire-30", "expirationDays": 30}],
    )
    out = ops.diagnose_retention_gaps(conn)
    codes = [f["code"] for f in out["findings"]]
    assert "LIFECYCLE_CANNOT_EXPIRE_UNDER_RETENTION" in codes
    finding = next(f for f in out["findings"] if f["code"] == codes[0])
    assert out["findings"][0]["severity"] == "high"  # sorted worst-first
    assert finding["rank"] == 1
    gap = next(
        f for f in out["findings"]
        if f["code"] == "LIFECYCLE_CANNOT_EXPIRE_UNDER_RETENTION"
    )
    assert gap["expirationDays"] == 30
    assert gap["defaultRetentionDays"] == 365
    assert gap["shortfallDays"] == 335


def test_lifecycle_longer_than_retention_is_not_a_finding():
    conn = _scan_conn(LOCK_GOV_30, lifecycle=[{"ruleId": "e", "expirationDays": 90}])
    codes = [f["code"] for f in ops.diagnose_retention_gaps(conn)["findings"]]
    assert "LIFECYCLE_CANNOT_EXPIRE_UNDER_RETENTION" not in codes


def test_lock_enabled_without_default_rule_is_high():
    conn = _scan_conn(LOCK_NO_DEFAULT)
    out = ops.diagnose_retention_gaps(conn)
    finding = next(
        f for f in out["findings"] if f["code"] == "LOCK_ENABLED_NO_DEFAULT_RETENTION"
    )
    assert finding["severity"] == "high"
    assert out["lockEnabledBuckets"] == 1


def test_bucket_without_lock_yields_no_findings_but_is_counted_as_scanned():
    conn = _scan_conn(None)
    out = ops.diagnose_retention_gaps(conn)
    assert out["findings"] == []
    assert out["bucketsScanned"] == 1
    assert out["lockEnabledBuckets"] == 0


def test_governance_and_long_compliance_defaults_get_their_own_codes():
    gov_out = ops.diagnose_retention_gaps(_scan_conn(LOCK_GOV_30))
    assert [f["code"] for f in gov_out["findings"]] == ["GOVERNANCE_DEFAULT_BYPASSABLE"]
    assert gov_out["findings"][0]["severity"] == "low"

    comp_out = ops.diagnose_retention_gaps(_scan_conn(LOCK_COMPLIANCE_2Y))
    codes = [f["code"] for f in comp_out["findings"]]
    assert "COMPLIANCE_DEFAULT_LONG" in codes


def test_lock_without_active_versioning_is_flagged():
    conn = _scan_conn(LOCK_GOV_30, versioning="Suspended")
    codes = [f["code"] for f in ops.diagnose_retention_gaps(conn)["findings"]]
    assert "LOCK_WITHOUT_ACTIVE_VERSIONING" in codes


def test_probe_failure_is_reported_not_swallowed():
    """bug class #3: a failed probe must not look like a clean bucket."""
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": "a-bkt"}, {"name": "b-bkt"}]
    conn.get_bucket_lifecycle.side_effect = [RuntimeError("boom"), None]
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_bucket_versioning.return_value = "Enabled"
    out = ops.diagnose_retention_gaps(conn)
    assert out["bucketErrors"][0]["bucket"] == "a-bkt"
    assert "boom" in out["bucketErrors"][0]["error"]
    assert "does not mean those buckets are clean" in out["note"]


def test_findings_truncation_is_measured_and_ranked():
    conn = _scan_conn(LOCK_NO_DEFAULT, buckets=tuple(f"bkt-{i}" for i in range(5)))
    out = ops.diagnose_retention_gaps(conn, limit=3)
    assert out["returned"] == 3 and out["limit"] == 3 and out["truncated"] is True
    assert [f["rank"] for f in out["findings"]] == [1, 2, 3]


# ─── writes: guards refuse before any round trip ───────────────────────────


def test_retention_on_bucket_without_lock_is_refused_without_writing():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = None
    with pytest.raises(writes.ObjectLockUnavailable, match="only at bucket creation"):
        writes.set_object_retention(conn, "plain-bkt", "k.txt", "GOVERNANCE", 30)
    assert not conn.set_object_retention.called


def test_compliance_requires_explicit_acknowledgement():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_retention.return_value = None
    with pytest.raises(writes.IrreversibleRetention, match="acknowledge_irreversible"):
        writes.set_object_retention(conn, "worm-bkt", "k.txt", "COMPLIANCE", 30)
    assert not conn.set_object_retention.called

    out = writes.set_object_retention(
        conn, "worm-bkt", "k.txt", "COMPLIANCE", 30, acknowledge_irreversible=True
    )
    assert out["reversible"] is False
    assert conn.set_object_retention.called


def test_governance_does_not_require_acknowledgement():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_retention.return_value = None
    out = writes.set_object_retention(conn, "worm-bkt", "k.txt", "GOVERNANCE", 7)
    assert out["applied"]["mode"] == "GOVERNANCE"
    # The remedy names a real command: `mc retention clear` has no --bypass flag
    # (measured), so quoting one would send the operator to debug the wrong thing.
    assert "mc retention clear" in out["note"]
    assert "--bypass" not in out["note"]


def test_shortening_retention_is_refused_naming_the_bypass_header():
    """The SDK never sends x-amz-bypass-governance-retention, so the server
    would reject this regardless of the credential's permissions. Refuse locally
    so the reason survives into the dry run."""
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_retention.return_value = {
        "mode": "GOVERNANCE", "retainUntil": _iso_in(90)
    }
    with pytest.raises(writes.RetentionWeakened, match="bypass-governance-retention"):
        writes.set_object_retention(conn, "worm-bkt", "k.txt", "GOVERNANCE", 10)
    assert not conn.set_object_retention.called


def test_extending_retention_is_allowed():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_retention.return_value = {
        "mode": "GOVERNANCE", "retainUntil": _iso_in(10)
    }
    out = writes.set_object_retention(conn, "worm-bkt", "k.txt", "GOVERNANCE", 90)
    assert conn.set_object_retention.called
    assert out["priorState"]["mode"] == "GOVERNANCE"


def test_downgrading_compliance_to_governance_is_refused():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_retention.return_value = {
        "mode": "COMPLIANCE", "retainUntil": _iso_in(10)
    }
    with pytest.raises(writes.RetentionWeakened, match="downgrades COMPLIANCE"):
        writes.set_object_retention(
            conn, "worm-bkt", "k.txt", "GOVERNANCE", 3650,
            acknowledge_irreversible=True,
        )
    assert not conn.set_object_retention.called


def test_default_retention_requires_exactly_one_duration_unit():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    with pytest.raises(ValueError, match="exactly one of days or years"):
        writes.set_default_retention(conn, "worm-bkt", "GOVERNANCE")
    with pytest.raises(ValueError, match="exactly one of days or years"):
        writes.set_default_retention(conn, "worm-bkt", "GOVERNANCE", days=1, years=1)
    assert not conn.set_object_lock_config.called


def test_clear_default_retention_says_lock_stays_enabled():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_GOV_30
    out = writes.clear_default_retention(conn, "worm-bkt")
    conn.set_object_lock_config.assert_called_once_with("worm-bkt", None, None, None)
    assert out["priorState"]["defaultRetention"] == LOCK_GOV_30["defaultRetention"]
    assert "remains ENABLED" in out["note"]


def test_mode_validation_rejects_anything_else():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    for bad in ("worm", "compliant", "", None):
        with pytest.raises(ValueError, match="mode must be one of"):
            writes.set_object_retention(conn, "worm-bkt", "k.txt", bad, 30)


def test_object_key_gate_rejects_control_characters_and_oversize():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    with pytest.raises(ValueError, match="control characters"):
        writes.set_legal_hold(conn, "worm-bkt", "bad\nkey", True)
    with pytest.raises(ValueError, match="bytes"):
        writes.set_legal_hold(conn, "worm-bkt", "k" * 1025, True)
    with pytest.raises(ValueError, match="must not be empty"):
        writes.set_legal_hold(conn, "worm-bkt", "", True)
    assert not conn.set_object_legal_hold.called


def test_create_bucket_reads_back_what_the_server_did():
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_bucket_versioning.return_value = "Enabled"
    out = writes.create_bucket(conn, "worm-bkt", object_lock=True)
    conn.make_bucket.assert_called_once_with("worm-bkt", object_lock=True)
    # Reported as observed, not echoed from the request.
    assert out["requestedObjectLock"] is True
    assert out["objectLockEnabled"] is True
    assert out["versioning"] == "Enabled"


# ─── MCP layer: undo presence/absence and replayability ────────────────────


@pytest.fixture
def recorded(monkeypatch):
    import minio_aiops.governance.undo as undo_mod

    box: dict = {}

    class _Store:
        def record(self, *, skill, tool, undo_descriptor, orig_params, effect_verified=True):
            box["d"] = undo_descriptor
            return "undo-1"

    monkeypatch.setattr(undo_mod, "get_undo_store", lambda: _Store())
    return box


def _gov_conn(monkeypatch) -> MagicMock:
    from mcp_server.tools import objectlock as gov

    conn = MagicMock(name="conn")
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    return conn


def test_object_retention_records_no_undo_descriptor(monkeypatch, recorded):
    """An undo token here would be one whose replay is guaranteed to fail while
    the audit row claims the write was reversible — bug class #6."""
    from mcp_server.tools import objectlock as gov

    conn = _gov_conn(monkeypatch)
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_retention.return_value = None
    result = gov.set_object_retention(
        bucket_name="worm-bkt", object_name="k.txt", mode="GOVERNANCE", days=30
    )
    assert "error" not in result
    assert result["reversible"] is False
    assert "d" not in recorded  # no undo descriptor was recorded at all


def test_legal_hold_undo_restores_prior_state_and_replays(monkeypatch, recorded):
    from mcp_server.tools import objectlock as gov

    conn = _gov_conn(monkeypatch)
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_legal_hold.return_value = False
    gov.set_legal_hold(bucket_name="worm-bkt", object_name="k.txt", hold_on=True)
    descriptor = recorded["d"]
    assert descriptor["tool"] == "set_legal_hold"
    assert descriptor["params"]["hold_on"] is False

    replay_conn = _gov_conn(monkeypatch)
    replay_conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    replay_conn.get_object_legal_hold.return_value = True
    replayed = getattr(gov, descriptor["tool"])(**descriptor["params"])
    assert "error" not in replayed, f"undo replay failed: {replayed}"
    replay_conn.set_object_legal_hold.assert_called_once_with(
        "worm-bkt", "k.txt", False, version_id=None
    )


def test_default_retention_undo_restores_prior_rule_and_replays(monkeypatch, recorded):
    from mcp_server.tools import objectlock as gov

    conn = _gov_conn(monkeypatch)
    conn.get_object_lock_config.return_value = LOCK_GOV_30
    gov.set_default_retention(bucket_name="worm-bkt", mode="COMPLIANCE", years=7)
    descriptor = recorded["d"]
    assert descriptor["tool"] == "set_default_retention"
    assert descriptor["params"]["mode"] == "GOVERNANCE"
    assert descriptor["params"]["days"] == 30

    replay_conn = _gov_conn(monkeypatch)
    replay_conn.get_object_lock_config.return_value = LOCK_COMPLIANCE_2Y
    replayed = getattr(gov, descriptor["tool"])(**descriptor["params"])
    assert "error" not in replayed, f"undo replay failed: {replayed}"
    replay_conn.set_object_lock_config.assert_called_once_with(
        "worm-bkt", "GOVERNANCE", 30, "Days"
    )


def test_default_retention_undo_clears_when_there_was_no_prior_rule(monkeypatch, recorded):
    from mcp_server.tools import objectlock as gov

    conn = _gov_conn(monkeypatch)
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    gov.set_default_retention(bucket_name="worm-bkt", mode="GOVERNANCE", days=30)
    assert recorded["d"]["tool"] == "clear_default_retention"


def test_create_bucket_undo_is_the_delete(monkeypatch, recorded):
    from mcp_server.tools import objectlock as gov

    conn = _gov_conn(monkeypatch)
    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_bucket_versioning.return_value = "Enabled"
    gov.bucket_create(bucket_name="worm-bkt", object_lock=True)
    descriptor = recorded["d"]
    assert descriptor["tool"] == "bucket_delete"
    assert descriptor["params"] == {"bucket_name": "worm-bkt"}


# ─── dry runs run the guards (a preview must not report green) ──────────────


def test_dry_run_refuses_what_the_real_call_would_refuse(monkeypatch):
    """bug class #10: dry_run may read, and here it must — 'would this be
    refused?' is the only question worth asking before an irreversible write."""
    from mcp_server.tools import objectlock as gov

    conn = _gov_conn(monkeypatch)
    conn.get_object_lock_config.return_value = None
    result = gov.set_object_retention(
        bucket_name="plain-bkt", object_name="k.txt", mode="GOVERNANCE",
        days=30, dry_run=True,
    )
    assert "error" in result
    assert "dryRun" not in result
    assert not conn.set_object_retention.called

    conn.get_object_lock_config.return_value = LOCK_NO_DEFAULT
    conn.get_object_retention.return_value = None
    unacked = gov.set_object_retention(
        bucket_name="worm-bkt", object_name="k.txt", mode="COMPLIANCE",
        days=30, dry_run=True,
    )
    assert "error" in unacked and "acknowledge_irreversible" in unacked["error"]

    allowed = gov.set_object_retention(
        bucket_name="worm-bkt", object_name="k.txt", mode="GOVERNANCE",
        days=30, dry_run=True,
    )
    assert allowed["dryRun"] is True
    assert allowed["reversible"] is False
    assert allowed["wouldSetObjectRetention"]["retainUntil"]
    assert not conn.set_object_retention.called


def test_dry_run_default_retention_reports_the_refusal_too(monkeypatch):
    from mcp_server.tools import objectlock as gov

    conn = _gov_conn(monkeypatch)
    conn.get_object_lock_config.return_value = None
    result = gov.set_default_retention(
        bucket_name="plain-bkt", mode="GOVERNANCE", days=30, dry_run=True
    )
    assert "error" in result and "object lock" in result["error"]
    assert not conn.set_object_lock_config.called


def test_protection_says_a_delete_marker_is_still_possible():
    """Verified against a live MinIO: a plain DELETE on a retained key always
    succeeds and writes a delete marker; only deleting that *version* is refused
    ("is WORM protected"). A flat "deletable: false" would therefore be a wrong
    headline — the bytes are safe, the key's visibility is not."""
    conn = MagicMock(name="conn")
    conn.get_object_lock_config.return_value = LOCK_GOV_30
    conn.get_object_retention.return_value = {
        "mode": "COMPLIANCE", "retainUntil": _iso_in(30)
    }
    conn.get_object_legal_hold.return_value = False
    out = ops.object_lock_status(conn, "worm-bkt", "k.txt")
    assert out["protection"]["versionDestroyable"] is False
    assert out["protection"]["deleteMarkerStillPossible"] is True
    assert "delete marker" in out["protection"]["note"]
    # An unprotected version carries neither the flag nor the caveat.
    conn.get_object_retention.return_value = None
    clean = ops.object_lock_status(conn, "worm-bkt", "k.txt")
    assert "deleteMarkerStillPossible" not in clean["protection"]
