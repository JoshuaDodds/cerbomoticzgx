import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta

from frontend import data
from frontend.config_schema import CONFIG_SCHEMA


def _schema_keys():
    return {
        setting["key"]
        for group in CONFIG_SCHEMA
        for setting in group["settings"]
    }


def test_app_env_path_override_drives_config_reads_and_writes(monkeypatch, tmp_path):
    env_path = tmp_path / "runtime.env"
    env_path.write_text("AI_POWERED_ESS_ALGORITHM=True\nFRONTEND_PORT=9090\n")

    monkeypatch.setenv("APP_ENV_PATH", str(env_path))

    assert data._env()["FRONTEND_PORT"] == "9090"
    saved = data.update_env_setting("AI_POWERED_ESS_ALGORITHM", "false")

    assert saved == {"key": "AI_POWERED_ESS_ALGORITHM", "value": "False"}
    assert "AI_POWERED_ESS_ALGORITHM=False\n" in env_path.read_text()


def test_config_schema_exposes_grid_charge_cap_and_advisor_safe_knobs():
    keys = _schema_keys()

    assert "ESS_MAX_GRID_CHARGE_SOC" in keys
    assert "ADVISOR_AUTH" in keys
    assert "ADVISOR_MODEL" in keys
    assert "ADVISOR_HISTORY_DAYS" in keys
    assert "ADVISOR_MAX_THINKING_TOKENS" in keys
    assert "ADVISOR_MAX_INPUT_CHARS" in keys
    assert "ADVISOR_RETRIEVAL_MAX_DAYS" in keys
    assert "ADVISOR_RETRIEVAL_MAX_CHARS" in keys


def test_numeric_config_schema_entries_have_bounds():
    for group in CONFIG_SCHEMA:
        for setting in group["settings"]:
            if setting.get("type") in ("int", "float"):
                assert "min" in setting, setting["key"]
                assert "max" in setting, setting["key"]


def test_numeric_config_writes_reject_values_outside_schema_bounds(tmp_path):
    env_path = tmp_path / "runtime.env"
    env_path.write_text("ESS_MAX_DISCHARGE_KW=12\nMIN_SOC_RESERVE_SUMMER=0\n")

    with pytest.raises(ValueError, match="between 0 and 50"):
        data.update_env_setting("ESS_MAX_DISCHARGE_KW", "99999", env_path=str(env_path))
    with pytest.raises(ValueError, match="between 0 and 100"):
        data.update_env_setting("MIN_SOC_RESERVE_SUMMER", "-50", env_path=str(env_path))

    text = env_path.read_text()
    assert "ESS_MAX_DISCHARGE_KW=12\n" in text
    assert "MIN_SOC_RESERVE_SUMMER=0\n" in text


def test_weather_behavior_toggles_are_first_in_weather_config_group():
    weather = next(group for group in CONFIG_SCHEMA if group["group"] == "Weather Forecast")
    keys = [setting["key"] for setting in weather["settings"]]

    assert keys[:3] == ["WEATHER_ENABLED", "HVAC_LOAD_APPLY", "PV_WEATHER_APPLY"]
    assert "ADVISOR_CLI_CMD" not in keys
    assert "CLAUDE_CONFIG_DIR" not in keys


def test_removed_grid_charge_price_caps_are_not_user_tunable():
    removed = {
        "ESS_MAX_GRID_CHARGE_PRICE",
        "ESS_GRID_CHARGE_CHEAP_PCT",
    }
    keys = _schema_keys()

    assert not (removed & keys)
    for path in (".env", ".env.example"):
        text = Path(path).read_text()
        for key in removed:
            assert key not in text


