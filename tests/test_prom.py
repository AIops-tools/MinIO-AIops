"""Tests for the metrics exposition-text parser (minio_aiops.prom)."""

from __future__ import annotations

import pytest

from minio_aiops.prom import by_label, first_value, parse_metrics_text, sum_values

pytestmark = pytest.mark.unit

SAMPLE = """\
# HELP minio_cluster_bucket_total Total number of buckets in the cluster
# TYPE minio_cluster_bucket_total gauge
minio_cluster_bucket_total{server="m1:9000"} 4
minio_bucket_usage_total_bytes{bucket="alpha",server="m1:9000"} 1024
minio_bucket_usage_total_bytes{bucket="beta",server="m1:9000"} 2048
minio_cluster_capacity_usable_free_bytes 5e9
minio_node_drive_free_bytes{drive="/data/d1",server="m1:9000"} 100
garbage line without a value
minio_bad_value{x="y"} notanumber
"""


def test_parses_values_labels_and_scientific_notation():
    m = parse_metrics_text(SAMPLE)
    assert first_value(m, "minio_cluster_bucket_total") == 4.0
    assert first_value(m, "minio_cluster_capacity_usable_free_bytes") == 5e9
    samples = m["minio_bucket_usage_total_bytes"]
    assert len(samples) == 2
    assert samples[0]["labels"] == {"bucket": "alpha", "server": "m1:9000"}


def test_skips_comments_and_malformed_lines():
    m = parse_metrics_text(SAMPLE)
    assert "garbage" not in m
    assert "minio_bad_value" not in m


def test_helpers():
    m = parse_metrics_text(SAMPLE)
    assert sum_values(m, "minio_bucket_usage_total_bytes") == 3072.0
    assert sum_values(m, "does_not_exist") is None
    assert first_value(m, "does_not_exist") is None
    assert by_label(m, "minio_bucket_usage_total_bytes", "bucket") == {
        "alpha": 1024.0,
        "beta": 2048.0,
    }


def test_escaped_label_values():
    m = parse_metrics_text('metric{path="C:\\\\data",note="say \\"hi\\""} 1\n')
    labels = m["metric"][0]["labels"]
    assert labels["path"] == "C:\\data"
    assert labels["note"] == 'say "hi"'


def test_empty_and_hostile_input():
    assert parse_metrics_text("") == {}
    assert parse_metrics_text("\x00\x01\x02") == {}
