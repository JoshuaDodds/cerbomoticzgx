from pathlib import Path
import sys
import json
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

stub_tibber_api = types.ModuleType("lib.tibber_api")
stub_tibber_api.lowest_48h_prices = MagicMock(return_value=[])
stub_tibber_api.lowest_24h_prices = MagicMock(return_value=[])
stub_tibber_api.publish_pricing_data = MagicMock()
stub_tibber_api.get_all_price_points = MagicMock(return_value=[])
sys.modules.setdefault("lib.tibber_api", stub_tibber_api)

stub_victron_integration = types.ModuleType("lib.victron_integration")
stub_victron_integration.ac_power_setpoint = MagicMock()
stub_victron_integration.limit_grid_feed_in = MagicMock()
stub_victron_integration.set_minimum_ess_soc = MagicMock()
stub_victron_integration.regulate_battery_max_voltage = MagicMock()
sys.modules.setdefault("lib.victron_integration", stub_victron_integration)

stub_config_retrieval = types.ModuleType("lib.config_retrieval")


def _stub_retrieve_setting(name):
    defaults = {
        "MAX_TIBBER_BUY_PRICE": "0.4",
        "ESS_EXPORT_AC_SETPOINT": "-10000",
        "DAILY_HOME_ENERGY_CONSUMPTION": "12",
        "TIBBER_UPDATES_ENABLED": "0",
    }
    return defaults.get(name)


stub_config_retrieval.retrieve_setting = _stub_retrieve_setting
sys.modules.setdefault("lib.config_retrieval", stub_config_retrieval)

import lib.energy_broker as energy_broker  # noqa: E402


class DummyState:
    def __init__(self, values):
        self._values = values

    def get(self, key):
        return self._values.get(key)

    def set(self, key, value):
        self._values[key] = value

    def has(self, key):
        return key in self._values


def test_hourly_load_profile_normalised(monkeypatch):
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    profile = energy_broker._hourly_load_profile()
    assert len(profile) == 24
    assert abs(sum(profile) - 1.0) < 1e-9
    # Evening peak hour should be weighted heavier than the small hours.
    assert profile[19] > profile[3]


def test_build_load_forecast_distributes_daily_total(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    # 20 kWh forecast for the day (VRM consumption forecast is in Wh).
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"consumption_total_projected": 20000}))
    # No history -> exercise the diurnal-profile fallback deterministically.
    monkeypatch.setattr(energy_broker, "_historical_load_by_slot", lambda days=3: {})

    slots = [{"start": datetime(2026, 6, 13, h, 0, 0)} for h in range(24)]
    forecast = energy_broker._build_load_forecast_by_slot(slots, 1.0)

    assert abs(sum(forecast.values()) - 20.0) < 1e-6
    # Evening consumption greater than overnight.
    assert forecast[datetime(2026, 6, 13, 19, 0, 0)] > forecast[datetime(2026, 6, 13, 3, 0, 0)]


def test_ev_smart_forecast_disabled_is_an_exact_noop(monkeypatch):
    """The feature gate must preserve Summer/Winter optimizer input byte-for-byte."""
    from datetime import datetime, timezone

    start = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    original = {start: 0.42}
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: "False" if name == "EV_SMART_CHARGE_ENABLED" else None,
    )

    overlaid, context = energy_broker._apply_ev_smart_charge_to_forecast(
        original,
        {start: 0.1},
        [{"start": start, "total": 0.20}],
        slot_duration_h=0.25,
        current_soc=20,
        now=start,
    )

    assert overlaid is original
    assert context["enabled"] is False
    assert context["active"] is False


def test_ev_smart_forecast_adds_planned_energy_and_publishes_snapshot(monkeypatch):
    """An active job is an explicit load overlay, never learned as base load."""
    from datetime import datetime, timedelta, timezone
    from lib import ev_smart_charge

    start = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    starts = [start + timedelta(minutes=15 * i) for i in range(2)]
    baseline = {starts[0]: 0.3, starts[1]: 0.4}
    saved = []
    planner_inputs = []
    fake_plan = {
        "available": True,
        "active": True,
        "status": "planned",
        "slots": [
            {"start": starts[0].isoformat(), "energy_kwh": 1.0, "target_kw": 4.0},
            {"start": starts[1].isoformat(), "energy_kwh": 0.0, "target_kw": 0.0},
        ],
    }

    settings = {
        "EV_SMART_CHARGE_ENABLED": "True",
        "EV_SMART_CHARGE_APPLY": "True",
        "EV_BATTERY_USABLE_KWH": "100",
        "EV_CHARGE_EFFICIENCY": "0.9",
        "EV_CHARGER_MAX_KW": "16",
        "EV_DEADLINE_BUFFER_MINUTES": "30",
        "EV_SMART_CHARGE_JOB_PATH": "/tmp/test-job.json",
        "EV_SMART_CHARGE_PLAN_PATH": "/tmp/test-plan.json",
    }
    monkeypatch.setattr(energy_broker, "STATE", DummyState({}))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: settings.get(name))
    monkeypatch.setattr(ev_smart_charge, "load_job", lambda path=None: {"status": "active"})
    monkeypatch.setattr(
        ev_smart_charge, "plan_charge",
        lambda *args, **kwargs: planner_inputs.append((args, kwargs)) or fake_plan)
    monkeypatch.setattr(ev_smart_charge, "save_plan_snapshot", lambda plan, path=None: saved.append((plan, path)))

    overlaid, context = energy_broker._apply_ev_smart_charge_to_forecast(
        baseline,
        {starts[0]: 0.2, starts[1]: 0.0},
        [{"start": starts[0], "total": 0.20}, {"start": starts[1], "total": 0.30}],
        slot_duration_h=0.25,
        current_soc=20,
        now=start,
    )

    assert overlaid[starts[0]] == pytest.approx(1.3)
    assert overlaid[starts[1]] == pytest.approx(0.4)
    assert baseline[starts[0]] == pytest.approx(0.3)  # caller input was not mutated
    assert context["active"] is True
    assert saved == [(fake_plan, "/tmp/test-plan.json")]
    # 13 kW site cap - (1.2 kW base - 0.8 kW PV) = 12.6 kW safe EV
    # forecast headroom. The 16 kW request remains only a Maxem-subordinate ceiling.
    assert planner_inputs[0][0][1][0]["expected_delivery_kw"] == pytest.approx(12.6)


def test_ev_smart_forecast_reserves_surplus_for_protected_home_battery(monkeypatch):
    """Forecast PV is EV surplus only after the existing ESS target is supplied."""
    from datetime import datetime, timedelta, timezone
    from lib import ev_smart_charge

    start = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    starts = [start + timedelta(minutes=15 * index) for index in range(4)]
    captured = []
    settings = {
        "EV_SMART_CHARGE_ENABLED": "True",
        "EV_SMART_CHARGE_APPLY": "False",
        "MINIMUM_ESS_SOC": "90",
        "BATTERY_CAPACITY_KWH": "40",
        "AC_DC_CHARGE_EFFICIENCY": "1",
    }
    monkeypatch.setattr(energy_broker, "STATE", DummyState({}))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: settings.get(name))
    monkeypatch.setattr(ev_smart_charge, "load_job", lambda path=None: {"status": "active"})
    monkeypatch.setattr(
        ev_smart_charge,
        "plan_charge",
        lambda _job, slots, **_kwargs: captured.extend(slots) or {
            "active": True, "status": "planned", "slots": [],
        },
    )
    monkeypatch.setattr(ev_smart_charge, "save_plan_snapshot", lambda *args, **kwargs: None)

    energy_broker._apply_ev_smart_charge_to_forecast(
        {slot: 0.0 for slot in starts},
        {slot: 1.0 for slot in starts},
        [{"start": slot, "total": 0.20} for slot in starts],
        slot_duration_h=0.25,
        current_soc=30,
        ess_soc=80,
        now=start,
    )

    # Raising a 40 kWh stationary pack from 80% to 90% consumes all 4 kWh.
    assert sum(slot["pv_reserved_for_ess_kwh"] for slot in captured) == pytest.approx(4.0)
    assert sum(slot["pv_surplus_kwh"] for slot in captured) == pytest.approx(0.0)
    assert all(slot["supply_forecast_known"] is True for slot in captured)


