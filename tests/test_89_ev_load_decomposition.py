"""Regression tests for measured EV/base-load classification and safe learning."""
from pathlib import Path
import sys
import types
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Reuse the same lightweight stubs the sibling energy_broker test installs, so
# importing lib.energy_broker never reaches real Tibber/Victron/config modules.
for name, attrs in {
    "lib.tibber_api": {"lowest_48h_prices": MagicMock(return_value=[]),
                       "lowest_24h_prices": MagicMock(return_value=[]),
                       "publish_pricing_data": MagicMock(),
                       "get_all_price_points": MagicMock(return_value=[])},
    "lib.victron_integration": {"ac_power_setpoint": MagicMock(),
                                "limit_grid_feed_in": MagicMock(),
                                "set_minimum_ess_soc": MagicMock()},
}.items():
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)

stub_cfg = types.ModuleType("lib.config_retrieval")
stub_cfg.retrieve_setting = lambda name: {"DAILY_HOME_ENERGY_CONSUMPTION": "12"}.get(name)
sys.modules.setdefault("lib.config_retrieval", stub_cfg)

import lib.energy_broker as energy_broker  # noqa: E402
import lib.constants as constants  # noqa: E402
# Keep the package attribute consistent with sys.modules so this test remains
# isolated when collected before tests that monkeypatch via a dotted path.
import lib as lib_package  # noqa: E402
lib_package.config_retrieval = sys.modules["lib.config_retrieval"]


class DummyState:
    def __init__(self, values):
        self._values = values

    def get(self, key):
        return self._values.get(key)

    def set(self, key, value):
        self._values[key] = value

    def has(self, key):
        return key in self._values


# ---------------------------------------------------------------------------
# L1: ABB meter topic contract
# ---------------------------------------------------------------------------

def test_abb_phase_current_topics_use_ac_hierarchy():
    topics = constants.Topics["system0"]
    for phase in (1, 2, 3):
        assert topics[f"tesla_l{phase}_current"].endswith(
            f"/evcharger/42/Ac/L{phase}/Current"
        )


# ---------------------------------------------------------------------------
# L3: robust bucket reducer
# ---------------------------------------------------------------------------

def test_robust_bucket_caps_outliers():
    # 30 clean samples at 0.3 kW, 10 EV-contaminated at 17 kW. A plain mean would
    # be pulled to ~4.5 kW; the robust reducer must stay near the clean level.
    vals = [0.3] * 30 + [17.0] * 10
    plain = sum(vals) / len(vals)
    robust = energy_broker._robust_bucket_kw(vals)
    assert plain > 4.0
    assert robust < 1.0          # outliers capped, not averaged in


def test_robust_bucket_edge_cases():
    assert energy_broker._robust_bucket_kw([]) is None
    assert energy_broker._robust_bucket_kw([0.5]) == 0.5
    # All-equal bucket: MAD==0 fallback must not distort a genuinely flat bucket.
    assert abs(energy_broker._robust_bucket_kw([0.4, 0.4, 0.4]) - 0.4) < 1e-9


# ---------------------------------------------------------------------------
# L2: forecaster prefers base_load_w, falls back to load_w
# ---------------------------------------------------------------------------

def test_historical_load_prefers_base_load_w(monkeypatch):
    day_rows = [
        # New records: EV charging inflates load_w to 17 kW but base_load_w is clean.
        {"kind": "cycle", "ts": "2026-07-16T06:00:05+02:00", "load_w": 17000, "base_load_w": 500, "batt_w": 1000},
        {"kind": "cycle", "ts": "2026-07-16T06:05:05+02:00", "load_w": 17000, "base_load_w": 450, "batt_w": 1000},
        # Old record: no base_load_w -> fall back to load_w (clean day, no EV).
        {"kind": "cycle", "ts": "2026-07-16T06:10:05+02:00", "load_w": 600, "batt_w": 1000},
        # Settlement rows are ignored by the load learner.
        {"kind": "settlement", "ts": "2026-07-16T06:15:05+02:00", "actual_load_kwh": 3.1},
    ]
    # _historical_load_by_slot imports its own datetime locally and buckets by the
    # HH:MM of each record's ts, so the read_day date is irrelevant here — return the
    # rows for whatever day it asks for.
    fake_hist = types.SimpleNamespace(read_day=lambda d, hd: day_rows)
    monkeypatch.setattr(energy_broker, "_hist", fake_hist)
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: "data/history")

    out = energy_broker._historical_load_by_slot(days=1)
    # The 06:00 bucket must reflect the ~0.5 kW base load, not the 17 kW raw load.
    assert out["06:00"] < 1.0


