import json
from datetime import datetime, timedelta

from frontend import data


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
