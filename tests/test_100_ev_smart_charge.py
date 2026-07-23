"""Contract and adversarial tests for the pure EV smart-charge planner."""

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lib.ev_smart_charge import (
    clear_job,
    create_job,
    delete_job,
    load_job,
    load_plan_snapshot,
    overlay_load_forecast,
    plan_charge,
    plan_ev_charge,
    save_job,
    save_plan_snapshot,
    update_job_status,
)


def _slots(start, prices, *, pv=None):
    pv = pv or {}
    return [
        {
            "start": start + timedelta(minutes=15 * index),
            "grid_price_eur_per_kwh": price,
            "pv_surplus_kwh": pv.get(index, 0.0),
            "pv_opportunity_cost_eur_per_kwh": 0.04,
        }
        for index, price in enumerate(prices)
    ]


def _job(now, *, current=20, target=80, ready_hours=8):
    return create_job(
        job_id="test-job",
        current_soc=current,
        target_soc=target,
        ready_by=now + timedelta(hours=ready_hours),
        now=now,
    )


def test_job_round_trips_timezone_aware_and_replaces_atomically(tmp_path, monkeypatch):
    now = datetime(2026, 7, 20, 20, tzinfo=ZoneInfo("Europe/Amsterdam"))
    path = tmp_path / "state" / "ev-charge-job.json"
    job = _job(now)
    replacements = []
    real_replace = os.replace

    def observed_replace(source, target):
        replacements.append((source, target))
        assert os.path.dirname(source) == os.path.dirname(target)
        assert json.loads(open(source, encoding="utf-8").read())["id"] == "test-job"
        real_replace(source, target)

    monkeypatch.setattr("lib.ev_smart_charge.os.replace", observed_replace)
    save_job(job, path=path)

    assert replacements
    assert load_job(path=path) == job
    assert datetime.fromisoformat(load_job(path=path)["ready_by"]).utcoffset() == timedelta(hours=2)
    assert not list(path.parent.glob("*.tmp"))


def test_one_active_job_is_replaced_and_can_be_cleared(tmp_path):
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    path = tmp_path / "ev-charge-job.json"
    save_job(_job(now), path=path)
    replacement = create_job(
        job_id="replacement",
        current_soc=40,
        target_soc=70,
        ready_by=now + timedelta(hours=5),
        now=now,
    )
    save_job(replacement, path=path)

    assert load_job(path=path)["id"] == "replacement"
    assert clear_job(path=path) is True
    assert clear_job(path=path) is False
    assert load_job(path=path) is None


@pytest.mark.parametrize("current,target", [(-1, 80), (101, 80), (20, 49), (20, 101)])
def test_soc_values_are_validated(current, target):
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="SoC"):
        create_job(
            current_soc=current,
            target_soc=target,
            ready_by=now + timedelta(hours=3),
            now=now,
        )


def test_naive_or_non_future_deadlines_are_rejected():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        create_job(current_soc=20, target_soc=80, ready_by=now.replace(tzinfo=None), now=now)
    with pytest.raises(ValueError, match="future"):
        create_job(current_soc=20, target_soc=80, ready_by=now, now=now)


def test_corrupt_or_invalid_persisted_job_fails_closed(tmp_path):
    path = tmp_path / "ev-charge-job.json"
    path.write_text("not-json", encoding="utf-8")
    assert load_job(path=path) is None
    path.write_text(json.dumps({"id": "bad"}), encoding="utf-8")
    assert load_job(path=path) is None


def test_100kwh_default_and_efficiency_determine_ac_energy():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    plan = plan_ev_charge(
        job=_job(now, current=20, target=80, ready_hours=12),
        now=now,
        slots=_slots(now, [0.20] * 48),
        completion_buffer_minutes=30,
    )

    assert plan["required_stored_kwh"] == pytest.approx(60.0)
    assert plan["required_ac_kwh"] == pytest.approx(66.666667)
    assert plan["usable_capacity_kwh"] == 100.0
    assert plan["charge_efficiency"] == 0.90


