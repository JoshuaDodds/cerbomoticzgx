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


def test_load_forecast_prefers_historical_average(monkeypatch):
    from datetime import datetime
    # Realised 0.8 kW for the 06:00 quarter-hours over the last few days.
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
