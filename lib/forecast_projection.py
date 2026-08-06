"""Pure helpers for accounting for the unelapsed portion of a plan slot.

The optimiser intentionally retains the price slot containing ``now`` so it can
control the system immediately. Daily economics must not treat that whole slot
as future: the live daily counters already contain its elapsed portion.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


DEFAULT_SLOT_DURATION_H = 0.25


def slot_remaining_fraction(
    slot: dict[str, Any],
    *,
    as_of: datetime,
    default_duration_h: float = DEFAULT_SLOT_DURATION_H,
) -> float:
    """Return the unelapsed fraction of ``slot`` at ``as_of``.

    Timestamp-less legacy records retain their complete value. A malformed
    duration follows the same safe, backwards-compatible behaviour.
    """
    start = _as_datetime(slot.get("time"))
    if start is None:
        return 1.0

    try:
        duration_h = float(slot.get("duration_h", default_duration_h))
    except (TypeError, ValueError):
        duration_h = float(default_duration_h)
    if duration_h <= 0:
        return 1.0

    comparable_as_of = _match_timezone(as_of, start)
    end = start + timedelta(hours=duration_h)
    if comparable_as_of <= start:
        return 1.0
    if comparable_as_of >= end:
        return 0.0
    return max(0.0, min(1.0, (end - comparable_as_of).total_seconds()
                        / (duration_h * 3600.0)))


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _match_timezone(value: datetime, reference: datetime) -> datetime:
    """Make legacy naive and aware timestamps safely comparable."""
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value
