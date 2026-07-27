"""Read-only Daikin ONECTA monitoring and cumulative HVAC energy history.

ONECTA exposes one 24-value ``d`` array per operating mode. The official app
renders this as twelve two-hour buckets for yesterday followed by twelve for
today. This module therefore never sums all 24 values as a daily total: the
first and final halves are stored separately.

The collector is deliberately fail-open and runs on its own daemon thread. A
bounded freshness wait lets an optimizer cycle consume a just-fetched snapshot,
but an unavailable Daikin cloud can never indefinitely block ESS control.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.helpers import is_truthy, publish_message
from lib.onecta_api import OnectaError, discover_gateway_devices
from lib.onecta_control import (
    load_control_registry,
    public_control_capabilities,
    store_control_registry,
    unit_key,
)


DEFAULT_LATEST_PATH = Path("data/hvac/latest.json")
DEFAULT_HISTORY_PATH = Path("data/hvac/history.ndjson")
DEFAULT_FETCH_INTERVAL_SECONDS = 20 * 60
DEFAULT_FETCH_OFFSET_SECONDS = 10 * 60
DEFAULT_OPTIMIZER_MAX_AGE_SECONDS = 19 * 60
DEFAULT_STARTUP_MAX_AGE_SECONDS = 19 * 60
DEFAULT_OPTIMIZER_WAIT_SECONDS = 5.0
DEFAULT_DAILY_REQUEST_RESERVE = 10
MIN_REQUEST_INTERVAL_SECONDS = 60
EXPECTED_DAY_BUCKETS = 24
BUCKETS_PER_DAY = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _value(capability: Any) -> Any:
    if isinstance(capability, Mapping) and "value" in capability:
        return capability.get("value")
    return capability


def _sum_bucket_half(
    values: Any,
    half: str,
    *,
    mode: str,
    issues: list[str],
) -> float | None:
    if not isinstance(values, list) or len(values) != EXPECTED_DAY_BUCKETS:
        length = len(values) if isinstance(values, list) else "missing"
        issues.append(f"{mode}_daily_array_length_{length}")
        return None
    selected = values[:BUCKETS_PER_DAY] if half == "yesterday" else values[BUCKETS_PER_DAY:]
    numbers: list[float] = []
    for value in selected:
        # Future two-hour buckets are legitimately null. They represent zero
        # accumulated energy so far, while malformed strings must invalidate the
        # total instead of silently hiding a schema change.
        if value is None:
            numbers.append(0.0)
            continue
        number = _float(value)
        if number is None or number < 0:
            issues.append(f"{mode}_{half}_invalid_bucket")
            return None
        numbers.append(number)
    return round(sum(numbers), 3)


def _find_climate_control(device: Mapping[str, Any]) -> Mapping[str, Any] | None:
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


def _setpoint(point: Mapping[str, Any], mode: str) -> float | None:
    control = _value(point.get("temperatureControl"))
    if not isinstance(control, Mapping):
        return None
    operation_modes = control.get("operationModes")
    if not isinstance(operation_modes, Mapping):
        return None
    mode_data = operation_modes.get(mode)
    if not isinstance(mode_data, Mapping):
        return None
    setpoints = mode_data.get("setpoints")
    if not isinstance(setpoints, Mapping):
        return None
    return _float(_value(setpoints.get("roomTemperature")))


def _fan_state(point: Mapping[str, Any], mode: str) -> dict[str, Any]:
    control = _value(point.get("fanControl"))
    if not isinstance(control, Mapping):
        return {"mode": None, "level": None, "horizontal": None, "vertical": None}
    operation_modes = control.get("operationModes")
    if not isinstance(operation_modes, Mapping):
        return {"mode": None, "level": None, "horizontal": None, "vertical": None}
    mode_data = operation_modes.get(mode)
    if not isinstance(mode_data, Mapping):
        return {"mode": None, "level": None, "horizontal": None, "vertical": None}
    speed = mode_data.get("fanSpeed")
    if not isinstance(speed, Mapping):
        return {"mode": None, "level": None, "horizontal": None, "vertical": None}
    speed_mode = _value(speed.get("currentMode"))
    levels = speed.get("modes")
    fixed = levels.get("fixed") if isinstance(levels, Mapping) else None
    directions = mode_data.get("fanDirection")
    directions = directions if isinstance(directions, Mapping) else {}
    horizontal = directions.get("horizontal")
    horizontal = horizontal if isinstance(horizontal, Mapping) else {}
    vertical = directions.get("vertical")
    vertical = vertical if isinstance(vertical, Mapping) else {}
    return {
        "mode": str(speed_mode) if speed_mode is not None else None,
        "level": _float(_value(fixed)) if fixed is not None else None,
        "horizontal": _value(horizontal.get("currentMode")),
        "vertical": _value(vertical.get("currentMode")),
    }


def _aggregate_or_none(values: list[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return round(sum(float(value) for value in values), 3)


def _add_or_none(*values: float | None) -> float | None:
    if any(value is None for value in values):
        return None
    return round(sum(float(value) for value in values), 3)


def normalize_hvac_snapshot(
    devices: list[dict[str, Any]],
    rate_limits: Mapping[str, int] | None = None,
    *,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """Normalize one all-device response into anonymous forecast-safe state."""
    fetched = fetched_at or _utcnow()
    issues: list[str] = []
    units: list[dict[str, Any]] = []

    for index, device in enumerate(devices, start=1):
        point = _find_climate_control(device)
        if point is None:
            issues.append(f"unit_{index}_missing_climate_control")
            continue

        consumption = _value(point.get("consumptionData"))
        electrical = (
            consumption.get("electrical")
            if isinstance(consumption, Mapping)
            else None
        )
        if not isinstance(electrical, Mapping):
            electrical = {}

        energy: dict[str, dict[str, float | None]] = {}
        for half in ("today", "yesterday"):
            mode_totals = {}
            for mode in ("cooling", "heating"):
                mode_data = electrical.get(mode)
                daily = mode_data.get("d") if isinstance(mode_data, Mapping) else None
                mode_totals[f"{mode}_kwh"] = _sum_bucket_half(
                    daily,
                    half,
                    mode=f"unit_{index}_{mode}",
                    issues=issues,
                )
            mode_totals["total_kwh"] = _add_or_none(
                mode_totals["cooling_kwh"],
                mode_totals["heating_kwh"],
            )
            energy[half] = mode_totals

        sensory = _value(point.get("sensoryData"))
        sensory = sensory if isinstance(sensory, Mapping) else {}
        mode = str(_value(point.get("operationMode")) or "unknown")
        cloud_connected = _value(device.get("isCloudConnectionUp"))
        units.append(
            {
                "unit": unit_key(device, index),
                "display_name": str(_value(point.get("name")) or f"HVAC {index}"),
                "source_updated_at": device.get("timestamp"),
                "cloud_connected": (
                    bool(cloud_connected)
                    if isinstance(cloud_connected, bool)
                    else None
                ),
                "power": str(_value(point.get("onOffMode")) or "unknown"),
                "operation_mode": mode,
                "room_temperature_c": _float(
                    _value(sensory.get("roomTemperature"))
                ),
                "outdoor_temperature_c": _float(
                    _value(sensory.get("outdoorTemperature"))
                ),
                "target_temperature_c": _setpoint(point, mode),
                "fan": _fan_state(point, mode),
                "controls": public_control_capabilities(point),
                "powerful_mode": bool(
                    _value(point.get("isPowerfulModeActive")) is True
                ),
                "health": {
                    "error": bool(_value(point.get("isInErrorState")) is True),
                    "warning": bool(
                        _value(point.get("isInWarningState")) is True
                    ),
                    "caution": bool(
                        _value(point.get("isInCautionState")) is True
                    ),
                    "error_code": str(_value(point.get("errorCode")) or ""),
                },
                "energy": energy,
            }
        )

    all_devices_present = bool(devices) and len(units) == len(devices)
    today_cooling = (
        _aggregate_or_none(
            [unit["energy"]["today"]["cooling_kwh"] for unit in units]
        )
        if all_devices_present
        else None
    )
    today_heating = (
        _aggregate_or_none(
            [unit["energy"]["today"]["heating_kwh"] for unit in units]
        )
        if all_devices_present
        else None
    )
    yesterday_cooling = (
        _aggregate_or_none(
            [unit["energy"]["yesterday"]["cooling_kwh"] for unit in units]
        )
        if all_devices_present
        else None
    )
    yesterday_heating = (
        _aggregate_or_none(
            [unit["energy"]["yesterday"]["heating_kwh"] for unit in units]
        )
        if all_devices_present
        else None
    )
    source_times = sorted(
        {
            str(unit["source_updated_at"])
            for unit in units
            if unit.get("source_updated_at")
        }
    )
    return {
        "schema_version": 1,
        "source": "daikin-onecta",
        "fetched_at": fetched.astimezone(timezone.utc).isoformat(),
        "source_updated_at": source_times[-1] if source_times else None,
        "summary": {
            "device_count": len(units),
            "connected_units": sum(
                unit.get("cloud_connected") is True for unit in units
            ),
            "powered_units": sum(unit.get("power") == "on" for unit in units),
            "configured_modes": sorted(
                {
                    unit["operation_mode"]
                    for unit in units
                    if unit.get("operation_mode") not in (None, "unknown")
                }
            ),
            "powered_modes": sorted(
                {
                    unit["operation_mode"]
                    for unit in units
                    if unit.get("power") == "on"
                    and unit.get("operation_mode") not in (None, "unknown")
                }
            ),
            "today_cooling_kwh": today_cooling,
            "today_heating_kwh": today_heating,
            "today_total_kwh": _add_or_none(today_cooling, today_heating),
            "yesterday_cooling_kwh": yesterday_cooling,
            "yesterday_heating_kwh": yesterday_heating,
            "yesterday_total_kwh": _add_or_none(
                yesterday_cooling, yesterday_heating
            ),
        },
        "quality": {
            "daily_bucket_semantics": (
                "24 two-hour buckets: first 12 yesterday, final 12 today"
            ),
            "complete": not issues and all_devices_present,
            "issues": sorted(set(issues)),
        },
        "rate_limits": dict(rate_limits or {}),
        "units": units,
    }


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
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


def _history_signature(snapshot: Mapping[str, Any]) -> str:
    stable = {
        key: snapshot.get(key)
        for key in ("schema_version", "source_updated_at", "summary", "quality", "units")
    }
    return hashlib.sha256(
        json.dumps(stable, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def store_snapshot(
    snapshot: dict[str, Any],
    latest_path: str | Path = DEFAULT_LATEST_PATH,
    history_path: str | Path = DEFAULT_HISTORY_PATH,
) -> bool:
    """Atomically publish latest state and append history only when state changed."""
    latest = Path(latest_path)
    history = Path(history_path)
    previous = None
    try:
        previous = json.loads(latest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass

    _atomic_json_write(latest, snapshot)
    if isinstance(previous, Mapping) and _history_signature(previous) == _history_signature(snapshot):
        return False

    history.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(snapshot, separators=(",", ":"), sort_keys=True) + "\n"
    with open(history, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(history, 0o600)
    except OSError:
        pass
    return True


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _snapshot_age_seconds(snapshot: Mapping[str, Any] | None) -> float | None:
    if not snapshot:
        return None
    try:
        value = str(snapshot["fetched_at"]).replace("Z", "+00:00")
        fetched = datetime.fromisoformat(value)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return max(0.0, (_utcnow() - fetched.astimezone(timezone.utc)).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None


def _next_fetch_due_epoch(
    now: float,
    interval_seconds: int,
    offset_seconds: int = DEFAULT_FETCH_OFFSET_SECONDS,
) -> float:
    shifted = now - offset_seconds
    return (
        (int(shifted // interval_seconds) + 1) * interval_seconds
        + offset_seconds
    )


class OnectaMonitor:
    """Single-flight, rate-conscious read-only ONECTA snapshot collector."""

    def __init__(
        self,
        *,
        fetch: Callable[[], tuple[list[dict[str, Any]], dict[str, int]]] = discover_gateway_devices,
        latest_path: str | Path = DEFAULT_LATEST_PATH,
        history_path: str | Path = DEFAULT_HISTORY_PATH,
        registry_path: str | Path | None = None,
        enabled: Callable[[], bool] | None = None,
        daily_request_reserve: int = DEFAULT_DAILY_REQUEST_RESERVE,
        expected_units: int = 0,
    ):
        self.fetch = fetch
        self.latest_path = Path(latest_path)
        self.history_path = Path(history_path)
        self.registry_path = (
            Path(registry_path)
            if registry_path is not None
            else self.latest_path.parent / "control_registry.json"
        )
        self.enabled = enabled or (
            lambda: is_truthy(retrieve_setting("ONECTA_ENABLED"), False)
        )
        self.daily_request_reserve = max(0, int(daily_request_reserve))
        self.expected_units = max(0, int(expected_units))
        self.snapshot = _read_snapshot(self.latest_path)
        self._refresh_lock = threading.Lock()
        self._refresh_done = threading.Event()
        self._refresh_done.set()
        self._worker: threading.Thread | None = None
        self._loop_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_attempt_monotonic = 0.0
        self._last_error: str | None = None

    def rate_limit_allows_requests(self, count: int = 1) -> bool:
        limits = (self.snapshot or {}).get("rate_limits") or {}
        remaining = _int(limits.get("remaining_day"), -1)
        return remaining < 0 or remaining - max(1, int(count)) >= self.daily_request_reserve

    def update_rate_limits(self, limits: Mapping[str, int] | None) -> None:
        if not limits or self.snapshot is None:
            return
        self.snapshot["rate_limits"] = dict(limits)

    def _rate_limit_allows_fetch(self) -> bool:
        return self.rate_limit_allows_requests(1)

    def control_registry_ready(self) -> bool:
        public_keys = {
            str(unit.get("unit"))
            for unit in (self.snapshot or {}).get("units") or []
            if unit.get("unit")
        }
        private_keys = set(
            (load_control_registry(self.registry_path).get("units") or {}).keys()
        )
        return bool(public_keys) and public_keys.issubset(private_keys)

    def refresh(self) -> bool:
        if not self.enabled():
            return False
        if not self._rate_limit_allows_fetch():
            self._last_error = "daily request reserve reached"
            logging.warning(
                "ONECTA: preserving the final %s daily requests; using cached HVAC state.",
                self.daily_request_reserve,
            )
            return False

        with self._refresh_lock:
            self._refresh_done.clear()
            self._last_attempt_monotonic = time.monotonic()
            try:
                devices, limits = self.fetch()
                prior_expected = _int(
                    ((self.snapshot or {}).get("summary") or {}).get(
                        "device_count"
                    ),
                    0,
                )
                expected = max(self.expected_units, prior_expected)
                if not devices:
                    raise OnectaError(
                        "ONECTA gateway-device response contained no HVAC units"
                    )
                if expected and len(devices) < expected:
                    raise OnectaError(
                        "ONECTA gateway-device response was incomplete "
                        f"({len(devices)}/{expected} units)"
                    )
                snapshot = normalize_hvac_snapshot(
                    devices,
                    limits,
                    fetched_at=_utcnow(),
                )
                # Raw Daikin identifiers never enter MQTT or the dashboard.
                # Persist them separately, mode 0600, for guarded control only.
                try:
                    store_control_registry(devices, self.registry_path)
                except OSError as error:
                    # A private-registry storage fault must not discard otherwise
                    # valid read-only energy/state data.
                    logging.warning(
                        "ONECTA: control metadata could not be stored; "
                        "read-only monitoring remains available: %s",
                        error,
                    )
                store_snapshot(snapshot, self.latest_path, self.history_path)
                self.snapshot = snapshot
                self._last_error = None
                self._publish(snapshot)
                logging.info(
                    "ONECTA: refreshed %d HVAC units; today %.2f kWh "
                    "(cooling %.2f, heating %.2f).",
                    snapshot["summary"]["device_count"],
                    snapshot["summary"]["today_total_kwh"] or 0.0,
                    snapshot["summary"]["today_cooling_kwh"] or 0.0,
                    snapshot["summary"]["today_heating_kwh"] or 0.0,
                )
                return True
            except OnectaError as error:
                self._last_error = str(error)
                logging.warning(
                    "ONECTA: read-only refresh failed; retaining prior snapshot: %s",
                    error,
                )
                self._publish_status("stale" if self.snapshot else "unavailable")
                return False
            except Exception as error:
                self._last_error = type(error).__name__
                logging.warning(
                    "ONECTA: unexpected refresh failure; retaining prior snapshot: %s",
                    error,
                )
                self._publish_status("stale" if self.snapshot else "unavailable")
                return False
            finally:
                self._refresh_done.set()

    def _publish_status(self, status: str) -> None:
        publish_message(
            "hvac/status",
            payload=json.dumps(
                {
                    "status": status,
                    "fetched_at": (self.snapshot or {}).get("fetched_at"),
                    "source_updated_at": (self.snapshot or {}).get(
                        "source_updated_at"
                    ),
                },
                separators=(",", ":"),
            ),
            retain=True,
        )

    def _publish(self, snapshot: Mapping[str, Any]) -> None:
        self._publish_status("connected")
        summary = {
            "fetched_at": snapshot.get("fetched_at"),
            "source_updated_at": snapshot.get("source_updated_at"),
            "summary": snapshot.get("summary"),
            "quality": snapshot.get("quality"),
            "rate_limits": snapshot.get("rate_limits"),
        }
        publish_message(
            "hvac/summary",
            payload=json.dumps(summary, separators=(",", ":"), sort_keys=True),
            retain=True,
        )
        for unit in snapshot.get("units") or []:
            publish_message(
                f"hvac/units/{unit['unit']}",
                payload=json.dumps(unit, separators=(",", ":"), sort_keys=True),
                retain=True,
            )

    def request_refresh(
        self,
        *,
        max_cached_age_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
        force: bool = False,
    ) -> threading.Thread | None:
        if not self.enabled() or not self._rate_limit_allows_fetch():
            return None
        if self._worker is not None and self._worker.is_alive():
            return self._worker
        durable_age = _snapshot_age_seconds(self.snapshot)
        if (
            not force
            and
            durable_age is not None
            and durable_age < max(MIN_REQUEST_INTERVAL_SECONDS, max_cached_age_seconds)
        ):
            return None
        if (
            self._last_attempt_monotonic
            and time.monotonic() - self._last_attempt_monotonic
            < MIN_REQUEST_INTERVAL_SECONDS
        ):
            return None
        self._worker = threading.Thread(
            target=self.refresh,
            name="onecta-refresh",
            daemon=True,
        )
        # Clear before starting so ensure_fresh cannot observe the previous
        # completed event in the narrow interval before refresh() begins.
        self._refresh_done.clear()
        self._worker.start()
        return self._worker

    def ensure_fresh(
        self,
        *,
        max_age_seconds: float = DEFAULT_OPTIMIZER_MAX_AGE_SECONDS,
        wait_timeout_seconds: float = DEFAULT_OPTIMIZER_WAIT_SECONDS,
    ) -> dict[str, Any]:
        enabled = bool(self.enabled())
        if not enabled:
            return {"enabled": False, "available": False, "fresh": False}

        age = _snapshot_age_seconds(self.snapshot)
        if age is None or age > max(0.0, float(max_age_seconds)):
            worker = self.request_refresh()
            if worker is not None or (
                self._worker is not None and self._worker.is_alive()
            ):
                self._refresh_done.wait(max(0.0, float(wait_timeout_seconds)))
            age = _snapshot_age_seconds(self.snapshot)

        snapshot = self.snapshot
        fresh = (
            snapshot is not None
            and age is not None
            and age <= max(0.0, float(max_age_seconds))
        )
        return {
            "enabled": True,
            "available": snapshot is not None,
            "fresh": fresh,
            "age_seconds": round(age, 1) if age is not None else None,
            "fetched_at": (snapshot or {}).get("fetched_at"),
            "source_updated_at": (snapshot or {}).get("source_updated_at"),
            "summary": (snapshot or {}).get("summary") or {},
            "quality": (snapshot or {}).get("quality") or {},
            "last_error": self._last_error,
        }

    def _loop(self) -> None:
        startup_max_age = max(
            MIN_REQUEST_INTERVAL_SECONDS,
            _int(
                retrieve_setting("ONECTA_STARTUP_MAX_AGE_SECONDS"),
                DEFAULT_STARTUP_MAX_AGE_SECONDS,
            ),
        )
        control_needs_metadata = (
            is_truthy(retrieve_setting("ONECTA_CONTROL_ENABLED"), False)
            and not self.control_registry_ready()
        )
        if self.request_refresh(
            max_cached_age_seconds=startup_max_age,
            force=control_needs_metadata,
        ) is None:
            if self.snapshot is not None:
                self._publish(self.snapshot)
                logging.info(
                    "ONECTA: reused cached HVAC snapshot on startup (age %.1f min).",
                    (_snapshot_age_seconds(self.snapshot) or 0.0) / 60.0,
                )
        while not self._stop.is_set():
            now = time.time()
            interval_seconds = max(
                10 * 60,
                min(
                    60 * 60,
                    _int(
                        retrieve_setting("ONECTA_POLL_INTERVAL_MIN"),
                        DEFAULT_FETCH_INTERVAL_SECONDS // 60,
                    )
                    * 60,
                ),
            )
            # With the default 20-minute cadence this yields :10, :30 and :50.
            # The optimizer then sees a snapshot no more than 15 minutes old.
            due = _next_fetch_due_epoch(now, interval_seconds)
            due = max(now + 1.0, due)
            if self._stop.wait(due - now):
                break
            self.request_refresh()

    def start(self) -> threading.Thread | None:
        if not self.enabled():
            return None
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return self._loop_thread
        self._stop.clear()
        self._loop_thread = threading.Thread(
            target=self._loop,
            name="onecta-monitor",
            daemon=True,
        )
        self._loop_thread.start()
        return self._loop_thread

    def stop(self) -> None:
        self._stop.set()


_MONITOR: OnectaMonitor | None = None
_MONITOR_LOCK = threading.Lock()


def get_monitor() -> OnectaMonitor:
    global _MONITOR
    with _MONITOR_LOCK:
        if _MONITOR is None:
            _MONITOR = OnectaMonitor(
                latest_path=retrieve_setting("ONECTA_LATEST_PATH")
                or DEFAULT_LATEST_PATH,
                history_path=retrieve_setting("ONECTA_HISTORY_PATH")
                or DEFAULT_HISTORY_PATH,
                registry_path=retrieve_setting("ONECTA_CONTROL_REGISTRY_PATH")
                or "data/hvac/control_registry.json",
                daily_request_reserve=_int(
                    retrieve_setting("ONECTA_DAILY_REQUEST_RESERVE"),
                    DEFAULT_DAILY_REQUEST_RESERVE,
                ),
                expected_units=_int(
                    retrieve_setting("ONECTA_EXPECTED_UNITS"),
                    0,
                ),
            )
        return _MONITOR


def start_onecta_monitor_if_enabled() -> threading.Thread | None:
    """Start the read-only collector without delaying application startup."""
    monitor = get_monitor()
    thread = monitor.start()
    if thread is not None:
        logging.info(
            "ONECTA: read-only HVAC monitor started; refreshes run before "
            "optimizer cycles on a rate-conscious 20-minute cadence."
        )
    else:
        monitor._publish_status("disabled")
    return thread


def stop_onecta_monitor() -> None:
    if _MONITOR is not None:
        _MONITOR.stop()


def hvac_context_for_optimizer() -> dict[str, Any]:
    """Return bounded-fresh HVAC context; cloud failure never blocks control."""
    max_age = max(
        60,
        _int(
            retrieve_setting("ONECTA_OPTIMIZER_MAX_AGE_SECONDS"),
            DEFAULT_OPTIMIZER_MAX_AGE_SECONDS,
        ),
    )
    try:
        wait_seconds = max(
            0.0,
            min(
                20.0,
                float(
                    retrieve_setting("ONECTA_OPTIMIZER_WAIT_SECONDS")
                    or DEFAULT_OPTIMIZER_WAIT_SECONDS
                ),
            ),
        )
    except (TypeError, ValueError):
        wait_seconds = DEFAULT_OPTIMIZER_WAIT_SECONDS
    return get_monitor().ensure_fresh(
        max_age_seconds=max_age,
        wait_timeout_seconds=wait_seconds,
    )
