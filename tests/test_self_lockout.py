"""Refuse a bucket policy that revokes this tool's own ability to set policies.

``set_bucket_policy`` validated only that the document was a dict containing
``Statement`` — nothing looked at ``Effect``. In S3 an explicit Deny beats every
Allow, so a statement denying ``s3:PutBucketPolicy`` to the configured access
key APPLIES, and the undo (which replays the prior policy) is then denied in
turn. This tool has no IAM surface at all, so a bucket policy is the only way it
can revoke its own access — and there is no way back from inside the tool.

This only bites when the tool is configured with a non-root key:
``MINIO_ROOT_USER`` bypasses policy evaluation entirely. The setup guide neither
mandates nor forbids root, so both configurations are in the field and the guard
has to assume the one that can be hurt.

Detection is purely local — the submitted JSON plus ``target.access_key`` — so
there is no round trip and no unknown-identity case. It must be EXACT: ordinary
Deny statements aimed at other principals, and every Allow policy, keep working.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import bucket_writes as ops
from minio_aiops.ops.bucket_writes import SelfLockout

pytestmark = pytest.mark.unit

OUR_KEY = "aiops-svc"
PRIOR_POLICY = '{"Version":"2012-10-17","Statement":[{"Sid":"old"}]}'


def _conn(access_key: str = OUR_KEY):
    conn = MagicMock(name="conn")
    conn.target.access_key = access_key
    conn.get_bucket_policy.return_value = PRIOR_POLICY
    return conn


def _policy(*statements: dict) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": list(statements)})


def _deny(action, principal=OUR_KEY) -> dict:
    return {"Effect": "Deny", "Principal": {"AWS": principal},
            "Action": action, "Resource": "arn:aws:s3:::data-bkt/*"}


# ── the shapes that revoke our own PutBucketPolicy ──────────────────────────


def test_explicit_deny_on_put_bucket_policy_is_refused():
    conn = _conn()
    with pytest.raises(SelfLockout, match="explicit Deny"):
        ops.set_bucket_policy(conn, "data-bkt", _policy(_deny("s3:PutBucketPolicy")))
    conn.set_bucket_policy.assert_not_called()


def test_a_wildcard_action_covers_it_too():
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(_conn(), "data-bkt", _policy(_deny("s3:*")))


def test_a_bare_star_action_covers_it_too():
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(_conn(), "data-bkt", _policy(_deny("*")))


def test_a_prefix_glob_covers_it_too():
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(_conn(), "data-bkt", _policy(_deny("s3:PutBucket*")))


def test_a_wildcard_principal_covers_us():
    """Principal '*' denies everyone, this tool included."""
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(
            _conn(), "data-bkt", _policy(_deny("s3:PutBucketPolicy", principal="*"))
        )


def test_an_arn_principal_ending_in_our_key_is_matched():
    arn = f"arn:aws:iam::123456789012:user/{OUR_KEY}"
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(
            _conn(), "data-bkt", _policy(_deny("s3:PutBucketPolicy", principal=arn))
        )


def test_an_action_list_is_scanned_not_just_a_scalar():
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(
            _conn(), "data-bkt",
            _policy(_deny(["s3:GetObject", "s3:PutBucketPolicy"])),
        )


def test_the_effect_check_is_case_insensitive():
    statement = _deny("s3:PutBucketPolicy")
    statement["Effect"] = "DENY"
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(_conn(), "data-bkt", _policy(statement))


def test_the_refusal_explains_deny_precedence_and_the_way_out():
    with pytest.raises(SelfLockout) as ei:
        ops.set_bucket_policy(_conn(), "data-bkt", _policy(_deny("s3:PutBucketPolicy")))
    msg = str(ei.value)
    assert "beats every Allow" in msg, "must explain why the Deny wins"
    assert "undo" in msg, "must name the concrete failure"
    assert "mc" in msg or "admin credential" in msg, "must offer a route that works"


# ── exactness: everything else keeps working ────────────────────────────────


def test_a_deny_aimed_at_someone_else_is_allowed():
    """The common case — locking OTHER principals out — must still work."""
    conn = _conn()
    policy = _policy(_deny("s3:PutBucketPolicy", principal="arn:aws:iam::1:user/intern"))
    out = ops.set_bucket_policy(conn, "data-bkt", policy)
    assert out["priorState"]["policyJson"] == PRIOR_POLICY
    conn.set_bucket_policy.assert_called_once()


def test_a_deny_on_an_unrelated_action_is_allowed():
    conn = _conn()
    out = ops.set_bucket_policy(conn, "data-bkt", _policy(_deny("s3:DeleteObject")))
    assert "priorState" in out
    conn.set_bucket_policy.assert_called_once()


def test_an_allow_on_put_bucket_policy_is_allowed():
    """Only Deny is dangerous; an Allow to ourselves is the normal case."""
    conn = _conn()
    statement = _deny("s3:PutBucketPolicy")
    statement["Effect"] = "Allow"
    out = ops.set_bucket_policy(conn, "data-bkt", _policy(statement))
    assert "priorState" in out


def test_an_ordinary_public_read_policy_is_untouched_by_the_guard():
    policy = _policy({"Effect": "Allow", "Principal": "*",
                      "Action": "s3:GetObject", "Resource": "arn:aws:s3:::data-bkt/*"})
    conn = _conn()
    out = ops.set_bucket_policy(conn, "data-bkt", policy)
    assert "priorState" in out


# ── the guard leaves malformed input to the caller's own validation ─────────


def test_malformed_json_still_raises_the_original_error():
    with pytest.raises(ValueError, match="not valid JSON"):
        ops.set_bucket_policy(_conn(), "data-bkt", "{broken")


def test_a_document_without_statement_still_raises_the_original_error():
    with pytest.raises(ValueError, match="Statement"):
        ops.set_bucket_policy(_conn(), "data-bkt", '{"foo": 1}')


def test_a_non_string_access_key_does_not_crash_the_guard():
    """A MagicMock/None access_key must degrade to 'no key', not explode."""
    conn = MagicMock(name="conn")  # target.access_key is itself a MagicMock
    conn.get_bucket_policy.return_value = PRIOR_POLICY
    out = ops.set_bucket_policy(conn, "data-bkt", _policy(_deny("s3:PutBucketPolicy")))
    assert "priorState" in out, "an unidentifiable key must not block the write"


def test_a_wildcard_principal_is_caught_even_with_no_known_key():
    """Deny-everyone hits us whether or not we can name ourselves."""
    conn = MagicMock(name="conn")
    conn.target.access_key = ""
    with pytest.raises(SelfLockout):
        ops.set_bucket_policy(
            conn, "data-bkt", _policy(_deny("s3:PutBucketPolicy", principal="*"))
        )


def test_the_guard_is_reachable_without_performing_the_write():
    """The MCP wrapper calls this ahead of its dry_run return."""
    conn = _conn()
    ops.guard_set_bucket_policy(conn, _policy(_deny("s3:DeleteObject")))
    with pytest.raises(SelfLockout):
        ops.guard_set_bucket_policy(conn, _policy(_deny("s3:PutBucketPolicy")))
    conn.set_bucket_policy.assert_not_called()


def test_self_lockout_is_a_valueerror():
    """CLI/MCP error handling keys off ValueError; keep it in that family."""
    assert issubclass(SelfLockout, ValueError)


# ── set_lifecycle: honest wording, not a guard ──────────────────────────────


def test_set_lifecycle_states_that_expired_objects_do_not_come_back():
    """No guard is possible here — the undo restores the RULE, not the data.

    Matches the wording delete_queue uses in the sibling queue tool: the queue
    comes back, its messages do not.
    """
    conn = MagicMock(name="conn")
    conn.get_bucket_lifecycle_xml.return_value = "<LifecycleConfiguration/>"
    out = ops.set_lifecycle(conn, "data-bkt", expire_days=30)
    assert "NOT restored" in out["note"]
    assert "objects" in out["note"].lower()