def test_ev_smart_shadow_plans_but_does_not_change_ess_load(monkeypatch):
    from datetime import datetime, timezone
    from lib import ev_smart_charge

    start = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    baseline = {start: 0.3}
    fake_plan = {
        "active": True, "status": "planned",
        "slots": [{"start": start.isoformat(), "energy_kwh": 1.0,
                   "requested_power_kw": 4.0}],
    }
    settings = {
        "EV_SMART_CHARGE_ENABLED": "True",
        "EV_SMART_CHARGE_APPLY": "False",
    }
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: settings.get(name))
    monkeypatch.setattr(ev_smart_charge, "load_job", lambda path=None: {"status": "active"})
    monkeypatch.setattr(ev_smart_charge, "plan_charge", lambda *args, **kwargs: fake_plan)
    monkeypatch.setattr(ev_smart_charge, "save_plan_snapshot", lambda *args, **kwargs: None)

    forecast, context = energy_broker._apply_ev_smart_charge_to_forecast(
        baseline, {start: 0.0}, [{"start": start, "total": 0.2}],
        slot_duration_h=0.25, current_soc=20, now=start,
    )

    assert forecast is baseline
    assert context["active"] is True
    assert context["apply"] is False
    assert context["ess_overlay_applied"] is False


def test_pv_forecast_uses_learned_shape(monkeypatch):
    from datetime import datetime, date
    # 10 kWh remaining today; learned shape: nothing at 06:00, everything midday.
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"pv_projected_remaining": 10000, "pv_projected_tomorrow": 0}))
    monkeypatch.setattr(energy_broker, "_pv_shape_by_slot", lambda days=5: {"06:00": 0.0, "12:00": 4.0})
    t = date.today()
    slots = [{"start": datetime(t.year, t.month, t.day, 6, 0)},
             {"start": datetime(t.year, t.month, t.day, 12, 0)}]
    fc = energy_broker._build_pv_forecast_by_slot(slots, 0.25)
    # All 10 kWh is shaped onto the midday slot; the shaded 06:00 slot gets ~0.
    assert abs(fc[slots[1]["start"]] - 10.0) < 1e-6
    assert fc.get(slots[0]["start"], 0.0) < 1e-6


def test_forecast_slots_match_optimizer_future_horizon():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 6, 29, 14, 0, tzinfo=timezone.utc)
    slots = [{"start": base + timedelta(minutes=15 * i)} for i in range(12)]
    now = datetime(2026, 6, 29, 15, 15, tzinfo=timezone.utc)

    forecast_slots = energy_broker._forecast_slots_for_optimizer(slots, 0.25, now=now)

    assert forecast_slots[0]["start"] == now
    assert all(slot["start"] >= now for slot in forecast_slots)


def test_remaining_pv_is_not_distributed_into_elapsed_slots(monkeypatch):
    from datetime import datetime, timedelta, timezone, date

    day = date.today()
    base = datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc)
    slots = [{"start": base + timedelta(minutes=15 * i)} for i in range(12)]
    now = base + timedelta(hours=1, minutes=15)

    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"pv_projected_remaining": 6800, "pv_projected_tomorrow": 0}))
    monkeypatch.setattr(energy_broker, "_pv_intraday_remaining_kwh",
                        lambda shape, remaining, now=None: remaining)
    monkeypatch.setattr(energy_broker, "_pv_shape_by_slot",
                        lambda days=3: {f"{slot['start'].hour:02d}:{slot['start'].minute:02d}": 1.0
                                        for slot in slots})

    forecast_slots = energy_broker._forecast_slots_for_optimizer(slots, 0.25, now=now)
    fc = energy_broker._build_pv_forecast_by_slot(forecast_slots, 0.25)

    assert len(fc) == len(forecast_slots)
    assert abs(sum(fc.values()) - 6.8) < 1e-6


def test_pv_nowcast_raises_near_term_slots_from_live_anchor(monkeypatch):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 6, 29, 16, 0, tzinfo=timezone.utc)
    slots = [{"start": start + timedelta(minutes=15 * i)} for i in range(8)]
    base = {slot["start"]: 0.25 for slot in slots}
    weather_context = {
        "available": True,
        "slots": {
            slot["start"].isoformat(): {"gti_forecast_wm2": 560.0}
            for slot in slots
        },
        "summary": {},
    }
    monkeypatch.setattr(energy_broker, "_pv_nowcast_anchor_kwh",
                        lambda slot_h, now=None: {"slot_kwh": 1.5, "source": "test", "drop_ratio": 1.0})

    adjusted = energy_broker._apply_pv_nowcast(base, slots, weather_context, 0.25, now=start)

    assert adjusted[slots[0]["start"]] > 1.2
    assert adjusted[slots[0]["start"]] > base[slots[0]["start"]]
    assert weather_context["summary"]["pv_nowcast_applied"] is True
    assert weather_context["slots"][slots[0]["start"].isoformat()]["pv_nowcast_kwh"] == adjusted[slots[0]["start"]]


def test_pv_nowcast_fades_with_horizon_and_leaves_tomorrow(monkeypatch):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 6, 29, 16, 0, tzinfo=timezone.utc)
    today_slots = [{"start": start + timedelta(minutes=15 * i)} for i in range(20)]
    tomorrow_slot = {"start": start + timedelta(days=1)}
    slots = today_slots + [tomorrow_slot]
    base = {slot["start"]: 0.2 for slot in slots}
    weather_context = {
        "available": True,
        "slots": {
            slot["start"].isoformat(): {"gti_forecast_wm2": 560.0}
            for slot in slots
        },
        "summary": {},
    }
    monkeypatch.setattr(energy_broker, "_pv_nowcast_anchor_kwh",
                        lambda slot_h, now=None: {"slot_kwh": 1.2, "source": "test", "drop_ratio": 1.0})

    adjusted = energy_broker._apply_pv_nowcast(base, slots, weather_context, 0.25, now=start)

    assert adjusted[today_slots[0]["start"]] > adjusted[today_slots[-1]["start"]]
    assert adjusted[tomorrow_slot["start"]] == base[tomorrow_slot["start"]]


def test_pv_nowcast_dropoff_reduces_uplift_confidence(monkeypatch):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 6, 29, 18, 0, tzinfo=timezone.utc)
    slots = [{"start": start + timedelta(minutes=15 * i)} for i in range(4)]
    base = {slot["start"]: 0.2 for slot in slots}
    weather_context = {
        "available": True,
        "slots": {
            slot["start"].isoformat(): {"gti_forecast_wm2": 500.0}
            for slot in slots
        },
        "summary": {},
    }
    monkeypatch.setattr(energy_broker, "_pv_nowcast_anchor_kwh",
                        lambda slot_h, now=None: {"slot_kwh": 0.5, "source": "live_drop", "drop_ratio": 0.35})

    adjusted = energy_broker._apply_pv_nowcast(base, slots, weather_context, 0.25, now=start)

    assert adjusted[slots[0]["start"]] > base[slots[0]["start"]]
    assert adjusted[slots[0]["start"]] < 0.4


def test_pv_nowcast_lowers_near_term_slots_when_gti_confirms_overcast(monkeypatch):
    from datetime import datetime, timedelta, timezone

    # Baseline forecast is optimistic (1.0 kWh/slot) but the live/recent anchor plus a
    # matching GTI say only ~0.2 kWh is realistic — the overlay must pull the near-term
    # slots DOWN toward that evidence, not leave them optimistic.
    start = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    slots = [{"start": start + timedelta(minutes=15 * i)} for i in range(4)]
    base = {slot["start"]: 1.0 for slot in slots}
    weather_context = {
        "available": True,
        "slots": {
            slot["start"].isoformat(): {"gti_forecast_wm2": 500.0}
            for slot in slots
        },
        "summary": {},
    }
    monkeypatch.setattr(energy_broker, "_pv_nowcast_anchor_kwh",
                        lambda slot_h, now=None: {"slot_kwh": 0.2, "source": "test", "drop_ratio": 1.0})

    adjusted = energy_broker._apply_pv_nowcast(base, slots, weather_context, 0.25, now=start)

    assert adjusted[slots[0]["start"]] < base[slots[0]["start"]]   # pulled down
    assert 0.2 <= adjusted[slots[0]["start"]] < 0.4
    assert weather_context["summary"]["pv_nowcast_applied"] is True