def test_historical_load_skips_unclassified_legacy_ev_spikes(monkeypatch):
    rows = [
        {"kind": "cycle", "ts": "2026-07-16T12:45:05+02:00", "load_w": 17000, "batt_w": 1000},
        {"kind": "cycle", "ts": "2026-07-16T13:00:05+02:00", "load_w": 900, "batt_w": 1000},
    ]
    monkeypatch.setattr(energy_broker, "_hist", types.SimpleNamespace(read_day=lambda d, hd: rows))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: "data/history")

    out = energy_broker._historical_load_by_slot(days=1)

    assert "12:45" not in out
    assert out["13:00"] == 0.9


def test_pv_forecast_promotes_yesterdays_dated_tomorrow_forecast_at_midnight(monkeypatch):
    # Before the first successful new-day VRM refresh, yesterday's retained
    # `remaining=0` must not make the optimizer assume a zero-PV day. Yesterday's
    # explicitly dated tomorrow forecast is today's best available forecast.
    today = datetime.now().astimezone().date()
    state = DummyState({
        "pv_projected_remaining": 0.0,
        "pv_projected_today_date": (today - timedelta(days=1)).isoformat(),
        "pv_projected_tomorrow": 25853.0,
        "pv_projected_tomorrow_date": today.isoformat(),
    })
    monkeypatch.setattr(energy_broker, "STATE", state)
    monkeypatch.setattr(energy_broker, "_pv_shape_by_slot", lambda days: {})
    slots = [
        {"start": datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)},
        {"start": datetime.now().astimezone().replace(hour=13, minute=0, second=0, microsecond=0)},
    ]

    forecast = energy_broker._build_pv_forecast_by_slot(slots, 1.0)

    assert round(sum(forecast.values()), 3) == 25.853


def test_historical_load_deduplicates_replans_within_each_day_slot(monkeypatch):
    today_rows = [
        {"kind": "cycle", "ts": "2026-07-16T13:00:05+02:00", "load_w": 17000,
         "base_load_w": 500, "batt_w": 1000},
        {"kind": "cycle", "ts": "2026-07-16T13:05:05+02:00", "load_w": 17000,
         "base_load_w": 700, "batt_w": 1000},
        {"kind": "cycle", "ts": "2026-07-16T13:10:05+02:00", "load_w": 17000,
         "base_load_w": 900, "batt_w": 1000},
    ]
    prior_rows = [
        {"kind": "cycle", "ts": "2026-07-15T13:00:05+02:00", "load_w": 800,
         "base_load_w": 800, "batt_w": 1000},
    ]
    calls = {"n": 0}

    def read_day(_day, _history_dir):
        calls["n"] += 1
        return today_rows if calls["n"] == 1 else prior_rows

    monkeypatch.setattr(energy_broker, "_hist", types.SimpleNamespace(read_day=read_day))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: "data/history")

    out = energy_broker._historical_load_by_slot(days=2)

    # Day 1 contributes one daily median (0.7), not three independently weighted
    # optimizer/replan samples; day 2 contributes 0.8.
    assert out["13:00"] == 0.75


def test_historical_load_applies_robust_reducer_to_measured_base(monkeypatch):
    per_day = iter((
        [{"kind": "cycle", "ts": "2026-07-16T06:00:05+02:00", "load_w": 500,
          "base_load_w": 500, "batt_w": 0}],
        [{"kind": "cycle", "ts": "2026-07-15T06:00:05+02:00", "load_w": 600,
          "base_load_w": 600, "batt_w": 0}],
        [{"kind": "cycle", "ts": "2026-07-14T06:00:05+02:00", "load_w": 8000,
          "base_load_w": 8000, "batt_w": 0}],
    ))
    monkeypatch.setattr(energy_broker, "_hist",
                        types.SimpleNamespace(read_day=lambda d, hd: next(per_day)))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: "data/history")

    out = energy_broker._historical_load_by_slot(days=3)

    assert out["06:00"] < 1.0


