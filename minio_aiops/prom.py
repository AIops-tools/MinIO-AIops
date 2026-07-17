"""Minimal metrics-exposition-text parser for the MinIO cluster metrics endpoint.

Parses the plain-text exposition format (``name{label="v",...} value``) into
``{metricName: [{"labels": {...}, "value": float}, ...]}``. Deliberately tiny:
no dependency on a metrics client library, no histograms/summaries semantics —
the RCA analyses only need gauges/counters and their labels.

Defensive by design: malformed lines are skipped, never raised — a metrics
probe must survive a half-written scrape.
"""

from __future__ import annotations

import re

# name{labels} value [timestamp]  — labels part optional.
_LINE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r"\s+(?P<value>[^\s]+)"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')

_MAX_LINES = 200_000  # hard bound: never let a hostile payload spin forever


def _parse_labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    return {
        key: value.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        for key, value in _LABEL_RE.findall(raw)
    }


def parse_metrics_text(text: str) -> dict[str, list[dict]]:
    """Parse exposition text into {name: [{"labels": {...}, "value": float}]}."""
    out: dict[str, list[dict]] = {}
    for line in text.splitlines()[:_MAX_LINES]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        out.setdefault(match.group("name"), []).append(
            {"labels": _parse_labels(match.group("labels")), "value": value}
        )
    return out


def first_value(
    metrics: dict[str, list[dict]], name: str, default: float | None = None
) -> float | None:
    """The first sample's value for ``name`` (metrics with a single series)."""
    samples = metrics.get(name) or []
    return samples[0]["value"] if samples else default


def sum_values(metrics: dict[str, list[dict]], name: str) -> float | None:
    """Sum across all label series of ``name`` (None when the metric is absent)."""
    samples = metrics.get(name) or []
    if not samples:
        return None
    return float(sum(s["value"] for s in samples))


def by_label(metrics: dict[str, list[dict]], name: str, label: str) -> dict[str, float]:
    """Map ``label`` value -> sample value for every series of ``name``."""
    return {
        s["labels"].get(label, ""): s["value"]
        for s in metrics.get(name) or []
        if label in s["labels"]
    }
