from pathlib import Path
import sys
import json
import types
from unittest.mock import MagicMock

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


def test_current_min_soc_reserve_is_seasonal(monkeypatch):
    import sys
    import lib.helpers as helpers
    cr = sys.modules.get("lib.config_retrieval")
    monkeypatch.setattr(cr, "retrieve_setting",
                        lambda n: {"MIN_SOC_RESERVE_WINTER": "40", "MIN_SOC_RESERVE_SUMMER": "0"}.get(n))

    monkeypatch.setattr(helpers, "is_winter_month", lambda: True)
    assert helpers.current_min_soc_reserve() == 40.0

    monkeypatch.setattr(helpers, "is_winter_month", lambda: False)
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

    res = {"control_action": "SELL", "slot_duration_h": 0.25,
           "schedule": [{"grid_energy": -2.0, "price": 0.30, "sell": 0.30}]}

    # Cycle 1: counters at import 1.0 kWh (€0.20), no export; SoC 80%.
    energy_broker._settle_prior_slot(
        res, batt_soc=80.0,
        today_actuals={"imp_kwh": 1.0, "imp_cost": 0.20, "exp_kwh": 0.0, "exp_rev": 0.0})
    assert (tmp_path / "last_slot.json").exists()
    assert not list(tmp_path.glob("ess-*.ndjson"))  # nothing settled yet

    # Cycle 2: +2.0 kWh exported (+€0.60), SoC fell to 72%.
    energy_broker._settle_prior_slot(
        res, batt_soc=72.0,
        today_actuals={"imp_kwh": 1.0, "imp_cost": 0.20, "exp_kwh": 2.0, "exp_rev": 0.60})

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