def test_pv_shape_keeps_plain_mean_not_load_outlier_policy(monkeypatch):
    rows = [
        {"kind": "cycle", "ts": "2026-07-16T12:00:05+02:00", "pv_w": 1000},
        {"kind": "cycle", "ts": "2026-07-16T12:05:05+02:00", "pv_w": 1000},
        {"kind": "cycle", "ts": "2026-07-16T12:10:05+02:00", "pv_w": 10000},
    ]
    monkeypatch.setattr(energy_broker, "_hist", types.SimpleNamespace(read_day=lambda d, hd: rows))
    monkeypatch.setattr(energy_broker, "retrieve_setting", lambda name: "data/history")

    assert energy_broker._pv_shape_by_slot(days=1)["12:00"] == 4.0


def test_cycle_load_classification_requires_coherent_meter_data():
    assert energy_broker._classify_cycle_load(17000, 16000, meter_available=True) == (1000.0, "measured")
    assert energy_broker._classify_cycle_load(17000, 0, meter_available=True) == (17000.0, "measured")
    assert energy_broker._classify_cycle_load(17000, None, meter_available=False) == (None, "ev_meter_missing")
    assert energy_broker._classify_cycle_load(17000, 16000, meter_available=True,
                                              meter_fresh=False) == (None, "ev_meter_stale")
    assert energy_broker._classify_cycle_load(1000, 16000, meter_available=True) == (None, "ev_power_incoherent")
    assert energy_broker._classify_cycle_load(1000, -10, meter_available=True) == (None, "ev_power_incoherent")
    assert energy_broker._classify_cycle_load(30000, 25000, meter_available=True) == (None, "ev_power_incoherent")
    assert energy_broker._classify_cycle_load(1000, float("nan"), meter_available=True) == (None, "ev_power_incoherent")


def test_cycle_history_records_measurement_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(tmp_path) if name == "HISTORY_DIR" else None)
    monkeypatch.setattr(energy_broker, "STATE", DummyState({}))
    result = {"schedule": [], "control_action": "IDLE", "mode": "hold",
              "reason_code": "TEST", "weather_context": {}}

    energy_broker._append_history(
        result, batt_soc=50, applied_setpoint=0, today_actuals={},
        realized_power={"load_w": 17000, "ev_w": 16000, "ev_meter_available": True,
                        "grid_w": 0, "pv_w": 0, "batt_w": 0})

    path = next(tmp_path.glob("ess-*.ndjson"))
    rec = json.loads(path.read_text().splitlines()[-1])
    assert rec["ev_w"] == 16000.0
    assert rec["base_load_w"] == 1000.0
    assert rec["load_decomposition_quality"] == "measured"


def test_cycle_history_records_projected_final_net_for_current_day(monkeypatch, tmp_path):
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(tmp_path) if name == "HISTORY_DIR" else None)
    monkeypatch.setattr(energy_broker, "STATE", DummyState({}))
    result = {
        "schedule": [
            {
                "time": now,
                "grid_energy": -2.0,
                "price": 0.30,
                "sell": 0.30,
                "reason_code": "PRICE_PEAK",
                "soc_start": 50,
            },
            {
                # Tomorrow remains part of the legacy whole-horizon metric but
                # must never distort today's final-net forecast candle.
                "time": now + timedelta(days=1),
                "grid_energy": 10.0,
                "price": 0.50,
                "sell": 0.50,
                "reason_code": "GRID_CHARGE",
                "soc_start": 20,
            },
        ],
        "control_action": "SELL",
        "mode": "sell",
        "reason_code": "PRICE_PEAK",
        "current_price": 0.30,
        "weather_context": {},
    }

    energy_broker._append_history(
        result,
        batt_soc=50,
        applied_setpoint=-1000,
        today_actuals={"imp_cost": 2.0, "exp_rev": 1.0},
        realized_power={},
    )

    path = next(tmp_path.glob("ess-*.ndjson"))
    rec = json.loads(path.read_text().splitlines()[-1])
    # Actual net so far is -€1.00; remaining SELL forecast adds +€0.60.
    assert rec["forecast_day_net_eur"] == -0.4
    assert rec["plan_today_remaining_net_eur"] == 0.6
    assert rec["forecast_remaining_import_cost_eur"] == 0.0
    assert rec["forecast_remaining_export_reward_eur"] == 0.6
    assert rec["plan_horizon_net_eur"] == -4.4


# ---------------------------------------------------------------------------
# L1: settlement decomposition from the meter totalizer diff
# ---------------------------------------------------------------------------

