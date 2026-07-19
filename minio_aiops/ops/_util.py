"""Shared helpers for MinIO ops modules.

All API-returned text reaches the caller only after ``sanitize()`` (output
hygiene: control/format-char stripping + truncation). ``check_bucket_name``
fail-fast-validates every agent-supplied bucket name at the ops boundary so a
hostile value (path separators, control chars, absurd length) can never reach
the S3 request path.
"""

from __future__ import annotations

import re
from typing import Any

from minio_aiops.governance import opt_str, sanitize

# S3 bucket naming rules (the strict, portable subset): 3-63 chars, lowercase
# letters / digits / dots / hyphens, starts+ends alphanumeric, no "..".
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def check_bucket_name(name: Any) -> str:
    """Validate an agent-supplied bucket name; returns it or raises ValueError.

    This is the injection gate for every read/write that takes a bucket name:
    S3 names can never contain ``/``, whitespace, or control characters, so a
    traversal-style value like ``../admin`` is rejected before any request.
    """
    value = str(name or "")
    if not _BUCKET_RE.match(value) or ".." in value or _IPV4_RE.match(value):
        raise ValueError(
            f"Invalid bucket name {sanitize(value, 80)!r}: must be 3-63 chars of "
            f"lowercase letters, digits, dots, hyphens; start/end alphanumeric; "
            f"no '..'; not an IP address."
        )
    return value


def s(value: Any, limit: int = 256) -> str:
    """Sanitize an arbitrary value to a bounded, injection-safe string."""
    return sanitize(str(value if value is not None else ""), limit)


def opt_s(value: Any, limit: int = 256) -> str | None:
    """Sanitize an OPTIONAL API field, preserving the difference absent/empty.

    ``s()`` folds a missing value into ``""``, which downstream reads as "the
    field exists and is empty". For a field the API may simply not return
    (a bucket with no creation date, an upload with no initiated time) that is
    a fabricated fact. This returns ``None`` — JSON ``null`` — instead.
    """
    return opt_str(value, limit)


def bytes_h(n: Any) -> str:
    """Human-readable bytes (best-effort; returns '' for non-numeric)."""
    if not isinstance(n, (int, float)):
        return ""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(size) < 1024.0:
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}EiB"


def pct(used: Any, total: Any) -> float | None:
    """used/total as a 0-1 ratio (None when not computable)."""
    if not isinstance(used, (int, float)) or not isinstance(total, (int, float)) or not total:
        return None
    return round(float(used) / float(total), 4)