def test_settled_slots_for_today_are_schedule_shaped(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "_env", lambda: {"HISTORY_DIR": str(tmp_path)})
    now = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    path = tmp_path / f"ess-{now.date().isoformat()}.ndjson"
    rec = {
        "kind": "settlement",
        "slot_start": now.isoformat(),
        "slot_end": (now + timedelta(minutes=15)).isoformat(),
        "predicted_control_action": "IDLE",
        "actual_import_kwh": 0.1,
        "actual_export_kwh": 0.3,
        "actual_cost": 0.02,
        "actual_reward": 0.09,
        "actual_net_eur": 0.07,
        "actual_pv_kwh": 0.4,
        "soc_start": 10.0,
        "soc_end": 11.0,
        "price_buy": 0.2,
        "price_sell": 0.3,
    }
    path.write_text(json.dumps(rec) + "\n")

    slots = data.settled_slots_for_today((now + timedelta(hours=1)).isoformat())

    assert len(slots) == 1
    slot = slots[0]
    assert slot["settled"] is True
    assert slot["grid_energy"] == -0.19999999999999998
    assert slot["pv"] == 0.4
    assert slot["actual_net_eur"] == 0.07


def test_group_by_hour_aggregates_settled_actuals(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "_env", lambda: {"HISTORY_DIR": str(tmp_path)})
    now = datetime.now().astimezone().replace(hour=8, minute=0, second=0, microsecond=0)
    slot = {
        "time": now.isoformat(),
        "settled": True,
        "control_action": "IDLE",
        "grid_energy": -0.2,
        "price": 0.2,
        "sell": 0.3,
        "pv": 0.4,
        "load": None,
        "soc_start": 10.0,
        "soc_end": 11.0,
        "actual_import_kwh": 0.1,
        "actual_export_kwh": 0.3,
        "actual_cost": 0.02,
        "actual_reward": 0.09,
    }

    hours = data.group_by_hour([slot])

    assert len(hours) == 1
    hour = hours[0]
    assert hour["import_kwh"] == 0.1
    assert hour["export_kwh"] == 0.3
    assert hour["production_kwh"] == 0.4
    assert hour["net_cost"] == -0.07
    assert hour["is_current"] is False
    assert hour["hour_start"] == now.isoformat()


def test_forecast_accuracy_uses_settlement_predicted_and_actuals(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "_env", lambda: {"HISTORY_DIR": str(tmp_path)})
    day = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    path = tmp_path / f"ess-{day.date().isoformat()}.ndjson"
    records = [
        {
            "kind": "settlement",
            "slot_start": day.isoformat(),
            "slot_end": (day + timedelta(minutes=15)).isoformat(),
            "predicted_pv_kwh": 0.50,
            "actual_pv_kwh": 0.40,
            "predicted_load_kwh": 0.30,
            "actual_load_kwh": 0.35,
        },
        {
            "kind": "settlement",
            "slot_start": (day + timedelta(minutes=15)).isoformat(),
            "slot_end": (day + timedelta(minutes=30)).isoformat(),
            "predicted_pv_kwh": 0.60,
            "actual_pv_kwh": 0.75,
            "predicted_load_kwh": 0.25,
            "actual_load_kwh": 0.20,
        },
        {
            "kind": "settlement",
            "slot_start": (day + timedelta(minutes=30)).isoformat(),
            "incomplete": True,
            "predicted_pv_kwh": 99,
            "actual_pv_kwh": 99,
            "predicted_load_kwh": 99,
            "actual_load_kwh": 99,
        },
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))

    out = data.forecast_accuracy(days=7)

    assert out["available"] is True
    assert out["summary"]["slots"] == 2
    assert out["summary"]["load_mae_kwh"] == 0.05
    assert out["summary"]["pv_mae_kwh"] == 0.125
    assert out["slots"][0]["predicted_load_kwh"] == 0.3
    assert out["slots"][1]["actual_pv_kwh"] == 0.75


