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


#: S3 caps a key at 1024 **bytes** (not characters) after UTF-8 encoding.
_MAX_OBJECT_KEY_BYTES = 1024


def check_object_name(name: Any) -> str:
    """Validate an agent-supplied object key; returns it or raises ValueError.

    The bucket-name gate above can be strict because S3 bucket names are a tiny
    alphabet. Object keys are the opposite: any UTF-8 sequence is legal, so this
    only rejects what S3 itself cannot carry — an empty key, one over the
    1024-**byte** limit, and control characters (which would survive into the
    signed request and into every audit row and log line downstream).
    """
    value = str(name or "")
    if not value:
        raise ValueError("Object key must not be empty.")
    encoded = len(value.encode("utf-8", errors="surrogatepass"))
    if encoded > _MAX_OBJECT_KEY_BYTES:
        raise ValueError(
            f"Object key is {encoded} bytes; S3 allows at most "
            f"{_MAX_OBJECT_KEY_BYTES} bytes after UTF-8 encoding."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(
            f"Object key {sanitize(value, 80)!r} contains control characters. "
            f"List the bucket (bucket_objects) and copy the key from there."
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


def as_int(value: Any) -> int | None:
    """Coerce a Prometheus sample value to ``int``, preserving absence.

    Prometheus exposition is float-typed on the wire, so byte counts and object
    counts arrive as ``1500000.0`` / ``3.0``. They are integers in every sense
    that matters to a reader, and rendering them with a ``.0`` invites the
    question of whether the number was rounded.

    ``None`` stays ``None``: these come from ``.get()`` lookups where a missing
    sample is a real outcome, and an unknown count must not become ``0``.
    """
    if value is None or isinstance(value, bool):  # bool subclasses int; not a quantity
        return None
    if isinstance(value, int):
        return value  # already exact — never round-trip through float64
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


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