def test_pv_nowcast_does_not_lower_on_missing_gti(monkeypatch):
    from datetime import datetime, timedelta, timezone

    # No GTI data and beyond 1h ahead -> gti_ratio falls back to 0. With drop_ratio healthy,
    # the overlay must NOT zero out the slot on absent evidence; the baseline stands.
    start = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    slots = [{"start": start + timedelta(minutes=15 * i)} for i in range(12)]
    base = {slot["start"]: 1.0 for slot in slots}
    weather_context = {"available": True, "slots": {}, "summary": {}}   # no GTI rows
    monkeypatch.setattr(energy_broker, "_pv_nowcast_anchor_kwh",
                        lambda slot_h, now=None: {"slot_kwh": 0.2, "source": "test", "drop_ratio": 1.0})

    adjusted = energy_broker._apply_pv_nowcast(base, slots, weather_context, 0.25, now=start)

    far = slots[-1]["start"]   # >1h ahead, no GTI -> must be left at baseline
    assert adjusted[far] == base[far]


def test_pv_nowcast_accepts_fresh_zero_watts_as_sunset_evidence(monkeypatch):
    from datetime import datetime, timezone

    now = datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        energy_broker,
        "STATE",
        DummyState({
            "pv_power": 0.0,
            "pv_power_updated_at": now.timestamp(),
        }),
    )
    monkeypatch.setattr(
        energy_broker,
        "_latest_settled_pv_slot_kwh",
        lambda slot_h, now=None: 0.8,
    )

    anchor = energy_broker._pv_nowcast_anchor_kwh(0.25, now=now)

    assert anchor["source"] == "live_drop"
    assert anchor["slot_kwh"] == 0.0
    assert anchor["drop_ratio"] == 0.0


def test_confirmed_zero_pv_strongly_lowers_stale_sunset_forecast(monkeypatch):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc)
    slots = [{"start": start + timedelta(minutes=15 * i)} for i in range(4)]
    base = {slot["start"]: 0.8 for slot in slots}
    weather_context = {
        "available": True,
        "slots": {
            slot["start"].isoformat(): {"gti_forecast_wm2": 0.0}
            for slot in slots
        },
        "summary": {},
    }
    monkeypatch.setattr(
        energy_broker,
        "_pv_nowcast_anchor_kwh",
        lambda slot_h, now=None: {
            "slot_kwh": 0.0,
            "source": "live_drop",
            "drop_ratio": 0.0,
        },
    )

    adjusted = energy_broker._apply_pv_nowcast(
        base, slots, weather_context, 0.25, now=start)

    assert adjusted[slots[0]["start"]] <= 0.1
    assert adjusted[slots[-1]["start"]] < base[slots[-1]["start"]]


def test_pv_intraday_correction_scales_up_on_outperformance(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)  # defaults
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 20.0, "c2_daily_yield": 0.0}))
    shape = {"06:00": 1.0, "12:00": 1.0, "18:00": 1.0}     # 3 equal daylight slots
    now = datetime(2026, 6, 18, 12, 30)                    # 2/3 of the curve elapsed
    # projected_total = 20 / (2/3) = 30; projected_remaining = 10;
    # corrected = 2 + 0.6*(10-2) = 6.8  (scaled UP from VRM's 2 kWh)
    out = energy_broker._pv_intraday_remaining_kwh(shape, 2.0, now=now)
    assert abs(out - 6.8) < 1e-6


def test_pv_intraday_correction_scales_down_on_cloudy_underperformance(monkeypatch):
    from datetime import datetime
    # Cloudy day: VRM still expects a big total, but production so far is far below the
    # elapsed share of the curve, so the remaining forecast must be pulled DOWN.
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)  # defaults
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 4.0, "c2_daily_yield": 0.0}))
    shape = {"06:00": 1.0, "12:00": 1.0, "18:00": 1.0}     # 3 equal daylight slots
    now = datetime(2026, 6, 18, 12, 30)                    # 2/3 of the curve elapsed
    # projected_total = 4 / (2/3) = 6; VRM total = 4 + 16 = 20; projected_remaining = 2;
    # corrected = 16 + 0.6*(2-16) = 7.6  (scaled DOWN from VRM's 16 kWh remaining)
    out = energy_broker._pv_intraday_remaining_kwh(shape, 16.0, now=now)
    assert out < 16.0
    assert abs(out - 7.6) < 1e-6


def test_pv_intraday_correction_no_downscale_before_down_min_elapsed(monkeypatch):
    from datetime import datetime
    # Underproducing, but too early in the day (past the up-threshold, below the
    # down-threshold): a morning cloud that could still clear must not collapse the day.
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 0.3, "c2_daily_yield": 0.0}))
    shape = {"06:00": 0.15, "12:00": 0.85}                 # ~15% elapsed by 06:30
    now = datetime(2026, 6, 18, 6, 30)
    # frac 0.15: above ESS_PV_INTRADAY_MIN_ELAPSED (0.10) but below DOWN_MIN_ELAPSED (0.30)
    assert energy_broker._pv_intraday_remaining_kwh(shape, 8.0, now=now) == 8.0


def test_pv_intraday_correction_noop_when_on_track(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 20.0, "c2_daily_yield": 0.0}))
    shape = {"06:00": 1.0, "12:00": 1.0, "18:00": 1.0}
    now = datetime(2026, 6, 18, 12, 30)                    # projected_total 30 == VRM total
    out = energy_broker._pv_intraday_remaining_kwh(shape, 10.0, now=now)
    assert abs(out - 10.0) < 1e-6                          # already on track -> unchanged


def test_pv_intraday_correction_too_early_in_day(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 0.5, "c2_daily_yield": 0.0}))
    shape = {"05:00": 0.05, "12:00": 5.0, "18:00": 1.0}    # ~0.8% elapsed by 05:30
    now = datetime(2026, 6, 18, 5, 30)
    assert energy_broker._pv_intraday_remaining_kwh(shape, 8.0, now=now) == 8.0


def test_pv_intraday_correction_skips_outside_daylight(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 20.0, "c2_daily_yield": 0.0}))
    now = datetime(2026, 6, 18, 23, 0)
    assert energy_broker._pv_intraday_remaining_kwh({"12:00": 1.0}, 5.0, now=now) == 5.0


def test_pv_intraday_correction_disabled_by_setting(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: "0" if name == "ESS_PV_INTRADAY_CORRECTION" else None)
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 20.0, "c2_daily_yield": 0.0}))
    shape = {"06:00": 1.0, "12:00": 1.0, "18:00": 1.0}
    now = datetime(2026, 6, 18, 12, 30)
    assert energy_broker._pv_intraday_remaining_kwh(shape, 2.0, now=now) == 2.0


def test_history_compaction_job_runs_backfill_when_enabled(monkeypatch):
    calls = {}
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: {"HISTORY_DIR": "data/history"}.get(name))  # ENABLED unset -> default True
    monkeypatch.setattr(energy_broker._hist, "duckdb_available", lambda: True)

    def fake_backfill(hist_dir, remove_ndjson=True):
        calls["args"] = (hist_dir, remove_ndjson)
        return []

    monkeypatch.setattr(energy_broker._hist, "backfill_cold_months", fake_backfill)
    energy_broker._run_history_compaction()
    assert calls["args"] == ("data/history", True)


