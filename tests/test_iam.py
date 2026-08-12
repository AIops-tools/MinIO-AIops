"""IAM tests: self-targeting is refused, secrets never reach a result or the
audit row, and the analysis resolves group-inherited policies.

The load-bearing assertions are again negative. A test that only checked
"disable_user called the API" would pass against an implementation that happily
disables the tool's own credential and leaves no way back in — the exact defect
that was caught live in the sibling identity tool, where the disable succeeded
and the undo came back 403.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from minio_aiops.connection import MinioApiError
from minio_aiops.ops import iam as ops
from minio_aiops.ops import iam_writes as writes

pytestmark = pytest.mark.unit

OWN_KEY = "toolkey"


def _conn(users=None, groups=None, policies=None, own=OWN_KEY):
    conn = MagicMock(name="conn")
    conn.target = MagicMock()
    conn.target.access_key = own
    conn.list_users.return_value = users if users is not None else {}
    conn.list_groups.return_value = list((groups or {}).keys())
    conn.group_info.side_effect = lambda g: (groups or {}).get(g, {})
    conn.list_policies.return_value = policies if policies is not None else {}
    return conn


# ─── self-targeting: the operation that would destroy its own undo ─────────


@pytest.mark.parametrize(
    "call",
    [
        lambda c: writes.set_user_status(c, OWN_KEY, False),
        lambda c: writes.set_user_status(c, OWN_KEY, True),
        lambda c: writes.remove_user(c, OWN_KEY),
        lambda c: writes.set_user_policies(c, OWN_KEY, ["readonly"], attach=False),
        lambda c: writes.set_user_policies(c, OWN_KEY, ["readonly"], attach=True),
    ],
)
def test_every_user_write_refuses_its_own_credential(call):
    conn = _conn()
    with pytest.raises(writes.SelfTargeted, match="authenticates with"):
        call(conn)
    # Nothing at all reached the server.
    assert not conn.set_user_status.called
    assert not conn.remove_user.called
    assert not conn.set_user_policies.called


def test_a_different_user_is_not_blocked():
    conn = _conn()
    conn.user_info.return_value = {"status": "enabled", "policyName": "readonly"}
    out = writes.set_user_status(conn, "otherkey", False)
    conn.set_user_status.assert_called_once_with("otherkey", False)
    assert out["priorState"]["enabled"] is True


def test_the_guard_needs_no_round_trip_so_it_works_in_a_preview():
    """A purely local comparison — no unknown-identity case, and it fires
    identically under dry_run."""
    conn = _conn()
    with pytest.raises(writes.SelfTargeted):
        writes.guard_self_target(conn, OWN_KEY, "disable the user")
    assert conn.mock_calls == []  # not one call was made


def test_no_configured_key_does_not_block_everything():
    """A target with no access key must not make every IAM write impossible."""
    conn = _conn(own="")
    conn.user_info.return_value = {"status": "enabled"}
    writes.set_user_status(conn, "anyone", False)  # must not raise
    assert conn.set_user_status.called


# ─── secrets must not reach a result (the audit stores results verbatim) ───


def test_create_user_never_returns_the_secret():
    conn = _conn()
    conn.user_info.side_effect = MinioApiError("no such user", status_code=404)
    out = writes.create_user(conn, "newkey", "s3cret-value-123")
    conn.add_user.assert_called_once_with("newkey", "s3cret-value-123")
    serialized = json.dumps(out)
    assert "s3cret-value-123" not in serialized
    assert out["secretReturned"] is False
    assert out["priorState"]["existed"] is False


def test_create_user_is_declared_sensitive_so_the_audit_row_redacts_it():
    """The harness redacts declared sensitive params in the audit row and in the
    undo record's orig_params; results are stored verbatim, which is why the
    result carries no secret either."""
    from mcp_server.tools import iam as gov

    assert "secret_key" in getattr(gov.create_user, "_sensitive_params", [])


def test_create_user_validates_the_secret_length_before_calling():
    conn = _conn()
    with pytest.raises(ValueError, match="at least 8"):
        writes.create_user(conn, "newkey", "short")
    assert not conn.add_user.called


def test_access_key_gate_rejects_hostile_values():
    conn = _conn()
    for bad in ("", "ab", "has space", "sla/sh", "a" * 129):
        with pytest.raises(ValueError, match="Invalid access key"):
            writes.create_user(conn, bad, "s3cret-value-123")
    assert not conn.add_user.called


# ─── upsert semantics: the undo must not destroy what it cannot restore ───


def test_recreating_an_existing_user_says_the_secret_was_replaced():
    conn = _conn()
    conn.user_info.return_value = {"status": "enabled", "policyName": "readonly"}
    out = writes.create_user(conn, "existing", "s3cret-value-123")
    assert out["priorState"]["existed"] is True
    assert "REPLACED" in out["note"]
    assert out["priorState"]["policies"] == ["readonly"]


def test_no_undo_is_recorded_when_create_user_replaced_a_secret(monkeypatch, recorded):
    """Removing the account would destroy the credential rather than restore the
    prior one, and no descriptor can put the old secret back."""
    from mcp_server.tools import iam as gov

    conn = _conn()
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    conn.user_info.return_value = {"status": "enabled"}
    gov.create_user(access_key="existing", secret_key="s3cret-value-123")
    assert "d" not in recorded


def test_remove_user_records_no_undo_and_says_why():
    conn = _conn()
    conn.user_info.return_value = {"status": "enabled", "policyName": "readwrite"}
    out = writes.remove_user(conn, "gone")
    assert out["reversible"] is False
    assert out["priorState"]["policies"] == ["readwrite"]
    assert "no recoverable copy" in out["note"]


def test_remove_user_reports_a_failed_prior_read_rather_than_hiding_it():
    conn = _conn()
    conn.user_info.side_effect = RuntimeError("admin api denied")
    out = writes.remove_user(conn, "gone")
    assert "admin api denied" in out["priorState"]["readError"]
    assert conn.remove_user.called


# ─── reads: absence has a reason ──────────────────────────────────────────


def test_no_iam_users_is_explained_not_just_empty():
    """The root credential is not an IAM user, so an empty list is a real state
    and must not read as a failed probe."""
    out = ops.list_users(_conn(users={}))
    assert out["users"] == []
    assert "not an IAM user" in out["note"]


def test_users_carry_status_and_policies():
    out = ops.list_users(_conn(users={
        "alice": {"status": "enabled", "policyName": "readonly"},
        "bob": {"status": "disabled", "policyName": "consoleAdmin,readwrite"},
    }))
    by_key = {u["accessKey"]: u for u in out["users"]}
    assert by_key["alice"]["enabled"] is True
    assert by_key["bob"]["enabled"] is False
    assert by_key["bob"]["policies"] == ["consoleAdmin", "readwrite"]


def test_an_unknown_status_string_is_null_not_a_guess():
    out = ops.list_users(_conn(users={"alice": {"policyName": "readonly"}}))
    assert out["users"][0]["enabled"] is None


def test_group_read_failures_are_reported():
    conn = _conn(groups={"devs": {}, "ops": {}})
    conn.group_info.side_effect = [RuntimeError("boom"), {"members": ["alice"]}]
    out = ops.list_groups(conn)
    assert out["groupErrors"][0]["group"] == "devs"
    assert "does not mean few groups" in out["note"]


def test_listing_truncation_is_measured():
    users = {f"u{i}": {"status": "enabled"} for i in range(5)}
    out = ops.list_users(_conn(users=users), limit=2)
    assert out["returned"] == 2 and out["limit"] == 2 and out["truncated"] is True


# ─── the exposure analysis ────────────────────────────────────────────────


def test_a_user_with_no_policy_at_all_is_a_finding():
    """MinIO denies by default, so this account can do nothing — broken, not lax,
    and indistinguishable from a healthy one in a name-and-status listing."""
    out = ops.diagnose_iam_exposure(_conn(users={"app": {"status": "enabled"}}))
    codes = [r["code"] for f in out["findings"] for r in f["reasons"]]
    assert "NO_EFFECTIVE_POLICY" in codes


def test_a_group_managed_user_is_not_reported_as_having_no_policy():
    """Resolving inheritance matters: otherwise every correctly group-managed
    account is flagged, which is the normal way to run MinIO."""
    conn = _conn(
        users={"app": {"status": "enabled", "memberOf": ["devs"]}},
        groups={"devs": {"members": ["app"], "policyName": "readonly"}},
    )
    out = ops.diagnose_iam_exposure(conn)
    codes = [r["code"] for f in out["findings"] for r in f["reasons"]]
    assert "NO_EFFECTIVE_POLICY" not in codes


def test_admin_sprawl_outranks_broad_data_access():
    conn = _conn(users={
        "adm": {"status": "enabled", "policyName": "consoleAdmin"},
        "rw": {"status": "enabled", "policyName": "readwrite"},
    })
    out = ops.diagnose_iam_exposure(conn)
    assert out["findings"][0]["accessKey"] == "adm"
    assert out["findings"][0]["rank"] == 1
    assert out["findings"][0]["riskLevel"] == "high"
    assert [f["rank"] for f in out["findings"]] == [1, 2]


def test_a_disabled_but_privileged_account_is_called_out():
    conn = _conn(users={"old": {"status": "disabled", "policyName": "consoleAdmin"}})
    out = ops.diagnose_iam_exposure(conn)
    codes = [r["code"] for f in out["findings"] for r in f["reasons"]]
    assert "DISABLED_BUT_PRIVILEGED" in codes


def test_a_scoped_user_produces_no_finding():
    conn = _conn(users={"app": {"status": "enabled", "policyName": "bucket-scoped"}})
    out = ops.diagnose_iam_exposure(conn)
    assert out["findings"] == []
    assert out["usersScanned"] == 1


def test_groups_holding_policies_with_no_members_are_surfaced():
    conn = _conn(users={}, groups={"stale": {"members": [], "policyName": "readwrite"}})
    out = ops.diagnose_iam_exposure(conn)
    assert out["groupsWithPoliciesButNoMembers"] == ["stale"]


def test_an_empty_iam_surface_says_so():
    out = ops.diagnose_iam_exposure(_conn(users={}))
    assert out["findings"] == []
    assert "root credential is not an IAM user" in out["note"]


# ─── MCP layer: undo descriptors replay ───────────────────────────────────


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
    from mcp_server.tools import iam as gov

    conn = _conn()
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    return conn


def test_status_undo_restores_prior_and_replays(monkeypatch, recorded):
    from mcp_server.tools import iam as gov

    conn = _gov_conn(monkeypatch)
    conn.user_info.return_value = {"status": "enabled"}
    gov.set_user_status(access_key="alice", enabled=False)
    descriptor = recorded["d"]
    assert descriptor["params"] == {"access_key": "alice", "enabled": True}

    replay = _gov_conn(monkeypatch)
    replay.user_info.return_value = {"status": "disabled"}
    result = getattr(gov, descriptor["tool"])(**descriptor["params"])
    assert "error" not in result, f"undo replay failed: {result}"
    replay.set_user_status.assert_called_once_with("alice", True)


def test_policy_undo_detaches_only_what_was_attached(monkeypatch, recorded):
    from mcp_server.tools import iam as gov

    conn = _gov_conn(monkeypatch)
    conn.user_info.return_value = {"policyName": "readonly"}
    gov.attach_user_policy(access_key="alice", policies=["readwrite"])
    descriptor = recorded["d"]
    assert descriptor["tool"] == "detach_user_policy"
    assert descriptor["params"]["policies"] == ["readwrite"]

    replay = _gov_conn(monkeypatch)
    replay.user_info.return_value = {"policyName": "readonly,readwrite"}
    result = getattr(gov, descriptor["tool"])(**descriptor["params"])
    assert "error" not in result, f"undo replay failed: {result}"
    replay.set_user_policies.assert_called_once_with("alice", ["readwrite"], False)


def test_detach_undo_reattaches_and_replays(monkeypatch, recorded):
    from mcp_server.tools import iam as gov

    conn = _gov_conn(monkeypatch)
    conn.user_info.return_value = {"policyName": "readonly,readwrite"}
    gov.detach_user_policy(access_key="alice", policies=["readwrite"])
    descriptor = recorded["d"]
    assert descriptor["tool"] == "attach_user_policy"
    replay = _gov_conn(monkeypatch)
    replay.user_info.return_value = {"policyName": "readonly"}
    result = getattr(gov, descriptor["tool"])(**descriptor["params"])
    assert "error" not in result, f"undo replay failed: {result}"


def test_create_user_undo_removes_a_genuinely_new_account(monkeypatch, recorded):
    from mcp_server.tools import iam as gov

    conn = _gov_conn(monkeypatch)
    conn.user_info.side_effect = MinioApiError("no such user", status_code=404)
    gov.create_user(access_key="newkey", secret_key="s3cret-value-123")
    descriptor = recorded["d"]
    assert descriptor["tool"] == "remove_user"
    assert descriptor["params"] == {"access_key": "newkey"}

    replay = _gov_conn(monkeypatch)
    replay.user_info.return_value = {"status": "enabled"}
    result = getattr(gov, descriptor["tool"])(**descriptor["params"])
    assert "error" not in result, f"undo replay failed: {result}"
    replay.remove_user.assert_called_once_with("newkey")


# ─── previews run the guards ──────────────────────────────────────────────


def test_dry_run_reports_a_self_targeting_refusal(monkeypatch):
    from mcp_server.tools import iam as gov

    conn = _gov_conn(monkeypatch)
    for call in (
        lambda: gov.set_user_status(access_key=OWN_KEY, enabled=False, dry_run=True),
        lambda: gov.remove_user(access_key=OWN_KEY, dry_run=True),
        lambda: gov.detach_user_policy(access_key=OWN_KEY, policies=["p"], dry_run=True),
        lambda: gov.attach_user_policy(access_key=OWN_KEY, policies=["p"], dry_run=True),
    ):
        result = call()
        assert "error" in result, result
        assert "dryRun" not in result
    assert not conn.set_user_status.called
    assert not conn.remove_user.called
    assert not conn.set_user_policies.called


def test_dry_run_on_another_user_previews_without_writing(monkeypatch):
    from mcp_server.tools import iam as gov

    conn = _gov_conn(monkeypatch)
    result = gov.set_user_status(access_key="alice", enabled=False, dry_run=True)
    assert result["dryRun"] is True
    assert not conn.set_user_status.called


def test_a_failed_existence_probe_suppresses_the_undo_rather_than_deleting():
    """The destructive case. Any non-404 failure means we do not KNOW whether the
    account existed. Reading that as "did not exist" would record an undo that
    removes it — and remove_user cannot restore a credential MinIO no longer has.
    Unknown is its own state, and it suppresses the undo exactly as
    known-existing does."""
    conn = _conn()
    conn.user_info.side_effect = MinioApiError("admin API denied", status_code=403)
    out = writes.create_user(conn, "maybe-exists", "s3cret-value-123")
    assert out["priorState"]["existed"] is None
    assert "admin API denied" in out["probeError"]
    assert "could NOT be determined" in out["note"]
    # the account was still created — only the undo is withheld
    conn.add_user.assert_called_once_with("maybe-exists", "s3cret-value-123")


def test_no_undo_descriptor_when_the_probe_could_not_tell(monkeypatch, recorded):
    from mcp_server.tools import iam as gov

    conn = _gov_conn(monkeypatch)
    conn.user_info.side_effect = MinioApiError("connection reset", status_code=None)
    result = gov.create_user(access_key="maybe-exists", secret_key="s3cret-value-123")
    assert "error" not in result
    assert "d" not in recorded, "an unknown prior state must not yield a deleting undo"
