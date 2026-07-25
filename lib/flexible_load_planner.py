"""Pure planning helpers for deferrable household electrical loads.

The planner deliberately knows nothing about Home Connect, MQTT, or the ESS
runtime.  Given an earliest start, a load shape, and quarter-hour prices, it
returns a JSON-serialisable reservation.  The reservation's ``load_profile``
contains energy per grid-aligned quarter hour so callers can add it directly to
the optimizer's per-slot kWh load forecast.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import Sequence, Union


SLOT_MINUTES = 15
SLOT_SECONDS = SLOT_MINUTES * 60
DEFAULT_MIN_SAVINGS_EUR = 0.05

PowerShape = Union[float, Sequence[float]]


def _aware_datetime(value, field_name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid ISO-8601 datetime") from exc
    else:
        raise TypeError(f"{field_name} must be a datetime or ISO-8601 string")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return result


def _finite_number(value, field_name: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "a positive finite number" if positive else "finite"
        raise ValueError(f"{field_name} must be {qualifier}")
    return result


def _utc_key(value: datetime) -> int:
    """Return a stable whole-second key for an aware datetime."""
    return int(round(value.timestamp()))


def _floor_to_quarter(value: datetime) -> datetime:
    epoch = math.floor(value.timestamp() / SLOT_SECONDS) * SLOT_SECONDS
    return datetime.fromtimestamp(epoch, tz=value.tzinfo)


def _ceil_to_quarter(value: datetime) -> datetime:
    epoch = math.ceil(value.timestamp() / SLOT_SECONDS) * SLOT_SECONDS
    return datetime.fromtimestamp(epoch, tz=value.tzinfo)


def _add_elapsed(value: datetime, seconds: float) -> datetime:
    """Add real elapsed time while preserving the caller's timezone.

    Timestamp arithmetic avoids manufacturing nonexistent local times or
    stretching a program across daylight-saving transitions.
    """
    return datetime.fromtimestamp(value.timestamp() + seconds, tz=value.tzinfo)


def _normalise_prices(price_points: Sequence[dict]) -> dict[int, float]:
    parsed = []
    seen_keys = set()
    for index, point in enumerate(price_points or ()):
        if not isinstance(point, dict):
            raise TypeError(f"price_points[{index}] must be a mapping")
        try:
            start = _aware_datetime(point["start"], f"price_points[{index}].start")
            price = _finite_number(point["total"], f"price_points[{index}].total")
        except KeyError as exc:
            raise ValueError(f"price_points[{index}] is missing {exc.args[0]!r}") from exc
        if (start.minute % SLOT_MINUTES or start.second or start.microsecond):
            raise ValueError("price point starts must align to quarter-hour boundaries")
        key = _utc_key(start)
        if key in seen_keys:
            raise ValueError(f"duplicate price point at {start.isoformat()}")
        seen_keys.add(key)
        parsed.append((key, price))

    parsed.sort(key=lambda item: item[0])
    gaps = [
        parsed[index][0] - parsed[index - 1][0]
        for index in range(1, len(parsed))
        if parsed[index][0] > parsed[index - 1][0]
    ]
    # Tibber intentionally falls back to hourly data when native quarter-hour
    # prices are unavailable.  A flat hourly tariff applies to its four contained
    # quarters, allowing the same full-runtime scorer to degrade gracefully.
    hourly = bool(gaps and min(gaps) >= 3600)
    prices: dict[int, float] = {}
    for key, price in parsed:
        subdivisions = 4 if hourly else 1
        for offset in range(subdivisions):
            prices[key + offset * SLOT_SECONDS] = price
    return prices


def _normalise_power_shape(power_w: PowerShape, runtime_minutes: float) -> tuple[float, ...]:
    required_slots = int(math.ceil(runtime_minutes / SLOT_MINUTES))
    if isinstance(power_w, (str, bytes)):
        raise ValueError("power_w must be numeric or a quarter-hour power sequence")
    if isinstance(power_w, Sequence):
        shape = tuple(_finite_number(value, f"power_w[{index}]")
                      for index, value in enumerate(power_w))
        if len(shape) != required_slots:
            raise ValueError(
                "a power sequence must contain one value per runtime quarter hour "
                f"({required_slots} required)"
            )
        if any(value < 0 for value in shape) or not any(value > 0 for value in shape):
            raise ValueError("power_w sequence must be non-negative with some load")
        return shape
    return (_finite_number(power_w, "power_w", positive=True),) * required_slots


def _segments(start: datetime, runtime_minutes: float, shape: tuple[float, ...]):
    """Yield consecutive load-shape segments, including a partial final slot."""
    remaining_seconds = runtime_minutes * 60.0
    cursor = start
    for power in shape:
        duration_seconds = min(SLOT_SECONDS, remaining_seconds)
        if duration_seconds <= 0:
            break
        end = _add_elapsed(cursor, duration_seconds)
        yield cursor, end, power
        remaining_seconds -= duration_seconds
        cursor = end


def _profile_and_cost(
    start: datetime,
    runtime_minutes: float,
    shape: tuple[float, ...],
    prices: dict[int, float],
) -> tuple[list[dict], float | None]:
    """Build grid-aligned load buckets and calculate full-program cost.

    ``None`` cost means at least one price needed by the run was unavailable.
    The profile remains useful and complete in that case.
    """
    energy_by_slot: dict[int, float] = {}
    cost = 0.0
    cost_available = True

    for segment_start, segment_end, power in _segments(start, runtime_minutes, shape):
        cursor = segment_start
        while cursor < segment_end:
            price_slot_start = _floor_to_quarter(cursor)
            price_slot_end = _add_elapsed(price_slot_start, SLOT_SECONDS)
            overlap_end = min(segment_end, price_slot_end)
            duration_h = (overlap_end - cursor).total_seconds() / 3600.0
            energy_kwh = power / 1000.0 * duration_h
            key = _utc_key(price_slot_start)
            energy_by_slot[key] = energy_by_slot.get(key, 0.0) + energy_kwh
            if key not in prices:
                cost_available = False
            else:
                cost += energy_kwh * prices[key]
            cursor = overlap_end

    profile = []
    for key in sorted(energy_by_slot):
        slot_start = datetime.fromtimestamp(key, tz=start.tzinfo)
        energy_kwh = energy_by_slot[key]
        profile.append({
            "start": slot_start.isoformat(),
            "end": _add_elapsed(slot_start, SLOT_SECONDS).isoformat(),
            "load_w": round(energy_kwh / (SLOT_MINUTES / 60.0) * 1000.0, 3),
            "energy_kwh": round(energy_kwh, 6),
        })
    return profile, round(cost, 6) if cost_available else None


def _overnight_deadline(
    earliest_start: datetime,
    completion_hour: int,
    completion_minute: int,
) -> datetime:
    deadline_clock = time(completion_hour, completion_minute)
    # Requests after midnight but before the deadline belong to the overnight
    # window opened the previous evening.  Requests from the evening cutoff
    # onward target tomorrow morning.
    deadline_date = earliest_start.date()
    if earliest_start.timetz().replace(tzinfo=None) >= deadline_clock:
        deadline_date += timedelta(days=1)
    return datetime.combine(deadline_date, deadline_clock, tzinfo=earliest_start.tzinfo)


def plan_flexible_load(
    *,
    device: str,
    earliest_start: datetime,
    runtime_minutes: float,
    power_w: PowerShape,
    price_points: Sequence[dict],
    min_savings_eur: float = DEFAULT_MIN_SAVINGS_EUR,
    daytime_max_delay_hours: float = 5.0,
    evening_start_hour: int = 19,
    overnight_completion_hour: int = 5,
    overnight_completion_minute: int = 30,
) -> dict:
    """Choose an immediate or economically worthwhile deferred appliance run.

    Before 19:00 (configurable), delayed starts are limited to five hours from
    the request.  From 19:00 onward, a delayed run must *complete* by the next
    morning's 05:30 deadline.  The immediate option is retained unless a later
    contiguous run has complete price coverage and saves at least
    ``min_savings_eur`` across the whole load profile.

    ``power_w`` may be a constant power or one value per runtime quarter hour.
    This keeps today's dishwasher/dryer use simple while allowing a measured
    shape, and leaves the same API usable for a constant-power EV session.
    """
    if not isinstance(device, str) or not device.strip():
        raise ValueError("device must be a non-empty string")
    earliest = _aware_datetime(earliest_start, "earliest_start")
    runtime = _finite_number(runtime_minutes, "runtime_minutes", positive=True)
    savings_threshold = _finite_number(min_savings_eur, "min_savings_eur")
    max_delay = _finite_number(
        daytime_max_delay_hours, "daytime_max_delay_hours", positive=True)
    if savings_threshold < 0:
        raise ValueError("min_savings_eur cannot be negative")
    if not 0 <= evening_start_hour <= 23:
        raise ValueError("evening_start_hour must be between 0 and 23")
    if not 0 <= overnight_completion_hour <= 23:
        raise ValueError("overnight_completion_hour must be between 0 and 23")
    if not 0 <= overnight_completion_minute <= 59:
        raise ValueError("overnight_completion_minute must be between 0 and 59")

    prices = _normalise_prices(price_points)
    shape = _normalise_power_shape(power_w, runtime)
    immediate_profile, immediate_cost = _profile_and_cost(
        earliest, runtime, shape, prices)

    completion_clock = time(overnight_completion_hour, overnight_completion_minute)
    local_clock = earliest.timetz().replace(tzinfo=None)
    overnight = earliest.hour >= evening_start_hour or local_clock < completion_clock
    if overnight:
        latest_completion = _overnight_deadline(
            earliest, overnight_completion_hour, overnight_completion_minute)
        latest_start = _add_elapsed(latest_completion, -runtime * 60.0)
        policy = "overnight_completion_deadline"
    else:
        latest_completion = None
        latest_start = _add_elapsed(earliest, max_delay * 3600.0)
        policy = "daytime_max_start_delay"

    best_start = earliest
    best_profile = immediate_profile
    best_cost = immediate_cost
    candidate = _ceil_to_quarter(earliest)
    if candidate <= earliest:
        candidate = _add_elapsed(candidate, SLOT_SECONDS)
    valid_delayed_count = 0
    while candidate.timestamp() <= latest_start.timestamp():
        profile, cost = _profile_and_cost(candidate, runtime, shape, prices)
        if cost is not None:
            valid_delayed_count += 1
            if best_cost is None or cost < best_cost - 1e-12:
                best_start, best_profile, best_cost = candidate, profile, cost
        candidate = _add_elapsed(candidate, SLOT_SECONDS)

    decision = "immediate"
    selected_start = earliest
    selected_profile = immediate_profile
    selected_cost = immediate_cost
    saving = None
    if (immediate_cost is not None and best_start.timestamp() > earliest.timestamp()
            and best_cost is not None):
        saving = round(immediate_cost - best_cost, 6)
        if saving + 1e-12 >= savings_threshold:
            decision = "delayed"
            selected_start = best_start
            selected_profile = best_profile
            selected_cost = best_cost

    if immediate_cost is None:
        reason = "insufficient_immediate_price_horizon"
    elif decision == "delayed":
        reason = "material_saving"
    elif not valid_delayed_count:
        reason = "no_valid_delayed_window"
    else:
        reason = "saving_below_threshold"

    end = _add_elapsed(selected_start, runtime * 60.0)
    energy_kwh = sum(slot["energy_kwh"] for slot in selected_profile)
    average_load_kw = energy_kwh / (runtime / 60.0)
    comfort_window = {
        "policy": policy,
        "earliest_start": earliest.isoformat(),
        "latest_start": latest_start.isoformat(),
        "latest_completion": (
            latest_completion.isoformat() if latest_completion is not None else None
        ),
    }
    return {
        "schema_version": 1,
        "kind": "flexible_load_reservation",
        "device": device.strip(),
        "decision": decision,
        "reason": reason,
        "start": selected_start.isoformat(),
        "end": end.isoformat(),
        "runtime_minutes": runtime,
        "load_kw": round(average_load_kw, 6),
        "energy_kwh": round(energy_kwh, 6),
        "estimated_cost_eur": selected_cost,
        "immediate_cost_eur": immediate_cost,
        "estimated_savings_eur": saving,
        "min_savings_eur": savings_threshold,
        "comfort_window": comfort_window,
        "load_profile": selected_profile,
    }


__all__ = ["DEFAULT_MIN_SAVINGS_EUR", "SLOT_MINUTES", "plan_flexible_load"]