def test_monthly_history_adds_projected_today_profit_from_current_plan(monkeypatch, tmp_path):
    today = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    plan_path = tmp_path / "plan.json"
    hist_path = history_dir / f"ess-{today.date().isoformat()}.ndjson"
    hist_path.write_text(json.dumps({
        "kind": "cycle",
        "ts": today.isoformat(),
        "day_import_cost": 2.8,
        "day_export_reward": 0.0,
        "day_import_kwh": 8.0,
        "day_export_kwh": 0.0,
    }) + "\n")
    plan_path.write_text(json.dumps({
        "generated_at": today.isoformat(),
        "today_actuals": {
            "imp_kwh": 8.0,
            "imp_cost": 2.8,
            "exp_kwh": 0.0,
            "exp_rev": 0.0,
        },
        "schedule": [{
            "time": (today + timedelta(hours=6)).isoformat(),
            "control_action": "SELL",
            "grid_energy": -30.0,
            "price": 0.30,
            "sell": 0.30,
        }],
    }))
    monkeypatch.setattr(data, "_env", lambda: {
        "HISTORY_DIR": str(history_dir),
        "AI_PLAN_EXPORT_PATH": str(plan_path),
    })

    days = data.monthly_history()

    today_row = next(d for d in days if d["is_today"])
    assert today_row["net_eur"] == -2.8
    assert today_row["projected_net_eur"] == 6.2


def test_day_summary_idle_surplus_charges_battery_not_grid():
    # A future IDLE slot with PV surplus but a non-full battery charges the battery
    # (SoC up / cost basis down); it must NOT book phantom grid-export profit.
    now = datetime.now().astimezone().replace(hour=13, minute=0, second=0, microsecond=0)
    schedule = [{
        "time": now.isoformat(),
        "control_action": "IDLE",
        "grid_energy": -1.0,          # DP projects export, but battery isn't full
        "price": 0.25,
        "sell": 0.25,
        "soc_start": 40.0,
        "soc_end": 42.0,              # not full → surplus stored, no grid revenue
    }]

    summary = data.day_summary(schedule, None)

    today = next(d for d in summary["days"] if d["is_today"])
    assert today["forecast"]["export_kwh"] == 0.0
    assert today["forecast"]["export_rev"] == 0.0
    assert today["net"] == 0.0
    assert "projected_idle_net" not in today


def test_day_summary_idle_surplus_exports_when_battery_full():
    # Once the battery is full, IDLE PV surplus genuinely feeds the grid and books
    # the export revenue.
    now = datetime.now().astimezone().replace(hour=14, minute=0, second=0, microsecond=0)
    schedule = [{
        "time": now.isoformat(),
        "control_action": "IDLE",
        "grid_energy": -1.0,
        "price": 0.25,
        "sell": 0.25,
        "soc_start": 100.0,
        "soc_end": 100.0,            # full → real feed-in
    }]

    summary = data.day_summary(schedule, None)

    today = next(d for d in summary["days"] if d["is_today"])
    assert today["forecast"]["export_kwh"] == 1.0
    assert today["forecast"]["export_rev"] == 0.25
    assert today["net"] == -0.25      # −cost == €0.25 real projected profit


def test_group_by_hour_idle_surplus_charges_battery_not_grid(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "_env", lambda: {"HISTORY_DIR": str(tmp_path)})
    now = datetime.now().astimezone().replace(hour=14, minute=0, second=0, microsecond=0)
    slot = {
        "time": now.isoformat(),
        "control_action": "IDLE",     # forward (not settled)
        "grid_energy": -1.0,
        "price": 0.25,
        "sell": 0.25,
        "pv": 1.5,
        "load": 0.5,
        "soc_start": 40.0,
        "soc_end": 42.0,              # not full → surplus stored, no grid revenue
    }

    hours = data.group_by_hour([slot])

    hour = hours[0]
    assert hour["export_kwh"] == 0.0
    assert hour["net_cost"] == 0.0
    assert hour["grid_kwh"] == -1.0   # raw projected flow still shown
    assert "projected_idle_net" not in hour


