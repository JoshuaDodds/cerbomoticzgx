"""Capability-driven manual control for the optional Daikin ONECTA module.

The public dashboard and MQTT payloads use anonymous unit keys. Raw Daikin
gateway identifiers are kept only in a mode-0600 local registry and are never
returned to the browser or written to logs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.helpers import is_truthy
from lib.onecta_api import OnectaError, patch_characteristic


DEFAULT_REGISTRY_PATH = Path("data/hvac/control_registry.json")
DEFAULT_CONFIRM_DELAYS_SECONDS = (10.0, 20.0)
CONTROL_COMMANDS = {
    "power",
    "mode",
    "temperature",
    "fan_mode",
    "fan_level",
    "horizontal_swing",
    "vertical_swing",
    "powerful",
}


class OnectaControlDisabled(RuntimeError):
    """Raised when a write is attempted while the separate gate is disabled."""


class OnectaControlBusy(RuntimeError):
    """Raised when the same unit already has an in-flight command."""


def unit_key(device: Mapping[str, Any], index: int) -> str:
    raw = str(device.get("id") or device.get("_id") or index)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"unit-{digest}"


def _value(capability: Any) -> Any:
    if isinstance(capability, Mapping) and "value" in capability:
        return capability.get("value")
    return capability


def _climate_point(device: Mapping[str, Any]) -> Mapping[str, Any] | None:
    points = device.get("managementPoints")
    if not isinstance(points, list):
        return None
    for point in points:
        if (
            isinstance(point, Mapping)
            and point.get("managementPointType") == "climateControl"
        ):
            return point
    return None


def _simple_control(point: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    capability = point.get(name)
    if not isinstance(capability, Mapping) or capability.get("settable") is not True:
        return None
    result = {"settable": True, "value": capability.get("value")}
    values = capability.get("values")
    if isinstance(values, list):
        result["values"] = list(values)
    return result


def public_control_capabilities(point: Mapping[str, Any]) -> dict[str, Any]:
    """Return only writable, identifier-free controls advertised by one unit."""
    controls: dict[str, Any] = {}
    for name in ("onOffMode", "operationMode", "powerfulMode"):
        control = _simple_control(point, name)
        if control is not None:
            controls[name] = control

    temperature = _value(point.get("temperatureControl"))
    temperature_modes: dict[str, Any] = {}
    if isinstance(temperature, Mapping):
        modes = temperature.get("operationModes")
        if isinstance(modes, Mapping):
            for mode, mode_data in modes.items():
                if not isinstance(mode_data, Mapping):
                    continue
                setpoints = mode_data.get("setpoints")
                room = (
                    setpoints.get("roomTemperature")
                    if isinstance(setpoints, Mapping)
                    else None
                )
                if not isinstance(room, Mapping) or room.get("settable") is not True:
                    continue
                temperature_modes[str(mode)] = {
                    "value": room.get("value"),
                    "min": room.get("minValue"),
                    "max": room.get("maxValue"),
                    "step": room.get("stepValue"),
                    "unit": room.get("unit") or "°C",
                }
    if temperature_modes:
        controls["temperature"] = {"modes": temperature_modes}

    fan = _value(point.get("fanControl"))
    fan_modes: dict[str, Any] = {}
    if isinstance(fan, Mapping):
        modes = fan.get("operationModes")
        if isinstance(modes, Mapping):
            for mode, mode_data in modes.items():
                if not isinstance(mode_data, Mapping):
                    continue
                public_mode: dict[str, Any] = {}
                speed = mode_data.get("fanSpeed")
                if isinstance(speed, Mapping):
                    current = speed.get("currentMode")
                    if isinstance(current, Mapping) and current.get("settable") is True:
                        public_mode["speed_modes"] = list(current.get("values") or [])
                    fixed = (
                        (speed.get("modes") or {}).get("fixed")
                        if isinstance(speed.get("modes"), Mapping)
                        else None
                    )
                    if isinstance(fixed, Mapping) and fixed.get("settable") is True:
                        public_mode["fixed"] = {
                            "value": fixed.get("value"),
                            "min": fixed.get("minValue"),
                            "max": fixed.get("maxValue"),
                            "step": fixed.get("stepValue"),
                        }
                directions = mode_data.get("fanDirection")
                if isinstance(directions, Mapping):
                    for direction in ("horizontal", "vertical"):
                        direction_data = directions.get(direction)
                        current = (
                            direction_data.get("currentMode")
                            if isinstance(direction_data, Mapping)
                            else None
                        )
                        if (
                            isinstance(current, Mapping)
                            and current.get("settable") is True
                        ):
                            public_mode[f"{direction}_modes"] = list(
                                current.get("values") or []
                            )
                if public_mode:
                    fan_modes[str(mode)] = public_mode
    if fan_modes:
        controls["fan"] = {"modes": fan_modes}
    return controls


def build_control_registry(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the private unit-id registry plus public capability descriptors."""
    units: dict[str, Any] = {}
    for index, device in enumerate(devices, start=1):
        point = _climate_point(device)
        gateway_id = device.get("id") or device.get("_id")
        embedded_id = point.get("embeddedId") if point else None
        if not gateway_id or not embedded_id or point is None:
            continue
        key = unit_key(device, index)
        units[key] = {
            "gateway_id": str(gateway_id),
            "embedded_id": str(embedded_id),
            "controls": public_control_capabilities(point),
        }
    return {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "units": units,
    }


