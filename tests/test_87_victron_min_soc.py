"""Regression tests for optimizer reserve vs Victron hard minimum SoC."""

import ast
import logging
from pathlib import Path
from unittest.mock import MagicMock

from lib import victron_integration


class DummyState:
    """Mirror GlobalStateClient's important missing-key behavior."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key, 0)

    def has(self, key):
        return key in self.values

    def set(self, key, value):
        self.values[key] = value


def _patch_publish(monkeypatch):
    publish = MagicMock()
    monkeypatch.setattr(victron_integration.publish, "single", publish)
    return publish


def test_hardware_minimum_is_independent_from_winter_optimizer_reserve(monkeypatch):
    values = {
        "VICTRON_HARDWARE_MIN_SOC": "0",
        "MIN_SOC_RESERVE_WINTER": "40",
        "MIN_SOC_RESERVE_SUMMER": "0",
    }
    monkeypatch.setattr(victron_integration, "retrieve_setting", values.get)

    assert victron_integration.configured_victron_min_soc_limit() == 0


def test_summer_zero_is_written_when_actual_victron_value_is_still_forty(monkeypatch):
    state = DummyState({
        "minimum_ess_soc": 40,
        # The old shadow can incorrectly say zero after startup restoration.
        "min_ess_soc_applied": 0,
    })
    monkeypatch.setattr(victron_integration, "STATE", state)
    publish = _patch_publish(monkeypatch)

    changed = victron_integration.set_minimum_ess_soc(percent=0)

    assert changed is True
    publish.assert_called_once()
    assert publish.call_args.kwargs["payload"] == '{"value": 0}'
    assert state.get("min_ess_soc_applied") == 0


def test_missing_observed_topic_never_masquerades_as_applied_zero(monkeypatch):
    state = DummyState({"min_ess_soc_applied": 0})
    monkeypatch.setattr(victron_integration, "STATE", state)
    publish = _patch_publish(monkeypatch)

    changed = victron_integration.set_minimum_ess_soc(percent=0)

    assert changed is True
    publish.assert_called_once()


def test_observed_matching_victron_value_avoids_redundant_write(monkeypatch):
    state = DummyState({"minimum_ess_soc": 5})
    monkeypatch.setattr(victron_integration, "STATE", state)
    publish = _patch_publish(monkeypatch)

    changed = victron_integration.set_minimum_ess_soc(percent=5)

    assert changed is False
    publish.assert_not_called()
    assert state.get("min_ess_soc_applied") == 5


def test_force_reasserts_hardware_minimum_on_service_start(monkeypatch):
    state = DummyState({"minimum_ess_soc": 0})
    monkeypatch.setattr(victron_integration, "STATE", state)
    publish = _patch_publish(monkeypatch)

    changed = victron_integration.set_minimum_ess_soc(percent=0, force=True)

    assert changed is True
    publish.assert_called_once()


def test_main_force_reconciles_hardware_minimum_in_unconditional_init_path():
    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    )
    init_node = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "init"
    )
    calls = [
        node
        for node in ast.walk(init_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "set_minimum_ess_soc"
    ]

    assert len(calls) == 1
    assert any(
        keyword.arg == "force"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in calls[0].keywords
    )


def test_missing_hardware_minimum_defaults_to_zero_for_existing_installs(monkeypatch):
    monkeypatch.setattr(victron_integration, "retrieve_setting", lambda _key: None)

    assert victron_integration.configured_victron_min_soc_limit() == 0


def test_explicit_invalid_hardware_minimum_is_rejected_not_clamped(monkeypatch):
    values = iter(("invalid", "-10", "140", "nan"))
    monkeypatch.setattr(victron_integration, "retrieve_setting", lambda _key: next(values))

    assert victron_integration.configured_victron_min_soc_limit() is None
    assert victron_integration.configured_victron_min_soc_limit() is None
    assert victron_integration.configured_victron_min_soc_limit() is None
    assert victron_integration.configured_victron_min_soc_limit() is None


def test_invalid_hardware_minimum_does_not_publish(monkeypatch):
    monkeypatch.setattr(victron_integration, "retrieve_setting", lambda _key: "140")
    publish = _patch_publish(monkeypatch)

    changed = victron_integration.set_minimum_ess_soc()

    assert changed is False
    publish.assert_not_called()


def test_hardware_minimum_config_change_is_force_applied(monkeypatch):
    from lib import config_change_handler

    applied = MagicMock()
    monkeypatch.setattr(victron_integration, "set_minimum_ess_soc", applied)

    config_change_handler.handle_env_change("VICTRON_HARDWARE_MIN_SOC")

    applied.assert_called_once_with(force=True)


def test_hardware_minimum_config_write_failure_does_not_escape_watcher(
    monkeypatch, caplog
):
    from lib import config_change_handler

    monkeypatch.setattr(
        victron_integration,
        "set_minimum_ess_soc",
        MagicMock(side_effect=OSError("broker unavailable")),
    )

    with caplog.at_level(logging.ERROR):
        config_change_handler.handle_env_change("VICTRON_HARDWARE_MIN_SOC")

    assert "startup/optimizer reconciliation will retry" in caplog.text


def test_new_hardware_minimum_key_runs_watcher_handler(tmp_path):
    from lib.config_change_handler import ConfigWatcher

    env_file = tmp_path / ".env"
    env_file.write_text("AI_POWERED_ESS_ALGORITHM=True\n", encoding="utf-8")
    handled = []
    watcher = ConfigWatcher(env_file=str(env_file), handler=handled.append)

    env_file.write_text(
        "AI_POWERED_ESS_ALGORITHM=True\nVICTRON_HARDWARE_MIN_SOC=0\n",
        encoding="utf-8",
    )
    watcher.check_changes()

    assert handled == ["VICTRON_HARDWARE_MIN_SOC"]


def test_hardware_minimum_is_dashboard_bounded_and_distinct_from_reserves():
    from frontend.config_schema import CONFIG_SCHEMA

    battery_group = next(
        group for group in CONFIG_SCHEMA if group["group"] == "Battery & Power Limits"
    )
    settings = {setting["key"]: setting for setting in battery_group["settings"]}

    assert settings["VICTRON_HARDWARE_MIN_SOC"]["min"] == 0
    assert settings["VICTRON_HARDWARE_MIN_SOC"]["max"] == 100
    assert "independent" in settings["VICTRON_HARDWARE_MIN_SOC"]["desc"].lower()
