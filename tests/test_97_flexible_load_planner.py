"""Adversarial tests for the pure flexible-load planner."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lib.flexible_load_planner import plan_flexible_load


def _prices(start, count, default=0.30, overrides=None):
    overrides = overrides or {}
    return [
        {
            "start": start + timedelta(minutes=15 * index),
            "total": overrides.get(start + timedelta(minutes=15 * index), default),
        }
        for index in range(count)
    ]


def test_costs_the_full_contiguous_runtime_not_only_the_cheapest_first_slot():
    now = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
    superficially_cheap = now + timedelta(hours=1)
    genuinely_cheap = now + timedelta(hours=2)
    overrides = {superficially_cheap: -0.20}
    overrides.update({
        superficially_cheap + timedelta(minutes=15 * index): 0.50
        for index in range(1, 4)
    })
    overrides.update({
        genuinely_cheap + timedelta(minutes=15 * index): 0.10
        for index in range(4)
    })

    plan = plan_flexible_load(
        device="dishwasher",
        earliest_start=now,
        runtime_minutes=60,
        power_w=1000,
        price_points=_prices(now, 25, overrides=overrides),
    )

    assert plan["decision"] == "delayed"
    assert plan["start"] == genuinely_cheap.isoformat()
    assert plan["estimated_cost_eur"] == pytest.approx(0.10)


def test_small_saving_does_not_create_household_inconvenience():
    now = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
    later = now + timedelta(hours=2)
    overrides = {now + timedelta(minutes=15 * index): 0.20 for index in range(4)}
    overrides.update({later + timedelta(minutes=15 * index): 0.18 for index in range(4)})

    plan = plan_flexible_load(
        device="dishwasher",
        earliest_start=now,
        runtime_minutes=60,
        power_w=1000,
        price_points=_prices(now, 25, overrides=overrides),
        min_savings_eur=0.05,
    )

    assert plan["decision"] == "immediate"
    assert plan["reason"] == "saving_below_threshold"
    assert plan["estimated_savings_eur"] == pytest.approx(0.02)


def test_partial_quarters_are_energy_weighted_and_json_serialisable():
    price_start = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    now = price_start + timedelta(minutes=7)
    plan = plan_flexible_load(
        device="dryer",
        earliest_start=now,
        runtime_minutes=20,
        power_w=900,
        price_points=_prices(price_start, 4, default=0.20),
    )

    assert plan["decision"] == "immediate"
    assert plan["energy_kwh"] == pytest.approx(0.30)
    assert plan["load_kw"] == pytest.approx(0.90)
    assert [slot["energy_kwh"] for slot in plan["load_profile"]] == pytest.approx(
        [0.12, 0.18]
    )
    assert [slot["load_w"] for slot in plan["load_profile"]] == pytest.approx(
        [480, 720]
    )
    json.dumps(plan)


def test_missing_immediate_price_never_justifies_a_delay():
    now = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
    future_prices = _prices(now + timedelta(hours=1), 20, default=-0.20)

    plan = plan_flexible_load(
        device="dishwasher",
        earliest_start=now,
        runtime_minutes=60,
        power_w=1000,
        price_points=future_prices,
    )

    assert plan["decision"] == "immediate"
    assert plan["reason"] == "insufficient_immediate_price_horizon"
    assert plan["estimated_cost_eur"] is None


def test_hourly_fallback_prices_are_expanded_for_full_runtime_scoring():
    now = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
    hourly = [
        {"start": now + timedelta(hours=index), "total": price}
        for index, price in enumerate([0.40, 0.35, 0.10, 0.30, 0.30, 0.30])
    ]

    plan = plan_flexible_load(
        device="dishwasher",
        earliest_start=now,
        runtime_minutes=60,
        power_w=1000,
        price_points=hourly,
        min_savings_eur=0.05,
    )

    assert plan["decision"] == "delayed"
    assert plan["start"] == (now + timedelta(hours=2)).isoformat()
    assert plan["estimated_cost_eur"] == pytest.approx(0.10)


@pytest.mark.parametrize(
    "earliest,price_start",
    [
        (datetime(2026, 1, 15, 10), datetime(2026, 1, 15, 10, tzinfo=timezone.utc)),
        (datetime(2026, 1, 15, 10, tzinfo=timezone.utc), datetime(2026, 1, 15, 10)),
    ],
)
def test_naive_request_or_price_timestamps_are_rejected(earliest, price_start):
    with pytest.raises(ValueError, match="timezone-aware"):
        plan_flexible_load(
            device="dryer",
            earliest_start=earliest,
            runtime_minutes=60,
            power_w=1000,
            price_points=_prices(price_start, 24),
        )


def test_after_midnight_request_uses_same_morning_0530_deadline():
    now = datetime(2026, 1, 16, 1, tzinfo=timezone.utc)
    valid_start = now.replace(hour=4, minute=30)
    invalid_start = now.replace(hour=5, minute=0)
    overrides = {valid_start + timedelta(minutes=15 * i): 0.10 for i in range(4)}
    overrides.update({invalid_start + timedelta(minutes=15 * i): -0.20 for i in range(4)})

    plan = plan_flexible_load(
        device="dryer",
        earliest_start=now,
        runtime_minutes=60,
        power_w=2000,
        price_points=_prices(now, 24, overrides=overrides),
        min_savings_eur=0,
    )

    assert plan["start"] == valid_start.isoformat()
    assert datetime.fromisoformat(plan["end"]) <= now.replace(hour=5, minute=30)
    assert plan["comfort_window"]["latest_completion"] == now.replace(
        hour=5, minute=30
    ).isoformat()


def test_overnight_deadline_and_candidates_remain_valid_across_dst_fallback():
    zone = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 10, 24, 20, tzinfo=zone)
    # Build real contiguous quarters through the repeated DST hour.
    starts = [
        datetime.fromtimestamp(now.timestamp() + 900 * index, tz=zone)
        for index in range(48)
    ]
    cheap_start = datetime(2026, 10, 25, 3, 30, tzinfo=zone)
    prices = [
        {"start": start, "total": 0.05 if cheap_start <= start < cheap_start + timedelta(hours=1) else 0.30}
        for start in starts
    ]

    plan = plan_flexible_load(
        device="dishwasher",
        earliest_start=now,
        runtime_minutes=60,
        power_w=1000,
        price_points=prices,
        min_savings_eur=0,
    )

    selected = datetime.fromisoformat(plan["start"])
    deadline = datetime.fromisoformat(plan["comfort_window"]["latest_completion"])
    assert selected.timestamp() >= now.timestamp()
    assert datetime.fromisoformat(plan["end"]).timestamp() <= deadline.timestamp()
    assert deadline.hour == 5 and deadline.minute == 30