def test_previous_day_summary_uses_cumulative_counters_not_settlement_sum(monkeypatch, tmp_path):
    # Regression (2026-07-10): the Timeline previous-day total must match the Trends
    # monthly chart. Both now read the authoritative cumulative day counters, so a
    # dropped per-slot settlement can't make the two views disagree.
    monkeypatch.setattr(data, "_env", lambda: {"HISTORY_DIR": str(tmp_path)})
    day = datetime.now().date() - timedelta(days=1)
    base = datetime.now().astimezone().replace(
        year=day.year, month=day.month, day=day.day, hour=10, minute=0, second=0, microsecond=0)
    recs = [
        {"kind": "cycle", "ts": base.isoformat(), "day_import_cost": 2.0, "day_export_reward": 1.0},
        {"kind": "settlement", "slot_start": base.isoformat(),
         "slot_end": (base + timedelta(minutes=15)).isoformat(),
         "actual_cost": 0.5, "actual_reward": 1.0, "actual_import_kwh": 1.0, "actual_export_kwh": 3.0},
        # Cumulative counters jump (a mid-slot re-optimize the per-slot settlement missed):
        {"kind": "cycle", "ts": (base + timedelta(minutes=15)).isoformat(),
         "day_import_cost": 5.0, "day_export_reward": 2.0},
        {"kind": "settlement", "slot_start": (base + timedelta(minutes=15)).isoformat(),
         "slot_end": (base + timedelta(minutes=30)).isoformat(),
         "actual_cost": 0.5, "actual_reward": 0.0, "actual_import_kwh": 1.0, "actual_export_kwh": 0.0},
    ]
    (tmp_path / f"ess-{day.isoformat()}.ndjson").write_text(
        "".join(json.dumps(r) + "\n" for r in recs))

    out = data.previous_day_schedule(days_back=1)
    # Per-slot actual_cost sums to 1.0, but the cumulative counters say 5.0 import / 2.0 export.
    assert out["summary"]["import_cost"] == 5.0
    assert out["summary"]["export_rev"] == 2.0
    assert out["summary"]["net"] == 3.0        # 5.0 - 2.0, NOT the 0.0 the per-slot sum would give
    assert out["available"] is True


def test_day_totals_uses_last_reading_not_max_across_midnight_rollover(tmp_path):
    # The first cycle written just after midnight still holds YESTERDAY's cumulative
    # counters (Tibber resets a moment later). _day_totals must take the FINAL reading,
    # not the max — otherwise a profitable day (real end: import 5.65 / reward 8.12 =
    # +2.47) is reported as a loss because max() picks yesterday's stale import 9.10.
    path = tmp_path / "ess-2026-07-03.ndjson"
    rows = [
        {"kind": "cycle", "day_import_cost": 9.10, "day_export_reward": 7.17,
         "day_import_kwh": 30.0, "day_export_kwh": 25.0},   # stale: yesterday's totals
        {"kind": "cycle", "day_import_cost": 0.07, "day_export_reward": 0.0,
         "day_import_kwh": 0.2, "day_export_kwh": 0.0},      # after the midnight reset
        {"kind": "cycle", "day_import_cost": 5.65, "day_export_reward": 8.12,
         "day_import_kwh": 18.0, "day_export_kwh": 28.0},    # end of day
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    t = data._day_totals(str(path))

    assert t["import_cost"] == 5.65      # not 9.10 (the stale max)
    assert t["export_reward"] == 8.12
    assert t["import_kwh"] == 18.0
    assert t["export_kwh"] == 28.0
    assert round(t["export_reward"] - t["import_cost"], 2) == 2.47   # profit, not −0.99


def test_day_totals_ignores_trailing_null_counter(tmp_path):
    # A malformed/partial LAST record (null counter) must fall back to the prior valid
    # reading, not drop the whole day.
    path = tmp_path / "ess-2026-07-03.ndjson"
    rows = [
        {"kind": "cycle", "day_import_cost": 1.00, "day_export_reward": 2.00},
        {"kind": "cycle", "day_import_cost": 3.00, "day_export_reward": 4.00},
        {"kind": "cycle", "day_import_cost": None, "day_export_reward": None},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    t = data._day_totals(str(path))

    assert t["import_cost"] == 3.00
    assert t["export_reward"] == 4.00