def test_settlement_splits_ev_and_base(monkeypatch, tmp_path):
    monkeypatch.setattr(energy_broker, "_LAST_SLOT_PATH", str(tmp_path / "last_slot.json"))
    hist_dir = tmp_path / "history"
    hist_dir.mkdir()
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(hist_dir) if name == "HISTORY_DIR" else None)

    state = DummyState({
        "consumption_total_cumulative": 10000.0,   # Wh
        "tesla_charge_energy_forward": 100.0,       # kWh lifetime
        "c1_daily_yield": 0.0, "c2_daily_yield": 0.0,
    })
    monkeypatch.setattr(energy_broker, "STATE", state)

    def _result():
        return {"schedule": [{"time": datetime(2026, 7, 16, 12, 0), "grid_energy": 0.0,
                              "price": 0.25, "sell": 0.25, "pv": 0.2, "load": 0.3}],
                "slot_duration_h": 0.25, "control_action": "IDLE", "weather_context": {}}

    t0 = datetime(2026, 7, 16, 12, 0, 0).astimezone()
    energy_broker._settle_prior_slot(_result(), batt_soc=50.0, today_actuals={}, now=t0)

    # Next slot: +3 kWh total consumption, +2.8 kWh of it EV charge.
    state.set("consumption_total_cumulative", 13000.0)
    state.set("tesla_charge_energy_forward", 102.8)
    t1 = t0 + timedelta(minutes=15)
    energy_broker._settle_prior_slot(_result(), batt_soc=50.0, today_actuals={}, now=t1)

    path = hist_dir / f"ess-{t1.strftime('%Y-%m-%d')}.ndjson"
    settlement = [json.loads(l) for l in path.read_text().splitlines()][-1]
    assert settlement["kind"] == "settlement"
    assert abs(settlement["actual_load_kwh"] - 3.0) < 1e-6
    assert abs(settlement["ev_charge_kwh"] - 2.8) < 1e-6
    assert abs(settlement["base_load_kwh"] - 0.2) < 1e-6


def test_settlement_base_clamped_non_negative(monkeypatch, tmp_path):
    # EV meter reads slightly more than the consumption counter for a slot -> base
    # must clamp to 0, never go negative.
    monkeypatch.setattr(energy_broker, "_LAST_SLOT_PATH", str(tmp_path / "ls.json"))
    hist_dir = tmp_path / "h"
    hist_dir.mkdir()
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(hist_dir) if name == "HISTORY_DIR" else None)
    state = DummyState({"consumption_total_cumulative": 0.0, "tesla_charge_energy_forward": 0.0,
                        "c1_daily_yield": 0.0, "c2_daily_yield": 0.0})
    monkeypatch.setattr(energy_broker, "STATE", state)
    res = {"schedule": [{"time": datetime(2026, 7, 16, 12, 0), "grid_energy": 0.0,
                         "price": 0.2, "sell": 0.2, "pv": 0.0, "load": 0.0}],
           "slot_duration_h": 0.25, "control_action": "IDLE", "weather_context": {}}
    t0 = datetime(2026, 7, 16, 12, 0, 0).astimezone()
    energy_broker._settle_prior_slot(res, batt_soc=50.0, today_actuals={}, now=t0)
    state.set("consumption_total_cumulative", 2000.0)   # +2.0 kWh total
    state.set("tesla_charge_energy_forward", 2.2)       # +2.2 kWh EV (meter noise)
    t1 = t0 + timedelta(minutes=15)
    energy_broker._settle_prior_slot(res, batt_soc=50.0, today_actuals={}, now=t1)
    rec = [json.loads(l) for l in (hist_dir / f"ess-{t1.strftime('%Y-%m-%d')}.ndjson").read_text().splitlines()][-1]
    assert rec["base_load_kwh"] == 0.0


