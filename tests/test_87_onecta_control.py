import json
import stat
import time

import pytest

from lib import onecta_control as control


def _point():
    def cap(value, *, settable=False, values=None, **extra):
        result = {"value": value, "settable": settable, **extra}
        if values is not None:
            result["values"] = values
        return result

    return {
        "embeddedId": "climateControl",
        "managementPointType": "climateControl",
        "onOffMode": cap("off", settable=True, values=["off", "on"]),
        "operationMode": cap(
            "cooling",
            settable=True,
            values=["cooling", "heating", "fanOnly"],
        ),
        "powerfulMode": cap("off", settable=True, values=["off", "on"]),
        "temperatureControl": cap(
            {
                "operationModes": {
                    "cooling": {
                        "setpoints": {
                            "roomTemperature": cap(
                                25.0,
                                settable=True,
                                minValue=18.0,
                                maxValue=32.0,
                                stepValue=0.5,
                                unit="°C",
                            )
                        }
                    }
                }
            }
        ),
        "fanControl": cap(
            {
                "operationModes": {
                    "cooling": {
                        "fanSpeed": {
                            "currentMode": cap(
                                "auto",
                                settable=True,
                                values=["auto", "fixed", "quiet"],
                            ),
                            "modes": {
                                "fixed": cap(
                                    3,
                                    settable=True,
                                    minValue=1,
                                    maxValue=5,
                                    stepValue=1,
                                )
                            },
                        },
                        "fanDirection": {
                            "horizontal": {
                                "currentMode": cap(
                                    "stop",
                                    settable=True,
                                    values=["stop", "swing"],
                                )
                            },
                            "vertical": {
                                "currentMode": cap(
                                    "stop",
                                    settable=True,
                                    values=["stop", "swing"],
                                )
                            },
                        },
                    }
                }
            }
        ),
    }


def _device():
    return {"id": "raw-gateway-id", "managementPoints": [_point()]}


def _public_unit():
    return {
        "unit": control.unit_key(_device(), 1),
        "power": "off",
        "operation_mode": "cooling",
        "target_temperature_c": 25.0,
        "fan": {
            "mode": "auto",
            "level": 3,
            "horizontal": "stop",
            "vertical": "stop",
        },
        "powerful_mode": False,
        "controls": control.public_control_capabilities(_point()),
    }


class FakeMonitor:
    def __init__(self, unit=None, allowed=True):
        self.snapshot = {"units": [unit or _public_unit()], "rate_limits": {}}
        self.allowed = allowed
        self.refreshes = 0
        self.limits = []

    def rate_limit_allows_requests(self, count):
        return self.allowed

    def update_rate_limits(self, limits):
        self.limits.append(limits)

    def refresh(self):
        self.refreshes += 1
        return True


def _service(tmp_path, monitor, patch, enabled=True):
    registry_path = tmp_path / "registry.json"
    control.store_control_registry([_device()], registry_path)
    return control.OnectaControlService(
        monitor_getter=lambda: monitor,
        patch=patch,
        registry_path=registry_path,
        enabled=lambda: enabled,
        confirm_delay_seconds=0,
    )


def test_public_capabilities_expose_controls_without_raw_identifiers():
    capabilities = control.public_control_capabilities(_point())

    assert capabilities["operationMode"]["values"] == [
        "cooling",
        "heating",
        "fanOnly",
    ]
    assert capabilities["temperature"]["modes"]["cooling"]["min"] == 18.0
    assert capabilities["fan"]["modes"]["cooling"]["fixed"]["max"] == 5
    assert "raw-gateway-id" not in json.dumps(capabilities)
    assert "embeddedId" not in json.dumps(capabilities)


def test_private_registry_is_mode_0600_and_keeps_ids_out_of_public_unit(tmp_path):
    path = tmp_path / "registry.json"

    registry = control.store_control_registry([_device()], path)

    key = next(iter(registry["units"]))
    assert registry["units"][key]["gateway_id"] == "raw-gateway-id"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "raw-gateway-id" not in json.dumps(_public_unit())


def test_control_disabled_leaves_read_only_state_available(tmp_path):
    service = _service(tmp_path, FakeMonitor(), lambda *a, **k: {}, enabled=False)

    with pytest.raises(control.OnectaControlDisabled):
        service.command(_public_unit()["unit"], "power", "on")