def test_history_compaction_job_skipped_when_disabled(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: "0" if name == "HISTORY_COMPACTION_ENABLED" else None)
    monkeypatch.setattr(energy_broker._hist, "duckdb_available", lambda: True)

    def boom(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(energy_broker._hist, "backfill_cold_months", boom)
    energy_broker._run_history_compaction()
    assert called["n"] == 0


def test_history_compaction_job_skips_without_duckdb(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)   # enabled by default
    monkeypatch.setattr(energy_broker._hist, "duckdb_available", lambda: False)

    def boom(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(energy_broker._hist, "backfill_cold_months", boom)
    energy_broker._run_history_compaction()
    assert called["n"] == 0


def test_load_forecast_prefers_historical_average(monkeypatch):
    from datetime import datetime
    # Realised 0.8 kW for the 06:00 quarter-hours over the last few days.
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"consumption_total_projected": 20000}))
    monkeypatch.setattr(energy_broker, "_historical_load_by_slot",
                        lambda days=3: {"06:00": 0.8, "06:15": 0.8, "06:30": 0.8, "06:45": 0.8})
    slots = [{"start": datetime(2026, 6, 17, 6, m, 0)} for m in (0, 15, 30, 45)]
    fc = energy_broker._build_load_forecast_by_slot(slots, 0.25)
    # 0.8 kW * 0.25 h = 0.20 kWh per 15-min slot (empirical, not profile-derived).
    for s in slots:
        assert abs(fc[s["start"]] - 0.20) < 1e-6


def test_publish_plan_json_serializes_weather_datetime_maps(monkeypatch, tmp_path):
    from datetime import datetime

    out = tmp_path / "plan.json"
    settings = {
        "AI_PLAN_EXPORT_PATH": str(out),
        "ESS_MAX_GRID_CHARGE_SOC": "85",
        "ESS_MIN_SELL_PRICE": "0.18",
        "ESS_BATTERY_CYCLE_COST": "0.03",
        "ESS_ARBITRAGE_MARGIN": "0.03",
    }
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: settings.get(name),
    )
    monkeypatch.setattr(
        energy_broker,
        "STATE",
        DummyState({
            "c1_daily_yield": 1.0,
            "c2_daily_yield": 2.0,
            "consumption_total_cumulative": 4000,
            "pv_projected_today": 10.0,
            "pv_projected_tomorrow": 20.0,
        }),
    )
    slot_start = datetime(2026, 6, 28, 15, 45)
    result = {
        "schedule": [{
            "time": slot_start,
            "action": "hold",
            "control_action": "IDLE",
            "price": 0.142,
            "sell": 0.142,
            "soc_start": 92.0,
            "soc_end": 92.0,
            "grid_energy": 0.0,
            "pv": 0.2,
            "load": 0.3,
            "reason": "test",
            "reason_code": "TEST",
        }],
        "victron_slots": [],
        "slot_duration_h": 0.25,
        "mode": "hold",
        "control_action": "IDLE",
        "reason": "test",
        "reason_code": "TEST",
        "current_price": 0.142,
        "setpoint": 0.0,
        "limit_feed_in": False,
        "weather_context": {
            "available": True,
            "summary": {
                "source": "open-meteo",
                "fetched_at": slot_start,
                "pv_nowcast_applied": True,
                "pv_nowcast_delta_kwh": 0.4,
            },
            "load_adjustments": {slot_start: 0.1},
            "load_shadow_forecast": {slot_start: 0.4},
            "pv_shadow_forecast": {slot_start: 0.2},
            "slots": {slot_start: {"time": slot_start, "temp_forecast_c": 24.0}},
        },
        "planning_policy": {
            "selected": "today_first",
            "reason_code": "DAILY_SETTLEMENT_PROTECTED",
            "today_sacrifice_eur": 10.0,
            "future_gain_eur": 3.0,
        },
        "optimizer_mode": "winter",
        "winter_policy": {
            "mode": "winter",
            "selected_candidate": "self_sufficiency",
            "protected_soc_percent": 46.0,
            "reason_code": "WINTER_EXCEPTIONAL_SPREAD_REJECTED",
        },
        "appliance_reservations": {
            "enabled": True,
            "devices": ["Dishwasher", "Dryer"],
            "reserved_kwh": 2.35,
            "active_reservations": 2,
        },
    }

    energy_broker._publish_plan_json(
        result,
        batt_soc=92.0,
        price_points=96,
        pv_remaining=1234.0,
        applied_setpoint=0.0,
        today_actuals={"imp_kwh": 1.0, "exp_kwh": 0.5, "imp_cost": 0.2, "exp_rev": 0.1},
    )

    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["weather"]["summary"]["fetched_at"] == slot_start.isoformat()
    assert payload["weather"]["load_adjustments"][slot_start.isoformat()] == 0.1
    assert payload["weather"]["slots"][slot_start.isoformat()]["time"] == slot_start.isoformat()
    assert payload["pv_remaining_wh"] == 1234.0
    assert payload["pv_remaining_raw_wh"] == 1234.0
    assert payload["pv_remaining_raw_source"] == "VRM forecast"
    assert payload["pv_adjusted_remaining_wh"] == 200.0
    assert payload["pv_adjusted_remaining_source"] == "optimizer nowcast"
    assert payload["pv_adjustment_kwh"] == -1.034
    assert payload["optimizer_guardrails"] == {
        "max_grid_charge_soc": 85.0,
        "min_sell_price": 0.18,
        "battery_cycle_cost": 0.03,
        "arbitrage_margin": 0.03,
    }
    assert payload["planning_policy"]["selected"] == "today_first"
    assert payload["planning_policy"]["reason_code"] == "DAILY_SETTLEMENT_PROTECTED"
    assert payload["optimizer_mode"] == "winter"
    assert payload["winter_policy"] == {
        "mode": "winter",
        "selected_candidate": "self_sufficiency",
        "protected_soc_percent": 46.0,
        "reason_code": "WINTER_EXCEPTIONAL_SPREAD_REJECTED",
    }
    assert payload["appliance_reservations"] == {
        "enabled": True,
        "devices": ["Dishwasher", "Dryer"],
        "reserved_kwh": 2.35,
        "active_reservations": 2,
    }


def test_weather_context_log_message_explains_applied_adjustments():
    summary = {
        "source": "open-meteo",
        "max_temp_c": 27.9,
        "load_adj_today_kwh": 1.058,
        "pv_shadow_abs_delta_kwh": 3.42,
        "hvac_apply": True,
        "pv_apply": True,
    }

    msg = energy_broker._weather_context_log_message(summary)

    assert msg == (
        "Weather forecast applied: Open-Meteo load +1.06 kWh today (max 27.9C); "
        "PV timing shifted 3.42 kWh, total unchanged."
    )


def test_weather_context_log_message_shows_pv_total_direction_when_material():
    summary = {
        "source": "open-meteo",
        "max_temp_c": 25.3,
        "load_adj_today_kwh": -0.42,
        "pv_shadow_abs_delta_kwh": 4.4,
        "pv_shadow_net_delta_kwh": -1.25,
        "hvac_apply": True,
        "pv_apply": True,
    }

    msg = energy_broker._weather_context_log_message(summary)

    assert msg == (
        "Weather forecast applied: Open-Meteo load -0.42 kWh today (max 25.3C); "
        "PV total -1.25 kWh, timing shifted 4.40 kWh."
    )


def test_weather_context_log_message_stays_quiet_when_nothing_useful_changed():
    assert energy_broker._weather_context_log_message({
        "source": "open-meteo",
        "load_adj_today_kwh": 0.04,
        "pv_shadow_abs_delta_kwh": 0.19,
        "pv_shadow_net_delta_kwh": 0.04,
        "hvac_apply": False,
        "pv_apply": False,
    }) is None


def test_weather_context_log_signature_ignores_tiny_adjustment_changes():
    base = {
        "source": "open-meteo",
        "max_temp_c": 25.3,
        "load_adj_today_kwh": 1.46,
        "pv_shadow_abs_delta_kwh": 10.54,
        "pv_shadow_net_delta_kwh": 0.0,
        "hvac_apply": True,
        "pv_apply": True,
    }
    tiny = {**base, "load_adj_today_kwh": 1.51, "pv_shadow_abs_delta_kwh": 10.33}
    material = {**base, "load_adj_today_kwh": 1.82, "pv_shadow_abs_delta_kwh": 12.1}

    assert energy_broker._weather_context_log_signature(base) == energy_broker._weather_context_log_signature(tiny)
    assert energy_broker._weather_context_log_signature(base) != energy_broker._weather_context_log_signature(material)


