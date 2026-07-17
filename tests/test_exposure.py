"""Bucket exposure audit tests: policy parsing, scoring, ranking, resilience."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from minio_aiops.ops import exposure as ops

pytestmark = pytest.mark.unit

PUBLIC_READ_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]},
                   "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::pub/*"]}],
})
PUBLIC_WRITE_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": "*",
                   "Action": ["s3:PutObject", "s3:DeleteObject"],
                   "Resource": ["arn:aws:s3:::drop/*"]}],
})
PRIVATE_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["arn:aws:iam::1:user/x"]},
                   "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::priv/*"]}],
})


def test_anonymous_statement_parsing():
    assert ops._anonymous_statements(PUBLIC_READ_POLICY) == (True, False)
    read, write = ops._anonymous_statements(PUBLIC_WRITE_POLICY)
    assert write is True
    assert ops._anonymous_statements(PRIVATE_POLICY) == (False, False)
    assert ops._anonymous_statements(None) == (False, False)
    assert ops._anonymous_statements("not json{") == (False, False)


def _conn(policies: dict, versioning="Enabled", encryption=None, lifecycle=None):
    conn = MagicMock(name="conn")
    conn.list_buckets.return_value = [{"name": n} for n in policies]
    conn.get_bucket_policy.side_effect = lambda b: policies[b]
    conn.get_bucket_versioning.return_value = versioning
    conn.get_bucket_encryption.return_value = encryption
    conn.get_bucket_lifecycle.return_value = lifecycle
    return conn


def test_public_write_scores_highest_and_ranks_first():
    conn = _conn({"priv": PRIVATE_POLICY, "drop": PUBLIC_WRITE_POLICY,
                  "pub": PUBLIC_READ_POLICY},
                 versioning="Enabled",
                 encryption={"sseAlgorithm": "AES256"},
                 lifecycle=[{"ruleId": "r", "status": "Enabled"}])
    out = ops.bucket_exposure_audit(conn)
    assert out["bucketsAudited"] == 3
    assert out["findings"][0]["bucket"] == "drop"
    assert out["findings"][0]["riskLevel"] == "high"
    reasons = {r["reason"] for r in out["findings"][0]["reasons"]}
    assert "PUBLIC_WRITE_POLICY" in reasons
    # A fully hardened private bucket produces no finding at all.
    assert all(f["bucket"] != "priv" for f in out["findings"])


def test_hygiene_gaps_score_without_public_policy():
    conn = _conn({"lax": None}, versioning="Off", encryption=None, lifecycle=None)
    out = ops.bucket_exposure_audit(conn)
    finding = out["findings"][0]
    reasons = {r["reason"] for r in finding["reasons"]}
    assert reasons == {"NO_DEFAULT_ENCRYPTION", "VERSIONING_OFF", "NO_LIFECYCLE"}
    assert finding["riskScore"] == (
        ops.SCORE_NO_ENCRYPTION + ops.SCORE_VERSIONING_OFF + ops.SCORE_NO_LIFECYCLE
    )
    assert finding["riskLevel"] == "medium"
    assert all(r["cause"] and r["suggestedAction"] for r in finding["reasons"])


def test_per_probe_failure_degrades_not_raises():
    conn = _conn({"b1": PRIVATE_POLICY})
    conn.get_bucket_encryption.side_effect = RuntimeError("admin denied")
    out = ops.bucket_exposure_audit(conn)
    # The bucket may still have findings from other probes; the audit survives.
    assert out["bucketsAudited"] == 1


def test_audit_resilient_to_list_failure():
    conn = MagicMock(name="conn")
    conn.list_buckets.side_effect = RuntimeError("down")
    assert "error" in ops.bucket_exposure_audit(conn)