def test_idempotent_command_does_not_spend_an_api_request(tmp_path):
    monitor = FakeMonitor()
    calls = []
    service = _service(
        tmp_path,
        monitor,
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )

    result = service.command(_public_unit()["unit"], "power", "off")

    assert result["status"] == "already_set"
    assert calls == []
    assert monitor.refreshes == 0


def test_temperature_is_validated_against_advertised_range_and_step(tmp_path):
    service = _service(tmp_path, FakeMonitor(), lambda *a, **k: {})

    with pytest.raises(ValueError, match="between 18 and 32"):
        service.command(_public_unit()["unit"], "temperature", 40)
    with pytest.raises(ValueError, match="0.5 increments"):
        service.command(_public_unit()["unit"], "temperature", 25.2)


def test_command_uses_official_patch_path_and_one_delayed_confirmation(tmp_path):
    unit = _public_unit()
    monitor = FakeMonitor(unit)
    calls = []

    def patch(*args, **kwargs):
        calls.append((args, kwargs))
        monitor.snapshot["units"][0]["target_temperature_c"] = 26.0
        return {"remaining_day": 150}

    service = _service(tmp_path, monitor, patch)
    result = service.command(unit["unit"], "temperature", 26)
    deadline = time.time() + 1
    while service.statuses()[unit["unit"]]["status"] == "accepted" and time.time() < deadline:
        time.sleep(0.01)

    assert result["status"] == "accepted"
    assert calls == [
        (
            ("raw-gateway-id", "climateControl", "temperatureControl", 26.0),
            {"path": "/operationModes/cooling/setpoints/roomTemperature"},
        )
    ]
    assert monitor.refreshes == 1
    assert monitor.limits == [{"remaining_day": 150}]
    assert service.statuses()[unit["unit"]]["status"] == "confirmed"


def test_daily_request_reserve_blocks_before_patch(tmp_path):
    calls = []
    service = _service(
        tmp_path,
        FakeMonitor(allowed=False),
        lambda *a, **k: calls.append(1),
    )

    with pytest.raises(RuntimeError, match="reserve"):
        service.command(_public_unit()["unit"], "power", "on")

    assert calls == []


def test_missing_private_registry_is_hydrated_once_before_command(tmp_path):
    unit = _public_unit()
    monitor = FakeMonitor(unit)
    registry_path = tmp_path / "registry.json"
    calls = []

    def refresh():
        monitor.refreshes += 1
        control.store_control_registry([_device()], registry_path)
        return True

    monitor.refresh = refresh
    service = control.OnectaControlService(
        monitor_getter=lambda: monitor,
        patch=lambda *args, **kwargs: calls.append((args, kwargs)) or {},
        registry_path=registry_path,
        enabled=lambda: True,
        confirm_delay_seconds=0,
    )

    result = service.command(unit["unit"], "power", "on")

    assert result["status"] == "accepted"
    assert monitor.refreshes >= 1
    assert calls[0][0] == (
        "raw-gateway-id",
        "climateControl",
        "onOffMode",
        "on",
    )


def test_confirmation_allows_one_stale_read_before_cloud_converges(tmp_path):
    unit = _public_unit()
    monitor = FakeMonitor(unit)
    refreshes = []

    def refresh():
        refreshes.append("read")
        if len(refreshes) == 2:
            monitor.snapshot["units"][0]["power"] = "on"
        return True

    monitor.refresh = refresh
    registry_path = tmp_path / "registry.json"
    control.store_control_registry([_device()], registry_path)
    service = control.OnectaControlService(
        monitor_getter=lambda: monitor,
        patch=lambda *args, **kwargs: {},
        registry_path=registry_path,
        enabled=lambda: True,
        confirm_delays_seconds=(0, 0),
    )

    service.command(unit["unit"], "power", "on")
    deadline = time.time() + 1
    while service.statuses()[unit["unit"]]["status"] not in {
        "confirmed",
        "accepted_unconfirmed",
    } and time.time() < deadline:
        time.sleep(0.01)

    assert refreshes == ["read", "read"]
    assert service.statuses()[unit["unit"]]["status"] == "confirmed"


def test_later_dashboard_snapshot_reconciles_unconfirmed_command(tmp_path):
    unit = _public_unit()
    monitor = FakeMonitor(unit)
    service = _service(tmp_path, monitor, lambda *a, **k: {})
    service._status[unit["unit"]] = {
        "status": "accepted_unconfirmed",
        "command": "power",
        "desired": "on",
    }
    monitor.snapshot["units"][0]["power"] = "on"

    service.reconcile(monitor.snapshot)

    assert service.statuses()[unit["unit"]]["status"] == "confirmed"