def test_estimate_daily_consumption_prefers_vrm_forecast(monkeypatch):
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"consumption_total_projected": 18500}))
    assert abs(energy_broker._estimate_daily_consumption_kwh() - 18.5) < 1e-6


def test_grid_assist_setpoint_subtracts_pv(monkeypatch):
    # House load 3000W, PV 1200W -> import only the 1800W deficit.
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"ac_out_power": 3000, "pv_power": 1200}))
    assert energy_broker._grid_assist_setpoint_watts() == 1800


def test_grid_assist_setpoint_zero_when_pv_covers_load(monkeypatch):
    # PV exceeds load -> do not import; setpoint 0 so surplus PV charges/exports.
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"ac_out_power": 1000, "pv_power": 4000}))
    assert energy_broker._grid_assist_setpoint_watts() == 0


def test_manual_grid_assist_setpoint_matches_full_ac_load(monkeypatch):
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"ac_out_power": 3000, "pv_power": 1200}))

    assert energy_broker._grid_assist_setpoint_watts(cover_all_load=True) == 3000


def test_manual_grid_assist_reports_retain_even_with_zero_setpoint():
    assert energy_broker._grid_assist_control_action(applied_setpoint=0, manual_grid_assist=True) == "RETAIN"
    assert energy_broker._grid_assist_control_action(applied_setpoint=0, manual_grid_assist=False) == "IDLE"


def test_current_min_soc_reserve_follows_explicit_winter_mode(monkeypatch):
    import sys
    import lib.ess_mode as ess_mode
    import lib.helpers as helpers
    cr = sys.modules.get("lib.config_retrieval")
    settings = {
        "MIN_SOC_RESERVE_WINTER": "40",
        "MIN_SOC_RESERVE_SUMMER": "0",
    }
    monkeypatch.setattr(cr, "retrieve_setting", settings.get)
    monkeypatch.setattr(ess_mode, "WINTER_MODE", True)

    assert helpers.current_min_soc_reserve() == 40.0

    monkeypatch.setattr(ess_mode, "WINTER_MODE", False)
    assert helpers.current_min_soc_reserve() == 0.0


def test_settlement_writes_predicted_vs_actual(monkeypatch, tmp_path):
    # First cycle writes only the snapshot; the next cycle settles the slot that
    # just closed by diffing the cumulative counters.
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(tmp_path) if name == "HISTORY_DIR" else None)
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 1.0, "c2_daily_yield": 0.5}))
    monkeypatch.setattr(energy_broker, "_LAST_SLOT_PATH", str(tmp_path / "last_slot.json"))
    # Keep the cost-basis tracker's file inside the tmp dir (don't touch the repo).
    import lib.ess_cost_basis as _cb
    monkeypatch.setattr(_cb, "_path", lambda: str(tmp_path / "cost_basis.json"))

    from datetime import datetime, timezone, timedelta
    res = {"control_action": "SELL", "slot_duration_h": 0.25,
           "schedule": [{"grid_energy": -2.0, "price": 0.30, "sell": 0.30}]}

    t1 = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(minutes=15)   # next slot -> the prior slot settles
    res["schedule"][0].update({"time": t1, "pv": 0.7, "load": 0.8})
    res["weather_context"] = {
        "summary": {"hvac_apply": False, "pv_apply": False},
        "slots": {
            t1.isoformat(): {
                "baseline_load_kwh": 0.6,
                "weather_load_shadow_kwh": 0.65,
                "final_load_forecast_kwh": 0.8,
                "baseline_pv_kwh": 0.9,
                "weather_pv_shadow_kwh": 0.75,
                "final_pv_forecast_kwh": 0.7,
            },
        },
    }

    # Cycle 1: counters at import 1.0 kWh (€0.20), no export; SoC 80%.
    energy_broker._settle_prior_slot(
        res, batt_soc=80.0,
        today_actuals={"imp_kwh": 1.0, "imp_cost": 0.20, "exp_kwh": 0.0, "exp_rev": 0.0}, now=t1)
    assert (tmp_path / "last_slot.json").exists()
    assert not list(tmp_path.glob("ess-*.ndjson"))  # nothing settled yet

    # Cycle 2 (next slot): +2.0 kWh exported (+€0.60), SoC fell to 72%.
    energy_broker._settle_prior_slot(
        res, batt_soc=72.0,
        today_actuals={"imp_kwh": 1.0, "imp_cost": 0.20, "exp_kwh": 2.0, "exp_rev": 0.60}, now=t2)

    files = list(tmp_path.glob("ess-*.ndjson"))
    assert files
    recs = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    settlements = [r for r in recs if r.get("kind") == "settlement"]
    assert len(settlements) == 1
    s = settlements[0]
    assert s["predicted_control_action"] == "SELL"
    assert not s["incomplete"]
    assert abs(s["actual_export_kwh"] - 2.0) < 1e-6
    assert abs(s["actual_net_eur"] - 0.60) < 1e-6   # +0.60 reward − 0 added import
    assert abs(s["predicted_net_eur"] - 0.60) < 1e-6  # 2 kWh export @ €0.30
    assert abs(s["soc_delta"] - (-8.0)) < 1e-6
    # Cost-basis field is recorded (discharge slot -> basis present, may be 0).
    assert "cost_basis_eur_per_kwh" in s
    assert s["baseline_load_forecast_kwh"] == 0.6
    assert s["weather_load_shadow_kwh"] == 0.65
    assert s["final_load_forecast_kwh"] == 0.8
    assert s["baseline_pv_forecast_kwh"] == 0.9
    assert s["weather_pv_shadow_kwh"] == 0.75
    assert s["final_pv_forecast_kwh"] == 0.7
    assert s["weather_hvac_apply"] is False
    assert s["weather_pv_apply"] is False


def test_settlement_ignores_extra_cycle_within_same_slot(monkeypatch, tmp_path):
    # Regression: a mid-slot re-optimize (2nd cycle in the same 15-min slot) must NOT
    # emit a second settlement or advance the snapshot, or the slot's cost gets split /
    # dropped from the per-slot ledger (observed 2026-07-10: a €2.70 import slot booked 0).
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(tmp_path) if name == "HISTORY_DIR" else None)
    monkeypatch.setattr(energy_broker, "STATE",
                        DummyState({"c1_daily_yield": 0.0, "c2_daily_yield": 0.0}))
    monkeypatch.setattr(energy_broker, "_LAST_SLOT_PATH", str(tmp_path / "last_slot.json"))
    import lib.ess_cost_basis as _cb
    monkeypatch.setattr(_cb, "_path", lambda: str(tmp_path / "cost_basis.json"))

    res = {"control_action": "BUY", "slot_duration_h": 0.25,
           "schedule": [{"grid_energy": 9.0, "price": 0.30, "sell": 0.30}]}
    t0 = datetime(2026, 7, 10, 9, 15, tzinfo=timezone.utc)

    def settle(now, imp_cost, soc):
        energy_broker._settle_prior_slot(
            res, batt_soc=soc,
            today_actuals={"imp_kwh": 0.0, "imp_cost": imp_cost, "exp_kwh": 0.0, "exp_rev": 0.0},
            now=now)

    def settlements():
        files = list(tmp_path.glob("ess-*.ndjson"))
        if not files:
            return []
        return [json.loads(l) for l in files[0].read_text().splitlines()
                if l.strip() and json.loads(l).get("kind") == "settlement"]

    settle(t0, 3.21, 50.0)                          # slot-start snapshot (import @ 3.21)
    settle(t0 + timedelta(minutes=5), 5.91, 50.0)   # 2nd cycle SAME slot (counter jumped +2.70)
    assert settlements() == []                       # nothing settled yet, no partial/0 record

    settle(t0 + timedelta(minutes=15), 6.20, 40.0)  # next slot boundary -> settle the FULL slot
    recs = settlements()
    assert len(recs) == 1
    # Full slot delta from the slot-START snapshot (3.21) to the boundary (6.20) = 2.99,
    # i.e. it INCLUDES the mid-slot jump the old per-cycle logic dropped.
    assert abs(recs[0]["actual_cost"] - 2.99) < 1e-6
    assert recs[0]["slot_start"] == t0.isoformat()