def _atomic_registry_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def store_control_registry(
    devices: list[dict[str, Any]],
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> dict[str, Any]:
    registry = build_control_registry(devices)
    _atomic_registry_write(Path(path), registry)
    return registry


def load_control_registry(
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "units": {}}
    return payload if isinstance(payload, dict) else {"schema_version": 1, "units": {}}


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be numeric")
    if not math.isfinite(result):
        raise ValueError(f"{label} must be numeric")
    return result


def _range_value(value: Any, spec: Mapping[str, Any], label: str) -> float:
    number = _number(value, label)
    minimum = _number(spec.get("min"), f"{label} minimum")
    maximum = _number(spec.get("max"), f"{label} maximum")
    step = _number(spec.get("step") or 1, f"{label} step")
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    steps = (number - minimum) / step
    if abs(steps - round(steps)) > 1e-6:
        raise ValueError(f"{label} must use {step:g} increments")
    return number


def _numeric_equal(value: Any, expected: float) -> bool:
    try:
        return abs(float(value) - float(expected)) < 0.01
    except (TypeError, ValueError):
        return False


def _unit_snapshot(snapshot: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    for unit in (snapshot or {}).get("units") or []:
        if unit.get("unit") == key:
            return unit
    raise ValueError("HVAC unit is unavailable in the current snapshot")


def _command_patches(
    unit: Mapping[str, Any],
    command: str,
    requested: Any,
) -> tuple[list[tuple[str, Any, str | None]], Any, Callable[[Mapping[str, Any]], bool]]:
    controls = unit.get("controls") or {}
    mode = str(unit.get("operation_mode") or "")

    if command == "power":
        spec = controls.get("onOffMode") or {}
        value = str(requested).lower()
        allowed = spec.get("values") or ["on", "off"]
        if not spec.get("settable") or value not in allowed:
            raise ValueError("Requested power state is not supported")
        return [("onOffMode", value, None)], value, (
            lambda fresh: fresh.get("power") == value
        )

    if command == "mode":
        spec = controls.get("operationMode") or {}
        value = str(requested)
        if not spec.get("settable") or value not in (spec.get("values") or []):
            raise ValueError("Requested operation mode is not supported")
        return [("operationMode", value, None)], value, (
            lambda fresh: fresh.get("operation_mode") == value
        )

    if command == "temperature":
        spec = ((controls.get("temperature") or {}).get("modes") or {}).get(mode)
        if not isinstance(spec, Mapping):
            raise ValueError("Target temperature is unavailable in the current mode")
        value = _range_value(requested, spec, "temperature")
        path = f"/operationModes/{mode}/setpoints/roomTemperature"
        return [("temperatureControl", value, path)], value, (
            lambda fresh: (
                fresh.get("target_temperature_c") is not None
                and abs(float(fresh["target_temperature_c"]) - value) < 0.01
            )
        )

    fan_spec = ((controls.get("fan") or {}).get("modes") or {}).get(mode)
    if command in {
        "fan_mode",
        "fan_level",
        "horizontal_swing",
        "vertical_swing",
    } and not isinstance(fan_spec, Mapping):
        raise ValueError("Fan control is unavailable in the current mode")

    if command == "fan_mode":
        value = str(requested)
        if value not in (fan_spec.get("speed_modes") or []):
            raise ValueError("Requested fan mode is not supported")
        path = f"/operationModes/{mode}/fanSpeed/currentMode"
        return [("fanControl", value, path)], value, (
            lambda fresh: (fresh.get("fan") or {}).get("mode") == value
        )

    if command == "fan_level":
        fixed = fan_spec.get("fixed")
        if not isinstance(fixed, Mapping):
            raise ValueError("Fixed fan levels are unavailable in the current mode")
        value_number = _range_value(requested, fixed, "fan level")
        value = int(value_number)
        patches: list[tuple[str, Any, str | None]] = []
        if (unit.get("fan") or {}).get("mode") != "fixed":
            if "fixed" not in (fan_spec.get("speed_modes") or []):
                raise ValueError("Fixed fan mode is unavailable")
            patches.append(
                (
                    "fanControl",
                    "fixed",
                    f"/operationModes/{mode}/fanSpeed/currentMode",
                )
            )
        patches.append(
            (
                "fanControl",
                value,
                f"/operationModes/{mode}/fanSpeed/modes/fixed",
            )
        )
        return patches, value, (
            lambda fresh: (
                (fresh.get("fan") or {}).get("mode") == "fixed"
                and _numeric_equal((fresh.get("fan") or {}).get("level"), value)
            )
        )

    if command in {"horizontal_swing", "vertical_swing"}:
        direction = command.split("_", 1)[0]
        value = str(requested)
        if value not in (fan_spec.get(f"{direction}_modes") or []):
            raise ValueError(f"Requested {direction} airflow mode is not supported")
        path = f"/operationModes/{mode}/fanDirection/{direction}/currentMode"
        return [("fanControl", value, path)], value, (
            lambda fresh: (fresh.get("fan") or {}).get(direction) == value
        )

    if command == "powerful":
        spec = controls.get("powerfulMode") or {}
        if not spec.get("settable"):
            raise ValueError("Powerful mode is not supported")
        enabled = (
            requested
            if isinstance(requested, bool)
            else str(requested).strip().lower() in {"1", "true", "yes", "on"}
        )
        value = "on" if enabled else "off"
        return [("powerfulMode", value, None)], enabled, (
            lambda fresh: bool(fresh.get("powerful_mode")) is enabled
        )

    raise ValueError("Unsupported HVAC command")


class OnectaControlService:
    """Serialize per-unit writes and confirm each accepted command once."""

    def __init__(
        self,
        *,
        monitor_getter: Callable[[], Any],
        patch: Callable[..., dict[str, int]] = patch_characteristic,
        registry_path: str | Path = DEFAULT_REGISTRY_PATH,
        enabled: Callable[[], bool] | None = None,
        confirm_delay_seconds: float | None = None,
        confirm_delays_seconds: tuple[float, ...] | None = None,
    ):
        self.monitor_getter = monitor_getter
        self.patch = patch
        self.registry_path = Path(registry_path)
        self.enabled = enabled or (
            lambda: is_truthy(retrieve_setting("ONECTA_CONTROL_ENABLED"), False)
        )
        if confirm_delays_seconds is not None:
            delays = confirm_delays_seconds
        elif confirm_delay_seconds is not None:
            # Backwards-compatible single-attempt hook used by deterministic tests.
            delays = (confirm_delay_seconds,)
        else:
            delays = DEFAULT_CONFIRM_DELAYS_SECONDS
        self.confirm_delays_seconds = tuple(
            max(0.0, float(delay)) for delay in delays
        ) or DEFAULT_CONFIRM_DELAYS_SECONDS
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._status: dict[str, dict[str, Any]] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def statuses(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._status.items()}

    def reconcile(self, snapshot: Mapping[str, Any] | None) -> None:
        """Heal pending UI state when a later scheduled snapshot converges."""
        for key, status in self._status.items():
            if status.get("status") not in {
                "accepted",
                "confirming",
                "accepted_unconfirmed",
            }:
                continue
            try:
                unit = _unit_snapshot(snapshot, key)
                _, _, confirmed = _command_patches(
                    unit,
                    str(status.get("command") or ""),
                    status.get("desired"),
                )
                if confirmed(unit):
                    status["status"] = "confirmed"
                    status["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                    status.pop("message", None)
            except (TypeError, ValueError):
                continue

    def command(self, key: str, command: str, requested: Any) -> dict[str, Any]:
        if not self.enabled():
            raise OnectaControlDisabled("ONECTA control is disabled in Configuration")
        command = str(command or "").strip()
        if command not in CONTROL_COMMANDS:
            raise ValueError("Unsupported HVAC command")

        monitor = self.monitor_getter()
        snapshot_unit = _unit_snapshot(monitor.snapshot, key)
        registry = load_control_registry(self.registry_path)
        private = (registry.get("units") or {}).get(key)
        if not isinstance(private, Mapping):
            logging.info(
                "ONECTA [control]: private metadata is missing; hydrating it "
                "with one all-unit refresh before %s.",
                command,
            )
            if not monitor.refresh():
                raise RuntimeError(
                    "HVAC control metadata could not be refreshed from Daikin"
                )
            snapshot_unit = _unit_snapshot(monitor.snapshot, key)
            registry = load_control_registry(self.registry_path)
            private = (registry.get("units") or {}).get(key)
            if not isinstance(private, Mapping):
                raise RuntimeError(
                    "HVAC control metadata is absent from the refreshed unit state"
                )

        patches, desired, confirmed = _command_patches(
            snapshot_unit, command, requested
        )
        if confirmed(snapshot_unit):
            result = {
                "id": None,
                "status": "already_set",
                "command": command,
                "desired": desired,
            }
            self._status[key] = result
            return result

        required_requests = len(patches) + len(self.confirm_delays_seconds)
        if not monitor.rate_limit_allows_requests(required_requests):
            raise RuntimeError(
                "ONECTA daily request reserve would be crossed by this command"
            )

        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            raise OnectaControlBusy("This HVAC unit already has a command in progress")

        command_id = uuid.uuid4().hex[:12]
        status = {
            "id": command_id,
            "status": "sending",
            "command": command,
            "desired": desired,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._status[key] = status
        try:
            for characteristic, value, path in patches:
                logging.info(
                    "ONECTA [control]: sending %s to %s.",
                    command,
                    key,
                )
                limits = self.patch(
                    str(private["gateway_id"]),
                    str(private["embedded_id"]),
                    characteristic,
                    value,
                    path=path,
                )
                monitor.update_rate_limits(limits)
            status["status"] = "accepted"
            logging.info("ONECTA [control]: accepted %s for %s.", command, key)
        except Exception:
            status["status"] = "failed"
            status["message"] = "Daikin rejected or could not receive the command"
            lock.release()
            raise

        # Preserve the synchronous command acknowledgement for the caller.  The
        # confirmation worker can complete before ``Thread.start`` returns in
        # fast/mock environments; returning a copy afterwards would then make
        # this API race between ``accepted`` and ``confirmed``.
        response = dict(status)
        worker = threading.Thread(
            target=self._confirm,
            args=(key, command, desired, confirmed, lock),
            name=f"onecta-confirm-{key}",
            daemon=True,
        )
        worker.start()
        return response

    def _confirm(
        self,
        key: str,
        command: str,
        desired: Any,
        confirmed: Callable[[Mapping[str, Any]], bool],
        lock: threading.Lock,
    ) -> None:
        try:
            status = self._status.get(key, {})
            for attempt, delay in enumerate(self.confirm_delays_seconds, start=1):
                time.sleep(delay)
                monitor = self.monitor_getter()
                refreshed = monitor.refresh()
                fresh_unit = _unit_snapshot(monitor.snapshot, key)
                if refreshed and confirmed(fresh_unit):
                    status["status"] = "confirmed"
                    status["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                    logging.info(
                        "ONECTA [control]: confirmed %s for %s after read %d.",
                        command,
                        key,
                        attempt,
                    )
                    return
                if attempt < len(self.confirm_delays_seconds):
                    status["status"] = "confirming"
                    status["message"] = (
                        "Physical command accepted; waiting for ONECTA state "
                        "propagation"
                    )
                    logging.info(
                        "ONECTA [control]: %s state has not propagated for %s; "
                        "one bounded confirmation read remains.",
                        command,
                        key,
                    )
            status["status"] = "accepted_unconfirmed"
            status["message"] = (
                "Command was accepted but ONECTA still reports the prior state"
            )
            logging.warning(
                "ONECTA [control]: %s accepted but not confirmed for %s "
                "within the bounded convergence window.",
                command,
                key,
            )
        except (OnectaError, OSError, RuntimeError, ValueError) as error:
            status = self._status.get(key, {})
            status["status"] = "accepted_unconfirmed"
            status["message"] = "Confirmation refresh was unavailable"
            logging.warning(
                "ONECTA [control]: confirmation unavailable for %s on %s: %s",
                command,
                key,
                error,
            )
        finally:
            lock.release()


_CONTROL_SERVICE: OnectaControlService | None = None
_CONTROL_LOCK = threading.Lock()


def get_control_service() -> OnectaControlService:
    global _CONTROL_SERVICE
    with _CONTROL_LOCK:
        if _CONTROL_SERVICE is None:
            from lib.onecta_monitor import get_monitor

            _CONTROL_SERVICE = OnectaControlService(
                monitor_getter=get_monitor,
                registry_path=(
                    retrieve_setting("ONECTA_CONTROL_REGISTRY_PATH")
                    or DEFAULT_REGISTRY_PATH
                ),
            )
        return _CONTROL_SERVICE


def hvac_dashboard() -> dict[str, Any]:
    from lib.onecta_monitor import get_monitor

    monitor = get_monitor()
    enabled = bool(monitor.enabled())
    snapshot = monitor.snapshot if enabled else None
    control_service = get_control_service()
    control_service.reconcile(snapshot)
    return {
        "enabled": enabled,
        "available": bool(snapshot and (snapshot.get("units") or [])),
        "control_enabled": bool(
            is_truthy(retrieve_setting("ONECTA_CONTROL_ENABLED"), False)
        ),
        "fetched_at": (snapshot or {}).get("fetched_at"),
        "source_updated_at": (snapshot or {}).get("source_updated_at"),
        "summary": (snapshot or {}).get("summary") or {},
        "quality": (snapshot or {}).get("quality") or {},
        "rate_limits": (snapshot or {}).get("rate_limits") or {},
        "units": (snapshot or {}).get("units") or [],
        "commands": control_service.statuses(),
    }