def test_settlement_missing_meter_does_not_create_lifetime_delta(monkeypatch, tmp_path):
    monkeypatch.setattr(energy_broker, "_LAST_SLOT_PATH", str(tmp_path / "slot.json"))
    hist_dir = tmp_path / "history"
    hist_dir.mkdir()
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(hist_dir) if name == "HISTORY_DIR" else None)
    state = DummyState({"consumption_total_cumulative": 10000.0,
                        "c1_daily_yield": 0.0, "c2_daily_yield": 0.0})
    monkeypatch.setattr(energy_broker, "STATE", state)
    result = {"schedule": [{"time": datetime(2026, 7, 16, 12, 0), "grid_energy": 0,
                            "price": 0.2, "sell": 0.2, "pv": 0, "load": 0}],
              "slot_duration_h": 0.25, "control_action": "IDLE", "weather_context": {}}
    t0 = datetime(2026, 7, 16, 12, 0).astimezone()
    energy_broker._settle_prior_slot(result, batt_soc=50, today_actuals={}, now=t0)

    state.set("tesla_charge_energy_forward", 12345.0)
    state.set("consumption_total_cumulative", 10300.0)
    t1 = t0 + timedelta(minutes=15)
    energy_broker._settle_prior_slot(result, batt_soc=50, today_actuals={}, now=t1)

    rec = json.loads((hist_dir / f"ess-{t1:%Y-%m-%d}.ndjson").read_text().splitlines()[-1])
    assert rec["ev_charge_kwh"] is None
    assert rec["base_load_kwh"] is None
    assert rec["ev_meter_quality"] == "missing_endpoint"


def test_settlement_rejects_impossible_ev_meter_jump(monkeypatch, tmp_path):
    monkeypatch.setattr(energy_broker, "_LAST_SLOT_PATH", str(tmp_path / "slot.json"))
    hist_dir = tmp_path / "history"
    hist_dir.mkdir()
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(hist_dir) if name == "HISTORY_DIR" else None)
    state = DummyState({"consumption_total_cumulative": 10000.0,
                        "tesla_charge_energy_forward": 100.0,
                        "c1_daily_yield": 0.0, "c2_daily_yield": 0.0})
    monkeypatch.setattr(energy_broker, "STATE", state)
    result = {"schedule": [{"time": datetime(2026, 7, 16, 12, 0), "grid_energy": 0,
                            "price": 0.2, "sell": 0.2, "pv": 0, "load": 0}],
              "slot_duration_h": 0.25, "control_action": "IDLE", "weather_context": {}}
    t0 = datetime(2026, 7, 16, 12, 0).astimezone()
    energy_broker._settle_prior_slot(result, batt_soc=50, today_actuals={}, now=t0)

    state.set("tesla_charge_energy_forward", 110.0)  # 10 kWh in 15 min is impossible at 16 kW.
    state.set("consumption_total_cumulative", 13000.0)
    t1 = t0 + timedelta(minutes=15)
    energy_broker._settle_prior_slot(result, batt_soc=50, today_actuals={}, now=t1)

    rec = json.loads((hist_dir / f"ess-{t1:%Y-%m-%d}.ndjson").read_text().splitlines()[-1])
    assert rec["ev_charge_kwh"] is None
    assert rec["base_load_kwh"] is None
    assert rec["ev_meter_quality"] == "implausible_delta"


def test_settlement_does_not_classify_implausible_total_load_jump(monkeypatch, tmp_path):
    monkeypatch.setattr(energy_broker, "_LAST_SLOT_PATH", str(tmp_path / "slot.json"))
    hist_dir = tmp_path / "history"
    hist_dir.mkdir()
    monkeypatch.setattr(energy_broker, "retrieve_setting",
                        lambda name: str(hist_dir) if name == "HISTORY_DIR" else None)
    state = DummyState({"consumption_total_cumulative": 0.0,
                        "tesla_charge_energy_forward": 100.0,
                        "c1_daily_yield": 0.0, "c2_daily_yield": 0.0})
    monkeypatch.setattr(energy_broker, "STATE", state)
    result = {"schedule": [{"time": datetime(2026, 7, 16, 12, 0), "grid_energy": 0,
                            "price": 0.2, "sell": 0.2, "pv": 0, "load": 0}],
              "slot_duration_h": 0.25, "control_action": "IDLE", "weather_context": {}}
    t0 = datetime(2026, 7, 16, 12, 0).astimezone()
    energy_broker._settle_prior_slot(result, batt_soc=50, today_actuals={}, now=t0)

    # Mirrors the historical July 11 VRM recovery artifact: a daily counter jumps
    # by 23.4 kWh while only one 15-minute interval elapsed.
    state.set("consumption_total_cumulative", 23400.0)
    t1 = t0 + timedelta(minutes=15)
    energy_broker._settle_prior_slot(result, batt_soc=50, today_actuals={}, now=t1)

    rec = json.loads((hist_dir / f"ess-{t1:%Y-%m-%d}.ndjson").read_text().splitlines()[-1])
    assert rec["actual_load_kwh"] == 23.4  # raw accounting stays additive/unchanged
    assert rec["base_load_kwh"] is None
    assert rec["load_meter_quality"] == "implausible_delta"