def _patch_common_dependencies(monkeypatch, state_values=None, settings=None):
    state = DummyState(state_values or {})
    monkeypatch.setattr(energy_broker, "STATE", state)

    def fake_retrieve(name):
        overrides = settings or {}
        return overrides.get(name)

    monkeypatch.setattr(energy_broker, "retrieve_setting", fake_retrieve)
    monkeypatch.setattr(
        energy_broker,
        "get_seasonally_adjusted_max_charge_slots",
        lambda *args, **kwargs: 1,
    )
    monkeypatch.setattr(energy_broker, "lowest_24h_prices", MagicMock(return_value=[]))


def test_nightly_schedule_skips_with_high_soc(monkeypatch):
    _patch_common_dependencies(
        monkeypatch,
        state_values={"batt_soc": 85.0, "pv_projected_remaining": 0},
        settings={
            "NIGHT_CHARGE_SKIP_ENABLED": "1",
            "NIGHT_CHARGE_SKIP_MIN_SOC": "70",
            "NIGHT_CHARGE_SKIP_MAX_SOC": "100",
        },
    )
    lowest_48h_mock = MagicMock(return_value=[(0, "22", None, 0.1)])
    monkeypatch.setattr(energy_broker, "lowest_48h_prices", lowest_48h_mock)

    result = energy_broker.set_charging_schedule(
        caller="pytest",
        schedule_type="48h",
        charge_context="nightly",
        silent=True,
    )

    assert result is False
    lowest_48h_mock.assert_not_called()


def test_nightly_schedule_runs_when_skip_disabled(monkeypatch):
    _patch_common_dependencies(
        monkeypatch,
        state_values={"batt_soc": 85.0, "pv_projected_remaining": 0},
        settings={
            "NIGHT_CHARGE_SKIP_ENABLED": "0",
            "NIGHT_CHARGE_SKIP_MIN_SOC": "70",
            "NIGHT_CHARGE_SKIP_MAX_SOC": "100",
        },
    )
    lowest_48h_mock = MagicMock(return_value=[(0, "22", None, 0.1)])
    schedule_mock = MagicMock()
    clear_mock = MagicMock()
    remove_mock = MagicMock()

    monkeypatch.setattr(energy_broker, "lowest_48h_prices", lowest_48h_mock)
    monkeypatch.setattr(energy_broker, "schedule_victron_ess_charging", schedule_mock)
    monkeypatch.setattr(energy_broker, "clear_victron_schedules", clear_mock)
    monkeypatch.setattr(energy_broker, "remove_message", remove_mock)

    result = energy_broker.set_charging_schedule(
        caller="pytest",
        schedule_type="48h",
        charge_context="nightly",
        silent=True,
    )

    assert result is True
    clear_mock.assert_called_once()
    lowest_48h_mock.assert_called_once()
    schedule_mock.assert_called_once_with(22, schedule=0, day=0)
    remove_mock.assert_called()


# --- Manual grid-charge override -------------------------------------------

def test_realized_action_from_live_flow():
    # + = import/charge, - = export/discharge (W).
    assert energy_broker._realized_action(-8000, -10000) == "SELL"    # exporting + discharging
    assert energy_broker._realized_action(11000, 14000) == "BUY"      # importing + charging
    assert energy_broker._realized_action(1000, -50) == "RETAIN"      # grid covers load, batt held
    assert energy_broker._realized_action(-50, 2000) == "IDLE"        # PV charging, grid ~0
    assert energy_broker._realized_action(0, 0) == "IDLE"
    assert energy_broker._realized_action(None, None) == "IDLE"


def test_is_truthy_parses_false_string():
    # Regression: bool("False") is True, which made HOME_CONNECT_APPLIANCE_SCHEDULING
    # (and similar flags) ignore a "False" setting.
    from lib.helpers import is_truthy
    assert is_truthy("True") is True
    assert is_truthy("1") is True
    assert is_truthy("on") is True
    assert is_truthy("False") is False
    assert is_truthy("false") is False
    assert is_truthy("0") is False
    assert is_truthy("") is False
    assert is_truthy(None) is False
    assert is_truthy(True) is True
    assert is_truthy(None, default=True) is True


def test_manual_grid_charge_on_reads_toggle(monkeypatch):
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"grid_charging_enabled": "True"}))
    assert energy_broker._manual_grid_charge_on() is True
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"grid_charging_enabled": "False"}))
    assert energy_broker._manual_grid_charge_on() is False
    monkeypatch.setattr(energy_broker, "STATE", DummyState({}))
    assert energy_broker._manual_grid_charge_on() is False


def test_grid_assist_stands_down_during_ai_buy_slot(monkeypatch):
    # Regression: with manual grid-assist on under AI control, a retain "cover the load"
    # setpoint during an AI BUY slot clobbered the optimizer's (larger) charge setpoint and
    # improperly interfered with the buy. During BUY we must NOT apply a retain setpoint.
    monkeypatch.setattr(energy_broker, "_ai_optimizer_active_and_healthy", lambda: True)
    monkeypatch.setattr(energy_broker, "_manual_grid_charge_on", lambda: True)
    applied = []
    monkeypatch.setattr(energy_broker, "_apply_grid_assist_setpoint",
                        lambda *a, **k: applied.append((a, k)))

    monkeypatch.setattr(energy_broker, "STATE", DummyState({"ai_control_action": "BUY"}))
    energy_broker.manage_grid_usage_based_on_current_price(price=0.30, power=1500)
    assert applied == []                       # stood down during BUY

    monkeypatch.setattr(energy_broker, "STATE", DummyState({"ai_control_action": "RETAIN"}))
    energy_broker.manage_grid_usage_based_on_current_price(price=0.30, power=1500)
    assert len(applied) == 1                    # retain setpoint applied outside a BUY


def test_ai_ess_override_stands_optimizer_down(monkeypatch):
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: "1" if name == "AI_POWERED_ESS_ALGORITHM" else None)
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"ai_ess_override_enabled": "True", "batt_soc": 57}))
    prices = MagicMock(return_value=[{"start": "2026-06-29T09:00:00+02:00", "total": 0.2}])
    monkeypatch.setattr(energy_broker, "get_all_price_points", prices)
    optimizer = MagicMock()
    monkeypatch.setattr(energy_broker, "optimize_schedule", optimizer)

    energy_broker.run_ai_optimizer()

    prices.assert_not_called()
    optimizer.assert_not_called()


def test_run_ai_optimizer_skips_when_optimizer_lock_is_held(monkeypatch, caplog):
    runner = MagicMock()
    monkeypatch.setattr(energy_broker, "_run_ai_optimizer_once", runner)
    caplog.set_level("INFO")

    acquired = energy_broker._AI_OPTIMIZER_LOCK.acquire(blocking=False)
    assert acquired
    try:
        assert energy_broker.run_ai_optimizer() is False
    finally:
        energy_broker._AI_OPTIMIZER_LOCK.release()

    runner.assert_not_called()
    assert "Optimization already running" in caplog.text


def test_ai_optimizer_skips_when_soc_key_missing_but_voltage_exists(monkeypatch, caplog):
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: "1" if name == "AI_POWERED_ESS_ALGORITHM" else None)
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"batt_voltage": 53.4}))
    prices = MagicMock(return_value=[{"start": "2026-06-28T09:15:00+02:00"}])
    monkeypatch.setattr(energy_broker, "get_all_price_points", prices)

    energy_broker.run_ai_optimizer()

    prices.assert_not_called()
    assert "Battery SoC not available yet" in caplog.text


def test_ai_optimizer_accepts_reported_zero_soc(monkeypatch):
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: "1" if name == "AI_POWERED_ESS_ALGORITHM" else None)
    monkeypatch.setattr(energy_broker, "STATE", DummyState({"batt_soc": 0, "batt_voltage": 53.4}))
    prices = MagicMock(return_value=[])
    monkeypatch.setattr(energy_broker, "get_all_price_points", prices)

    energy_broker.run_ai_optimizer()

    prices.assert_called_once()


