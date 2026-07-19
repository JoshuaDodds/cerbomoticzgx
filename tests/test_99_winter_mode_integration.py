"""End-to-end integration checks for restart-isolated Winter Mode."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _broker_import_probe(tmp_path, enabled):
    """Import the complete broker path in a fresh process and report engines."""
    env_file = tmp_path / ("winter.env" if enabled else "summer.env")
    env_file.write_text(
        "\n".join((
            f"WINTER_MODE={'True' if enabled else 'False'}",
            "BATTERY_FLOAT_VOLTAGE=54.0",
            "BATTERY_ABSORPTION_VOLTAGE=56.0",
            "BATTERY_FULL_VOLTAGE=55.0",
        )) + "\n",
        encoding="utf-8",
    )
    script = r'''
import json
import sys

import lib.energy_broker as broker

print(json.dumps({
    "mode": broker.OPTIMIZER_MODE,
    "summer_loaded": "lib.ai_powered_ess" in sys.modules,
    "winter_loaded": "lib.ai_powered_ess_winter" in sys.modules,
}))
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["APP_ENV_PATH"] = str(env_file)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_full_broker_startup_imports_only_summer_engine(tmp_path):
    assert _broker_import_probe(tmp_path, False) == {
        "mode": "summer",
        "summer_loaded": True,
        "winter_loaded": False,
    }


def test_full_broker_startup_imports_only_winter_engine(tmp_path):
    assert _broker_import_probe(tmp_path, True) == {
        "mode": "winter",
        "summer_loaded": False,
        "winter_loaded": True,
    }


def test_frontend_plan_preserves_mode_and_winter_policy(monkeypatch, tmp_path):
    from frontend import data

    plan_file = tmp_path / "plan.json"
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    generated = datetime.now().astimezone().isoformat()
    policy = {
        "mode": "winter",
        "selected_candidate": "self_sufficiency",
        "protected_soc_percent": 46.0,
        "forecast_house_energy_required_kwh": 2.0,
    }
    plan_file.write_text(json.dumps({
        "generated_at": generated,
        "optimizer_mode": "winter",
        "winter_policy": policy,
        "schedule": [],
    }), encoding="utf-8")
    monkeypatch.setattr(data, "_env", lambda: {
        "AI_PLAN_EXPORT_PATH": str(plan_file),
        "HISTORY_DIR": str(history_dir),
    })

    plan = data.get_plan()

    assert plan["available"] is True
    assert plan["optimizer_mode"] == "winter"
    assert plan["winter_policy"] == policy


def test_runtime_and_tools_do_not_bypass_optimizer_selector():
    guarded_files = (
        ROOT / "lib" / "energy_broker.py",
        ROOT / "scripts" / "ai_ess_dryrun.py",
    )

    for path in guarded_files:
        source = path.read_text(encoding="utf-8")
        assert "from lib.ai_powered_ess import" not in source, path
        assert "import lib.ai_powered_ess" not in source, path
