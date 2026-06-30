import json
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