def test_selects_cheapest_quarters_at_full_ceiling_with_one_partial_slot():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    cheap = {4, 5, 8}
    prices = [0.40] * 12
    for index in cheap:
        prices[index] = 0.10
    plan = plan_ev_charge(
        job=_job(now, current=50, target=60, ready_hours=4),
        now=now,
        slots=_slots(now, prices),
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    # 10 kWh requires two full 4 kWh quarters and one final 2 kWh quarter.
    assert plan["status"] == "planned"
    assert [slot["start"] for slot in plan["slots"]] == [
        (now + timedelta(minutes=60)).isoformat(),
        (now + timedelta(minutes=75)).isoformat(),
        (now + timedelta(minutes=120)).isoformat(),
    ]
    assert [slot["energy_kwh"] for slot in plan["slots"]] == [4.0, 4.0, 2.0]
    assert [slot["requested_power_kw"] for slot in plan["slots"]] == [16.0, 16.0, 8.0]
    assert len(plan["blocks"]) == 2
    assert plan["blocks"][0]["energy_kwh"] == 8.0
    assert len(plan["timeline_slots"]) == 12
    assert sum(slot["selected"] for slot in plan["timeline_slots"]) == 3
    assert plan["timeline_slots"][0]["grid_price_eur_per_kwh"] == 0.40
    assert plan["slots"][0]["soc_start"] == 50
    assert plan["slots"][-1]["soc_end"] == 60


def test_long_horizon_spreads_charge_across_each_day_at_its_cheapest_time():
    zone = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 7, 20, 20, tzinfo=zone)
    days = 9
    slot_count = days * 24 * 4
    prices = [0.30] * slot_count
    # Give every local date a distinct cheap hour. A global optimizer would
    # consume the first day's cheap/ordinary capacity and finish immediately;
    # a holiday plan should instead make gentle daily progress.
    for index in range(slot_count):
        start = now + timedelta(minutes=15 * index)
        if start.hour == 3:
            prices[index] = 0.10 + start.day / 10_000
    job = create_job(
        job_id="holiday",
        current_soc=30,
        target_soc=95,
        ready_by=now + timedelta(days=days),
        now=now,
    )

    plan = plan_charge(
        job,
        _slots(now, prices),
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    selected_dates = {
        datetime.fromisoformat(slot["start"]).astimezone(zone).date()
        for slot in plan["slots"]
    }
    eligible_dates = {
        (now + timedelta(minutes=15 * index)).date()
        for index in range(slot_count)
    }
    assert plan["planning_strategy"] == "daily_paced"
    assert selected_dates == eligible_dates
    assert plan["planned_ac_kwh"] == pytest.approx(65.0)
    assert plan["expected_completion"].startswith("2026-07-29")
    assert len(plan["daily_plan"]) == len(eligible_dates)
    assert all(day["energy_kwh"] > 0 for day in plan["daily_plan"])
    assert all(len(day["windows"]) == 1 for day in plan["daily_plan"])
    assert max(day["energy_kwh"] for day in plan["daily_plan"]) - min(
        day["energy_kwh"] for day in plan["daily_plan"]
    ) <= 0.001
    # Every full future day uses its deliberately cheapest hour.
    assert all(
        datetime.fromisoformat(slot["start"]).hour == 3
        for slot in plan["slots"]
        if datetime.fromisoformat(slot["start"]).date() != now.date()
    )


def test_short_horizon_keeps_global_cheapest_deadline_optimisation():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    prices = [0.30] * (48 * 4)
    prices[4] = 0.10
    prices[5] = 0.10
    prices[100] = 0.05
    prices[101] = 0.05
    plan = plan_charge(
        _job(now, current=50, target=58, ready_hours=48),
        _slots(now, prices),
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    assert plan["planning_strategy"] == "deadline_optimised"
    assert [slot["start"] for slot in plan["slots"]] == [
        (now + timedelta(minutes=25 * 60)).isoformat(),
        (now + timedelta(minutes=25 * 60 + 15)).isoformat(),
    ]


def test_daily_pacing_charges_early_when_later_headroom_cannot_meet_deadline():
    now = datetime(2026, 7, 20, 0, tzinfo=timezone.utc)
    slots = _slots(now, [0.20] * (72 * 4))
    for slot in slots:
        if slot["start"].date() > now.date():
            slot["max_energy_kwh"] = 0
    plan = plan_charge(
        _job(now, current=50, target=70, ready_hours=72),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    assert plan["planning_strategy"] == "daily_paced"
    assert plan["status"] == "planned"
    assert plan["planned_ac_kwh"] == 20
    assert {datetime.fromisoformat(slot["start"]).date() for slot in plan["slots"]} == {
        now.date()
    }


def test_daily_pacing_pulls_cheap_forecast_solar_forward_from_future_grid_days():
    now = datetime(2026, 7, 20, 0, tzinfo=timezone.utc)
    slots = _slots(now, [0.20] * (72 * 4))
    # Eight kWh of genuinely cheap exportable PV on day one should advance the
    # long-horizon job beyond its 4 kWh even share and reduce later grid needs.
    for index in (48, 49):
        slots[index]["pv_surplus_kwh"] = 4.0
        slots[index]["pv_opportunity_cost_eur_per_kwh"] = 0.05
    plan = plan_charge(
        _job(now, current=50, target=62, ready_hours=72),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    by_day = {day["date"]: day for day in plan["daily_plan"]}
    first = by_day[now.date().isoformat()]
    later = [day for date, day in by_day.items() if date != now.date().isoformat()]
    assert first["energy_kwh"] == pytest.approx(8.0)
    assert first["pv_energy_kwh"] == pytest.approx(8.0)
    assert sum(day["energy_kwh"] for day in later) == pytest.approx(4.0)
    assert plan["forecast_pv_kwh"] == pytest.approx(8.0)
    assert plan["forecast_grid_kwh"] == pytest.approx(4.0)


def test_daily_pacing_does_not_pull_expensive_export_value_ahead_of_cheap_grid():
    now = datetime(2026, 7, 20, 0, tzinfo=timezone.utc)
    slots = _slots(now, [0.10] * (72 * 4))
    for index in (48, 49):
        slots[index]["pv_surplus_kwh"] = 4.0
        slots[index]["pv_opportunity_cost_eur_per_kwh"] = 0.30
    plan = plan_charge(
        _job(now, current=50, target=62, ready_hours=72),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    assert max(day["energy_kwh"] for day in plan["daily_plan"]) == pytest.approx(4.0)
    assert plan["forecast_pv_kwh"] == pytest.approx(0.0)


def test_unknown_future_supply_is_pending_not_claimed_as_grid():
    now = datetime(2026, 7, 20, 0, tzinfo=timezone.utc)
    slots = _slots(now, [None] * (72 * 4))
    for slot in slots:
        slot["supply_forecast_known"] = False
    plan = plan_charge(
        _job(now, current=50, target=56, ready_hours=72),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
        unknown_price_eur_per_kwh=0.20,
    )

    assert {slot["supply"] for slot in plan["slots"]} == {"pending"}
    assert {day["supply"] for day in plan["daily_plan"]} == {"pending"}
    assert plan["source_pending_kwh"] == pytest.approx(6.0)


def test_pv_is_costed_at_opportunity_value_not_assumed_free():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    slots = _slots(now, [0.10, 0.20], pv={1: 4.0})
    slots[1]["pv_opportunity_cost_eur_per_kwh"] = 0.15
    plan = plan_ev_charge(
        job=_job(now, current=50, target=54, ready_hours=1),
        now=now,
        slots=slots,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    assert len(plan["slots"]) == 1
    assert plan["slots"][0]["start"] == now.isoformat()
    assert plan["slots"][0]["supply"] == "grid"


def test_partial_slot_uses_its_actual_pv_cost_instead_of_full_slot_average():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    slots = _slots(now, [0.10, 1.00])
    # Only 1 kWh is required.  The second slot is expensive at full power but
    # its first 1 kWh is forecast PV worth only 5 cents.
    slots[1]["pv_surplus_kwh"] = 1.0
    slots[1]["pv_opportunity_cost_eur_per_kwh"] = 0.05
    plan = plan_ev_charge(
        job=_job(now, current=50, target=51, ready_hours=1),
        now=now,
        slots=slots,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )

    assert len(plan["slots"]) == 1
    assert plan["slots"][0]["start"] == (now + timedelta(minutes=15)).isoformat()
    assert plan["slots"][0]["supply"] == "solar"
    assert plan["estimated_incremental_cost_eur"] == pytest.approx(0.05)


def test_negative_prices_are_selected_and_cost_remains_signed():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    plan = plan_charge(
        _job(now, current=50, target=54, ready_hours=2),
        _slots(now, [0.30, -0.10, 0.20]),
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        completion_buffer_minutes=0,
    )
    assert plan["slots"][0]["start"] == (now + timedelta(minutes=15)).isoformat()
    assert plan["estimated_incremental_cost_eur"] == pytest.approx(-0.40)


def test_tiny_price_dip_does_not_create_an_extra_charge_block():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    # Eight kWh requires two full quarters. Slot 2 is fractionally cheaper than
    # slot 1, but using 0+2 would need two starts instead of one contiguous run.
    plan = plan_charge(
        _job(now, current=50, target=58, ready_hours=2),
        _slots(now, [0.20, 0.20, 0.40, 0.198]),
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
        block_start_penalty_eur=0.02,
    )

    assert [slot["start"] for slot in plan["slots"]] == [
        now.isoformat(),
        (now + timedelta(minutes=15)).isoformat(),
    ]
    assert len(plan["blocks"]) == 1
    assert plan["estimated_incremental_cost_eur"] == pytest.approx(1.60)
    assert plan["optimization_block_penalty_eur"] == pytest.approx(0.02)


def test_materially_cheaper_isolated_slot_still_creates_an_extra_block():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    plan = plan_charge(
        _job(now, current=50, target=58, ready_hours=2),
        _slots(now, [0.20, 0.20, 0.40, 0.18]),
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
        block_start_penalty_eur=0.02,
    )

    assert [slot["start"] for slot in plan["slots"]] == [
        now.isoformat(),
        (now + timedelta(minutes=45)).isoformat(),
    ]
    assert len(plan["blocks"]) == 2
    assert plan["estimated_incremental_cost_eur"] == pytest.approx(1.52)
    assert plan["optimization_block_penalty_eur"] == pytest.approx(0.04)


def test_partial_energy_is_the_tail_of_a_selected_block():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    plan = plan_charge(
        _job(now, current=50, target=60, ready_hours=2),
        _slots(now, [0.20, 0.20, 0.20, 0.40]),
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
        block_start_penalty_eur=0.02,
    )

    assert [slot["energy_kwh"] for slot in plan["slots"]] == [4.0, 4.0, 2.0]
    assert [slot["requested_power_kw"] for slot in plan["slots"]] == [16.0, 16.0, 8.0]
    assert len(plan["blocks"]) == 2  # partial power is represented separately
    assert plan["blocks"][0]["start"] == now.isoformat()
    assert plan["blocks"][0]["end"] == (now + timedelta(minutes=30)).isoformat()


def test_contiguous_equal_power_is_one_command_block_across_supply_labels():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    slots = _slots(now, [0.10, 0.10], pv={1: 4.0})
    plan = plan_charge(
        _job(now, current=50, target=58, ready_hours=1),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )
    assert [slot["supply"] for slot in plan["slots"]] == ["grid", "solar"]
    assert plan["charge_start_count"] == 1
    assert len(plan["blocks"]) == 1
    assert plan["blocks"][0]["supply"] == "mixed"


def test_unknown_prices_are_tentative_and_keep_cost_claims_separate():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    plan = plan_ev_charge(
        job=_job(now, current=50, target=58, ready_hours=2),
        now=now,
        slots=_slots(now, [None, None, 0.40, 0.40]),
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
        unknown_price_eur_per_kwh=0.20,
    )

    assert plan["status"] == "planned"
    assert plan["tentative"] is True
    assert plan["confidence"] == "low"
    assert plan["estimated_incremental_cost_eur"] is None
    assert plan["provisional_incremental_cost_eur"] == pytest.approx(1.60)
    assert all(slot["tentative"] for slot in plan["slots"])


def test_infeasible_plan_exposes_shortfall_and_latest_safe_start():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    job = _job(now, current=20, target=80, ready_hours=2)
    plan = plan_ev_charge(
        job=job,
        now=now,
        slots=_slots(now, [0.10] * 8),
        requested_ceiling_kw=16,
        conservative_delivery_kw=14,
        completion_buffer_minutes=30,
    )

    assert plan["status"] == "infeasible"
    assert plan["energy_shortfall_kwh"] > 0
    # An already-infeasible horizon must say to plug in now rather than exposing
    # a theoretical timestamp in the past or a falsely safe later start.
    assert datetime.fromisoformat(plan["latest_safe_start"]) == now
    assert plan["requested_ceiling_kw"] == 16
    assert plan["expected_delivery_kw"] == 14


def test_latest_safe_start_includes_efficiency_and_completion_buffer():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    job = _job(now, current=20, target=80, ready_hours=14)
    plan = plan_charge(
        job,
        _slots(now, [0.20] * 56),
        now=now,
        requested_ceiling_kw=16,
        conservative_delivery_kw=16,
        completion_buffer_minutes=30,
    )
    # 66.667 AC kWh / 16 kW = 4h10m, plus 30m.  The advertised fallback
    # start is floored to a quarter so it is conservative, never late.
    exact = datetime.fromisoformat(job["ready_by"]) - timedelta(
        minutes=30 + (60 / 0.9) / 16 * 60
    )
    latest = datetime.fromisoformat(plan["latest_safe_start"])
    assert latest <= exact
    assert (exact - latest).total_seconds() < 15 * 60


def test_no_job_is_idle_and_forecast_overlay_is_identical():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    baseline = {
        now: 0.4,
        now + timedelta(minutes=15): 0.3,
    }
    plan = plan_ev_charge(job=None, now=now, slots=[])
    overlaid, summary = overlay_load_forecast(baseline, plan)

    assert plan["status"] == "idle"
    assert overlaid == baseline
    assert summary == {"planned_ev_kwh": 0.0, "active_job": False}


def test_overlay_adds_planned_ev_energy_without_mutating_baseline():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    baseline = {now: 0.4, now + timedelta(minutes=15): 0.3}
    plan = plan_ev_charge(
        job=_job(now, current=50, target=54, ready_hours=1),
        now=now,
        slots=_slots(now, [0.10, 0.20]),
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )
    overlaid, summary = overlay_load_forecast(baseline, plan)

    assert baseline[now] == 0.4
    assert overlaid[now] == pytest.approx(4.4)
    assert summary == {"planned_ev_kwh": 4.0, "active_job": True}


def test_replan_uses_fresh_soc_after_external_overload_reduced_delivery():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    job = _job(now, current=20, target=80, ready_hours=12)
    first = plan_charge(job, _slots(now, [0.20] * 48), now=now)
    later = now + timedelta(hours=1)
    second = plan_charge(
        job,
        _slots(later, [0.20] * 44),
        now=later,
        current_soc=25,  # observed progress, irrespective of requested power
    )
    assert first["required_ac_kwh"] == pytest.approx(66.666667)
    assert second["required_ac_kwh"] == pytest.approx(61.111111)
    assert second["current_soc"] == 25


def test_per_slot_site_headroom_caps_expected_ev_power_and_energy():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    slots = _slots(now, [0.10] * 4)
    # Broker calculation for a 16 kW grid ceiling and 3 kW forecast base load.
    for slot in slots:
        slot["expected_delivery_kw"] = 13.0
    plan = plan_charge(
        _job(now, current=50, target=60, ready_hours=2),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        conservative_delivery_kw=16,
        completion_buffer_minutes=0,
    )

    assert plan["status"] == "planned"
    assert [slot["safe_power_cap_kw"] for slot in plan["slots"]] == [13.0] * 4
    assert max(slot["requested_power_kw"] for slot in plan["slots"]) <= 13.0
    assert [slot["energy_kwh"] for slot in plan["slots"]] == [3.25, 3.25, 3.25, 0.25]
    assert sum(slot["energy_kwh"] for slot in plan["slots"]) == pytest.approx(10.0)


def test_variable_headroom_changes_feasibility_and_shortfall_exactly():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    capped = _slots(now, [0.10] * 3)
    for slot in capped:
        slot["expected_delivery_kw"] = 13.0
    kwargs = dict(
        job=_job(now, current=50, target=60, ready_hours=1),
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        conservative_delivery_kw=16,
        completion_buffer_minutes=0,
    )
    unrestricted = plan_charge(slots=_slots(now, [0.10] * 3), **kwargs)
    restricted = plan_charge(slots=capped, **kwargs)

    assert unrestricted["status"] == "planned"
    assert restricted["status"] == "infeasible"
    assert restricted["planned_ac_kwh"] == pytest.approx(9.75)
    assert restricted["energy_shortfall_kwh"] == pytest.approx(0.25)


def test_latest_safe_start_accumulates_variable_capacity_back_from_cutoff():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    slots = _slots(now, [0.10] * 8)
    # First half has 13 kW headroom; late quarters only have 2 kW.
    for index, slot in enumerate(slots):
        slot["expected_delivery_kw"] = 13.0 if index < 4 else 2.0
    plan = plan_charge(
        _job(now, current=50, target=58, ready_hours=2),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        conservative_delivery_kw=16,
        completion_buffer_minutes=0,
    )

    # Working backward: late 2 kW quarters provide 2 kWh total, then two
    # 3.25-kWh quarters are needed. The fallback therefore begins at slot 2.
    assert plan["latest_safe_start"] == (now + timedelta(minutes=30)).isoformat()
    assert datetime.fromisoformat(plan["latest_safe_start"]) < now + timedelta(hours=1)


def test_explicit_max_energy_cap_is_honoured_and_zero_cap_is_never_selected():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    slots = _slots(now, [-0.50, 0.10, 0.20])
    slots[0]["max_energy_kwh"] = 0.0
    slots[1]["max_energy_kwh"] = 1.0
    plan = plan_charge(
        _job(now, current=50, target=52, ready_hours=1),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )
    assert all(slot["start"] != now.isoformat() for slot in plan["slots"])
    assert plan["slots"][0]["energy_kwh"] == 1.0
    assert sum(slot["energy_kwh"] for slot in plan["slots"]) == 2.0


def test_many_sub_quantum_caps_remain_feasible_and_exact():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    slots = _slots(now, [0.10] * 12)
    for slot in slots:
        slot["max_energy_kwh"] = 0.10
    plan = plan_charge(
        _job(now, current=50, target=51, ready_hours=4),
        slots,
        now=now,
        usable_capacity_kwh=100,
        charge_efficiency=1,
        requested_ceiling_kw=16,
        completion_buffer_minutes=0,
    )
    assert plan["status"] == "planned"
    assert plan["planned_ac_kwh"] == 1.0
    assert sum(slot["energy_kwh"] for slot in plan["slots"]) == pytest.approx(1.0)
    assert max(slot["requested_power_kw"] for slot in plan["slots"]) == 0.4


def test_planning_is_stable_json_serialisable_and_dst_safe():
    zone = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 10, 24, 22, tzinfo=zone)
    starts = [
        datetime.fromtimestamp(now.timestamp() + index * 900, tz=zone)
        for index in range(32)
    ]
    slots = [
        {"start": start, "grid_price_eur_per_kwh": 0.10 + (index % 4) * 0.01}
        for index, start in enumerate(starts)
    ]
    job = create_job(
        job_id="dst-job",
        current_soc=70,
        target_soc=80,
        ready_by=datetime.fromtimestamp(now.timestamp() + 8 * 3600, tz=zone),
        now=now,
    )

    first = plan_ev_charge(job=job, now=now, slots=slots, completion_buffer_minutes=30)
    second = plan_ev_charge(job=job, now=now, slots=list(reversed(slots)), completion_buffer_minutes=30)

    assert first == second
    json.dumps(first)
    ready_by = datetime.fromisoformat(job["ready_by"])
    assert all(
        datetime.fromisoformat(slot["end"]).timestamp() <= ready_by.timestamp()
        for slot in first["slots"]
    )


def test_already_at_target_requires_no_charge():
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    plan = plan_ev_charge(
        job=_job(now, current=85, target=80),
        now=now,
        slots=_slots(now, [0.20] * 8),
    )
    assert plan["status"] == "completed"
    assert plan["required_ac_kwh"] == 0
    assert plan["slots"] == []
    assert plan["blocks"] == []


def test_pause_and_resume_are_persistent_and_paused_job_reserves_no_load(tmp_path):
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    path = tmp_path / "ev-charge-job.json"
    save_job(_job(now), path=path)

    paused = update_job_status("pause", path=path, now=now + timedelta(minutes=1))
    assert paused["status"] == "paused"
    assert load_job(path=path)["status"] == "paused"
    plan = plan_charge(paused, _slots(now, [0.10] * 32), now=now)
    assert plan["status"] == "paused"
    assert plan["slots"] == []

    resumed = update_job_status("resume", path=path, now=now + timedelta(minutes=2))
    assert resumed["status"] == "active"
    assert resumed["updated_at"] == (now + timedelta(minutes=2)).isoformat()
    assert delete_job(path=path) is True


def test_plan_snapshot_round_trips_atomically_and_corruption_is_ignored(tmp_path):
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    path = tmp_path / "ev-charge-plan.json"
    plan = plan_charge(None, [], now=now)
    save_plan_snapshot(plan, path=path)
    assert load_plan_snapshot(path=path) == plan
    path.write_text("broken", encoding="utf-8")
    assert load_plan_snapshot(path=path) is None
