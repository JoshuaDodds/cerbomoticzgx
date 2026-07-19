"""Mode-selector and Winter Mode configuration regression tests."""

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _selector_probe(raw_setting):
    """Import the selector in isolation and report optimizer imports."""
    script = r'''
import importlib
import json
import sys
import types

raw_setting = json.loads(sys.argv[1])
config = types.ModuleType("lib.config_retrieval")
config.retrieve_setting = lambda key: {
    "WINTER_MODE": raw_setting,
    "MIN_SOC_RESERVE_WINTER": "40",
    "MIN_SOC_RESERVE_SUMMER": "5",
}.get(key)
sys.modules["lib.config_retrieval"] = config

real_import_module = importlib.import_module
optimizer_imports = []

def tracked_import(name, package=None):
    if name in {"lib.ai_powered_ess", "lib.ai_powered_ess_winter"}:
        optimizer_imports.append(name)
        module = types.ModuleType(name)
        module.OptimizationEngine = type("FakeOptimizationEngine", (), {"source": name})
        module.format_plan_summary = lambda *args, **kwargs: name
        module.optimize_schedule = lambda *args, **kwargs: name
        module._coerce_datetime = lambda value: value
        return module
    return real_import_module(name, package)

importlib.import_module = tracked_import
import lib.ess_optimizer_selector as selector
from lib.helpers import current_min_soc_reserve
print(json.dumps({
    "imports": optimizer_imports,
    "mode": selector.OPTIMIZER_MODE,
    "module": selector.OPTIMIZER_MODULE_NAME,
    "call": selector.optimize_schedule(),
    "engine": selector.OptimizationEngine.source,
    "format": selector.format_plan_summary({}),
    "reserve": current_min_soc_reserve(),
}))
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(raw_setting)],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_selector_defaults_to_importing_only_summer_optimizer():
    result = _selector_probe(None)

    assert result == {
        "imports": ["lib.ai_powered_ess"],
        "mode": "summer",
        "module": "lib.ai_powered_ess",
        "call": "lib.ai_powered_ess",
        "engine": "lib.ai_powered_ess",
        "format": "lib.ai_powered_ess",
        "reserve": 5.0,
    }


def test_selector_imports_only_winter_optimizer_when_enabled():
    result = _selector_probe("True")

    assert result == {
        "imports": ["lib.ai_powered_ess_winter"],
        "mode": "winter",
        "module": "lib.ai_powered_ess_winter",
        "call": "lib.ai_powered_ess_winter",
        "engine": "lib.ai_powered_ess_winter",
        "format": "lib.ai_powered_ess_winter",
        "reserve": 40.0,
    }


def test_winter_mode_is_second_ai_ess_dashboard_setting():
    from frontend.config_schema import CONFIG_SCHEMA

    ai_group = next(group for group in CONFIG_SCHEMA if group["group"] == "AI ESS Optimizer")
    assert ai_group["settings"][1] == {
        "key": "WINTER_MODE",
        "label": "Winter Mode",
        "type": "bool",
        "desc": (
            "Use the restart-isolated winter optimizer and winter SoC reserve. "
            "Off keeps the summer optimizer and summer reserve."
        ),
    }


def test_winter_mode_change_requests_supervised_restart(monkeypatch):
    from lib import config_change_handler

    published = []
    monkeypatch.setattr(
        config_change_handler,
        "publish_message",
        lambda topic, message, retain: published.append((topic, message, retain)),
    )

    config_change_handler.handle_env_change("WINTER_MODE")

    assert published == [("Cerbomoticzgx/system/shutdown", "True", True)]


def test_new_winter_mode_key_triggers_watcher_handler(tmp_path):
    from lib.config_change_handler import ConfigWatcher

    env_file = tmp_path / ".env"
    env_file.write_text("AI_POWERED_ESS_ALGORITHM=True\n", encoding="utf-8")
    handled = []
    watcher = ConfigWatcher(env_file=str(env_file), handler=handled.append)

    env_file.write_text(
        "AI_POWERED_ESS_ALGORITHM=True\nWINTER_MODE=True\n",
        encoding="utf-8",
    )
    watcher.check_changes()

    assert handled == ["WINTER_MODE"]
    assert watcher._cache["WINTER_MODE"] == "True"


def test_reserve_does_not_change_before_restart_when_setting_changes():
    script = r'''
import json
import sys
import types

values = {
    "WINTER_MODE": "False",
    "MIN_SOC_RESERVE_WINTER": "40",
    "MIN_SOC_RESERVE_SUMMER": "5",
}
config = types.ModuleType("lib.config_retrieval")
config.retrieve_setting = values.get
sys.modules["lib.config_retrieval"] = config

from lib.helpers import current_min_soc_reserve
from lib.ess_mode import OPTIMIZER_MODE

before = current_min_soc_reserve()
values["WINTER_MODE"] = "True"
after = current_min_soc_reserve()
print(json.dumps({"mode": OPTIMIZER_MODE, "before": before, "after": after}))
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "mode": "summer",
        "before": 5.0,
        "after": 5.0,
    }


def test_appliance_season_helper_remains_calendar_based(monkeypatch):
    from lib import helpers

    class January:
        @classmethod
        def now(cls):
            return cls()

        month = 1

    class July:
        @classmethod
        def now(cls):
            return cls()

        month = 7

    monkeypatch.setattr(helpers, "datetime", January)
    assert helpers.is_winter_month() is True
    monkeypatch.setattr(helpers, "datetime", July)
    assert helpers.is_winter_month() is False
