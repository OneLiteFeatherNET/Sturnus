"""Shared time helpers for the domain layer.

Private module: not part of the public domain API, only imported by
sibling domain modules that need the same small check.
"""

from __future__ import annotations

from datetime import datetime


def require_aware(value: datetime) -> datetime:
    """Returns `value` unchanged, or raises if it is a naive datetime."""
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value