def test_ai_optimizer_applies_pv_nowcast_when_weather_unavailable(monkeypatch):
    from datetime import datetime, timedelta, timezone
    import lib.weather as weather

    start = datetime(2026, 6, 29, 13, 0, tzinfo=timezone.utc)
    prices = [
        {"start": (start + timedelta(minutes=15 * i)).isoformat(), "total": 0.20}
        for i in range(2)
    ]
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: "1" if name == "AI_POWERED_ESS_ALGORITHM" else None,
    )
    monkeypatch.setattr(
        energy_broker,
        "STATE",
        DummyState({
            "batt_soc": 42,
            "ac_in_power": 0,
            "pv_power": 6000,
            "ac_out_power": 1000,
            "batt_power": 5000,
            "pv_projected_remaining": 8000,
        }),
    )
    monkeypatch.setattr(energy_broker, "get_all_price_points", lambda: prices)
    monkeypatch.setattr(
        energy_broker,
        "_build_pv_forecast_by_slot",
        lambda slots, slot_h: {slot["start"]: 0.1 for slot in slots},
    )
    monkeypatch.setattr(
        energy_broker,
        "_build_load_forecast_by_slot",
        lambda slots, slot_h: {slot["start"]: 0.2 for slot in slots},
    )
    monkeypatch.setattr(
        weather,
        "weather_context_for_slots",
        lambda *args, **kwargs: {"available": False, "summary": {}, "slots": {}},
    )
    nowcast = MagicMock(side_effect=lambda pv, slots, ctx, slot_h: pv)
    monkeypatch.setattr(energy_broker, "_apply_pv_nowcast", nowcast)
    monkeypatch.setattr(
        energy_broker,
        "optimize_schedule",
        lambda *args, **kwargs: {
            "schedule": [{
                "time": prices[0]["start"],
                "control_action": "IDLE",
                "soc_start": 42.0,
                "soc_end": 42.0,
                "grid_energy": 0.0,
                "price": 0.20,
            }],
            "victron_slots": [],
            "slot_duration_h": 0.25,
            "setpoint": 0.0,
            "control_action": "IDLE",
            "grid_assist": False,
            "mode": "hold",
            "current_price": 0.20,
            "limit_feed_in": False,
        },
    )
    monkeypatch.setattr(energy_broker, "_set_grid_assist", lambda enabled: None)
    monkeypatch.setattr(energy_broker, "ac_power_setpoint", lambda **kwargs: None)
    monkeypatch.setattr(energy_broker, "clear_victron_schedules", lambda: None)
    monkeypatch.setattr(energy_broker, "get_today_energy_actuals", lambda: {})
    monkeypatch.setattr(energy_broker, "_append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(energy_broker, "_settle_prior_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(energy_broker, "_publish_plan_json", lambda *args, **kwargs: None)

    assert energy_broker.run_ai_optimizer() is True

    nowcast.assert_called_once()
    assert nowcast.call_args.args[2]["available"] is False


def test_ai_optimizer_overlays_ev_and_blocks_stationary_battery_discharge(monkeypatch):
    from datetime import datetime, timedelta, timezone
    import lib.weather as weather

    start = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    prices = [
        {"start": (start + timedelta(minutes=15 * i)).isoformat(), "total": 0.20}
        for i in range(2)
    ]
    settings = {
        "AI_POWERED_ESS_ALGORITHM": "1",
        "EV_SMART_CHARGE_ENABLED": "1",
        "EV_SMART_CHARGE_APPLY": "1",
        "EV_ALLOW_ESS_DISCHARGE": "0",
    }
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: settings.get(name))
    monkeypatch.setattr(energy_broker, "STATE", DummyState({
        "batt_soc": 60,
        "tesla_soc": 25,
        "ac_in_power": 0,
        "pv_power": 0,
        "ac_out_power": 1000,
        "batt_power": 0,
    }))
    monkeypatch.setattr(energy_broker, "get_all_price_points", lambda: prices)
    monkeypatch.setattr(
        energy_broker, "_build_pv_forecast_by_slot",
        lambda slots, slot_h: {slot["start"]: 0.0 for slot in slots})
    monkeypatch.setattr(
        energy_broker, "_build_load_forecast_by_slot",
        lambda slots, slot_h: {slot["start"]: 0.2 for slot in slots})
    monkeypatch.setattr(
        weather, "weather_context_for_slots",
        lambda *args, **kwargs: {"available": False, "summary": {}, "slots": {}})
    monkeypatch.setattr(
        energy_broker, "_apply_appliance_reservations_to_forecast",
        lambda load, **kwargs: (load, {"enabled": False}))
    monkeypatch.setattr(energy_broker, "_apply_pv_nowcast", lambda pv, *args: pv)
    selected_start = prices[0]["start"]
    ev_plan = {
        "active": True,
        "status": "planned",
        "job": {"id": "job-1", "status": "active"},
        "slots": [{
            "start": selected_start,
            "end": prices[1]["start"],
            "energy_kwh": 3.5,
            "requested_power_kw": 14.0,
            "supply": "grid",
        }],
    }
    monkeypatch.setattr(
        energy_broker, "_apply_ev_smart_charge_to_forecast",
        lambda load, *args, **kwargs: (
            {key: value + (3.5 if index == 0 else 0.0)
             for index, (key, value) in enumerate(load.items())},
            {"enabled": True, "apply": True, "active": True,
             "ess_overlay_applied": True, "status": "planned",
             "job_id": "job-1", "plan": ev_plan},
        ),
    )
    optimizer_calls = []

    def optimizer(*args, **kwargs):
        optimizer_calls.append((args, kwargs))
        return {
            "schedule": [{
                "time": selected_start, "action": "hold", "control_action": "IDLE",
                "soc_start": 60.0, "soc_end": 60.0, "grid_energy": 3.7,
                "price": 0.20, "sell": 0.20, "load": args[2][next(iter(args[2]))],
            }],
            "victron_slots": [], "slot_duration_h": 0.25, "setpoint": 0.0,
            "control_action": "IDLE", "grid_assist": False, "mode": "hold",
            "current_price": 0.20, "limit_feed_in": False,
        }

    monkeypatch.setattr(energy_broker, "optimize_schedule", optimizer)
    monkeypatch.setattr(energy_broker, "_set_grid_assist", lambda enabled: None)
    monkeypatch.setattr(energy_broker, "ac_power_setpoint", lambda **kwargs: None)
    monkeypatch.setattr(energy_broker, "clear_victron_schedules", lambda: None)
    monkeypatch.setattr(energy_broker, "get_today_energy_actuals", lambda: {})
    monkeypatch.setattr(energy_broker, "_append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(energy_broker, "_settle_prior_slot", lambda *args, **kwargs: None)
    published = []
    monkeypatch.setattr(
        energy_broker, "_publish_plan_json",
        lambda result, **kwargs: published.append(result),
    )

    assert energy_broker.run_ai_optimizer() is True
    assert optimizer_calls[0][0][2][next(iter(optimizer_calls[0][0][2]))] == pytest.approx(3.7)
    assert optimizer_calls[0][1]["discharge_blocked_slots"] == {selected_start}
    assert published[0]["schedule"][0]["planned_ev_kwh"] == pytest.approx(3.5)
    assert published[0]["schedule"][0]["non_ev_load_kwh"] == pytest.approx(0.2)


def test_winter_optimizer_failure_clears_stale_control_and_retains(monkeypatch):
    from datetime import datetime, timedelta, timezone
    import lib.weather as weather

    start = datetime.now(timezone.utc)
    prices = [
        {"start": (start + timedelta(minutes=15 * i)).isoformat(), "total": 0.20}
        for i in range(2)
    ]
    monkeypatch.setattr(energy_broker, "OPTIMIZER_MODE", "winter")
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: "1" if name == "AI_POWERED_ESS_ALGORITHM" else None,
    )
    monkeypatch.setattr(
        energy_broker,
        "STATE",
        DummyState({"batt_soc": 42, "ac_in_power": 0, "pv_power": 0,
                    "ac_out_power": 1000, "batt_power": -1000}),
    )
    monkeypatch.setattr(energy_broker, "get_all_price_points", lambda: prices)
    monkeypatch.setattr(energy_broker, "_build_pv_forecast_by_slot", lambda *args: {})
    monkeypatch.setattr(energy_broker, "_build_load_forecast_by_slot", lambda *args: {})
    monkeypatch.setattr(energy_broker, "_apply_pv_nowcast", lambda pv, *args: pv)
    monkeypatch.setattr(
        weather, "weather_context_for_slots",
        lambda *args, **kwargs: {"available": False, "summary": {}, "slots": {}},
    )
    monkeypatch.setattr(energy_broker, "optimize_schedule", lambda *args: None)
    monkeypatch.setattr(energy_broker, "current_min_soc_reserve", lambda: 40.0)
    minimum_soc = MagicMock()
    clear_slots = MagicMock()
    grid_assist = MagicMock()
    monkeypatch.setattr(energy_broker, "set_minimum_ess_soc", minimum_soc)
    monkeypatch.setattr(energy_broker, "clear_victron_schedules", clear_slots)
    monkeypatch.setattr(energy_broker, "_set_grid_assist", grid_assist)
    monkeypatch.setattr(energy_broker, "_apply_grid_assist_setpoint", lambda **kwargs: None)
    monkeypatch.setattr(energy_broker, "_grid_assist_setpoint_watts", lambda **kwargs: 1000)
    monkeypatch.setattr(energy_broker, "limit_grid_feed_in", lambda **kwargs: None)
    monkeypatch.setattr(energy_broker, "get_today_energy_actuals", lambda: {})
    monkeypatch.setattr(energy_broker, "_append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(energy_broker, "_settle_prior_slot", lambda *args, **kwargs: None)
    published = []
    monkeypatch.setattr(
        energy_broker, "_publish_plan_json",
        lambda result, **kwargs: published.append(result.copy()),
    )

    assert energy_broker.run_ai_optimizer() is True

    minimum_soc.assert_called_once()
    clear_slots.assert_called_once()
    grid_assist.assert_called_with(True)
    assert published[0]["optimizer_mode"] == "winter"
    assert published[0]["control_action"] == "RETAIN"
    assert published[0]["winter_policy"]["warning"] == "optimizer_failed_safe_retain"
    assert energy_broker.STATE.get("ai_optimizer_mode") == "winter"
    assert energy_broker.STATE.get("ai_winter_candidate") == "self_sufficiency"
    assert energy_broker.STATE.get("ai_winter_protected_soc") == 40.0
    assert energy_broker.STATE.get("ai_winter_warning") == "optimizer_failed_safe_retain"


def test_low_soc_idle_retain_defers_to_cheaper_planned_buy(monkeypatch):
    monkeypatch.setattr(energy_broker, "current_min_soc_reserve", lambda: 0.0)
    result = {
        "control_action": "IDLE",
        "grid_assist": False,
        "mode": "hold",
        "setpoint": 0.0,
        "current_price": 0.182,
        "schedule": [
            {"control_action": "IDLE", "price": 0.182},
            {"control_action": "BUY", "price": 0.134},
        ],
    }

    out = energy_broker._apply_low_soc_retain_before_cheaper_buy(result, batt_soc=0.0)

    assert out["control_action"] == "RETAIN"
    assert out["grid_assist"] is True
    assert out["mode"] == "hold"
    assert out["setpoint"] == 0.0
    assert out["reason_code"] == "LOW_SOC_DEFER_CHEAPER_BUY"


def test_low_soc_idle_retain_uses_existing_reserve_threshold(monkeypatch):
    monkeypatch.setattr(energy_broker, "current_min_soc_reserve", lambda: 40.0)
    result = {
        "control_action": "IDLE",
        "grid_assist": False,
        "mode": "hold",
        "setpoint": 0.0,
        "current_price": 0.182,
        "schedule": [
            {"control_action": "IDLE", "price": 0.182},
            {"control_action": "BUY", "price": 0.134},
        ],
    }

    out = energy_broker._apply_low_soc_retain_before_cheaper_buy(result, batt_soc=41.0)

    assert out["control_action"] == "IDLE"
    assert out["grid_assist"] is False


def test_low_soc_idle_retain_requires_cheaper_future_buy(monkeypatch):
    monkeypatch.setattr(energy_broker, "current_min_soc_reserve", lambda: 0.0)
    result = {
        "control_action": "IDLE",
        "grid_assist": False,
        "mode": "hold",
        "setpoint": 0.0,
        "current_price": 0.132,
        "schedule": [
            {"control_action": "IDLE", "price": 0.132},
            {"control_action": "BUY", "price": 0.134},
        ],
    }

    out = energy_broker._apply_low_soc_retain_before_cheaper_buy(result, batt_soc=0.0)

    assert out["control_action"] == "IDLE"
    assert out["grid_assist"] is False


def test_grid_charge_cap_removes_active_victron_slot_when_reached(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 6, 28, 12, 15, tzinfo=timezone.utc)
    active = {"start": now - timedelta(minutes=15), "duration": 3600, "target_soc": 96}
    later = {"start": now + timedelta(hours=4), "duration": 3600, "target_soc": 94}
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: "90" if name == "ESS_MAX_GRID_CHARGE_SOC" else None,
    )

    filtered = energy_broker._filter_victron_slots_for_grid_charge_cap(
        [active, later],
        batt_soc=90.0,
        now=now,
    )

    assert filtered == [{"start": later["start"], "duration": 3600, "target_soc": 90}]


def test_grid_charge_cap_keeps_slots_below_threshold(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 6, 28, 12, 15, tzinfo=timezone.utc)
    active = {"start": now - timedelta(minutes=15), "duration": 3600, "target_soc": 90}
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: "90" if name == "ESS_MAX_GRID_CHARGE_SOC" else None,
    )

    filtered = energy_broker._filter_victron_slots_for_grid_charge_cap(
        [active],
        batt_soc=89.0,
        now=now,
    )

    assert filtered == [active]


# --- SELL hysteresis (minimum dwell + price band) ---------------------------

def test_sell_hysteresis_suppresses_quick_reentry(monkeypatch, tmp_path):
    # We stopped selling moments ago at €0.30; a fresh SELL at ~the same price
    # within the dwell window must be damped to a hold (RETAIN).
    monkeypatch.setattr(energy_broker, "_SELL_STATE_PATH", str(tmp_path / "sell.json"))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)  # defaults: 20min / €0.03
    (tmp_path / "sell.json").write_text(json.dumps(
        {"sell_on": False, "ts": __import__("time").time(), "price": 0.30}))

    result = {"control_action": "SELL", "grid_assist": False, "mode": "sell",
              "setpoint": -5000.0, "current_price": 0.305}
    out = energy_broker._apply_sell_hysteresis(result)
    assert out["control_action"] == "RETAIN"
    assert out["grid_assist"] is True
    assert out["setpoint"] == 0.0
    assert out["reason_code"] == "SELL_DAMPED_HYSTERESIS"


def test_sell_hysteresis_allows_reentry_on_big_price_move(monkeypatch, tmp_path):
    # Same recent stop at €0.30, but the price has jumped to €0.40 (> €0.03 band)
    # -> the SELL is allowed through.
    monkeypatch.setattr(energy_broker, "_SELL_STATE_PATH", str(tmp_path / "sell.json"))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    (tmp_path / "sell.json").write_text(json.dumps(
        {"sell_on": False, "ts": __import__("time").time(), "price": 0.30}))

    result = {"control_action": "SELL", "grid_assist": False, "mode": "sell",
              "setpoint": -5000.0, "current_price": 0.40}
    out = energy_broker._apply_sell_hysteresis(result)
    assert out["control_action"] == "SELL"


def test_sell_hysteresis_never_blocks_exit(monkeypatch, tmp_path):
    # Currently selling; the optimizer now wants to hold. Exiting SELL is always
    # allowed (the guard only ever suppresses *entering* a sell).
    monkeypatch.setattr(energy_broker, "_SELL_STATE_PATH", str(tmp_path / "sell.json"))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: None)
    (tmp_path / "sell.json").write_text(json.dumps(
        {"sell_on": True, "ts": __import__("time").time(), "price": 0.30}))

    result = {"control_action": "RETAIN", "grid_assist": True, "mode": "hold",
              "setpoint": 0.0, "current_price": 0.305}
    out = energy_broker._apply_sell_hysteresis(result)
    assert out["control_action"] == "RETAIN"
