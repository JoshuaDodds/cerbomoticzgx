"""Persist and overlay accepted flexible appliance-load reservations.

The Home Connect coordinator writes a reservation only after the appliance has
acknowledged its delayed start. EnergyBroker reads the same small JSON document
and adds the known demand to its ordinary house-load forecast before optimization.
Writes are atomic and the file contains at most one active entry per device.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path


_LOCK = threading.RLock()
_FILENAME = "appliance-reservations.json"


def default_path() -> Path:
    """Return the reservation path beside hot ESS history."""
    from lib.config_retrieval import retrieve_setting

    return Path(retrieve_setting("HISTORY_DIR") or "data/history") / _FILENAME


def _path(path=None) -> Path:
    return Path(path) if path is not None else default_path()


def _parse_aware(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _valid(reservation):
    if not isinstance(reservation, dict) or not str(reservation.get("device") or "").strip():
        return None
    start = _parse_aware(reservation.get("start"))
    end = _parse_aware(reservation.get("end"))
    try:
        load_kw = float(reservation.get("load_kw"))
    except (TypeError, ValueError):
        return None
    if (start is None or end is None or end <= start or not math.isfinite(load_kw)
            or load_kw <= 0.0):
        return None
    return start, end, load_kw


def _read(path=None) -> list[dict]:
    target = _path(path)
    try:
        with target.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return value if isinstance(value, list) else []


def _write(values, path=None) -> None:
    """Atomically write the small reservation list."""
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(values, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def active(*, path=None, now=None) -> list[dict]:
    """Return valid reservations whose end is still in the future."""
    with _LOCK:
        values = _read(path)
        if now is None:
            now = datetime.now().astimezone()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        kept = []
        for value in values:
            parsed = _valid(value)
            if parsed is not None and parsed[1] > now:
                kept.append(value)
        if kept != values:
            _write(kept, path)
        return kept


def upsert(reservation: dict, *, path=None) -> None:
    """Insert or replace one device reservation."""
    if _valid(reservation) is None:
        raise ValueError("invalid appliance reservation")
    device = str(reservation["device"])
    with _LOCK:
        values = [
            item for item in _read(path)
            if isinstance(item, dict) and str(item.get("device")) != device
        ]
        values.append(dict(reservation))
        _write(values, path)


def remove(device: str, *, path=None) -> bool:
    """Remove a device reservation, returning whether one existed."""
    with _LOCK:
        values = _read(path)
        kept = [
            item for item in values
            if not isinstance(item, dict) or str(item.get("device")) != str(device)
        ]
        changed = len(kept) != len(values)
        if changed:
            _write(kept, path)
        return changed


def _load_segments(reservation, parsed):
    """Return elapsed-time load segments, preferring a measured/planned profile."""
    reservation_start, reservation_end, average_load_kw = parsed
    profile_segments = []
    for item in reservation.get("load_profile") or []:
        if not isinstance(item, dict):
            continue
        start = _parse_aware(item.get("start"))
        end = _parse_aware(item.get("end"))
        try:
            energy_kwh = float(item.get("energy_kwh"))
        except (TypeError, ValueError):
            continue
        if (
            start is None
            or end is None
            or end.timestamp() <= start.timestamp()
            or not math.isfinite(energy_kwh)
            or energy_kwh < 0.0
        ):
            continue
        duration_h = (end.timestamp() - start.timestamp()) / 3600.0
        profile_segments.append((start.timestamp(), end.timestamp(), energy_kwh / duration_h))
    if profile_segments:
        return profile_segments
    return [(
        reservation_start.timestamp(),
        reservation_end.timestamp(),
        average_load_kw,
    )]


def overlay_forecast(forecast: dict, reservations: list[dict],
                     *, slot_duration_h: float) -> tuple[dict, dict]:
    """Add reservation energy overlapping each optimizer forecast slot."""
    duration_h = float(slot_duration_h)
    if not math.isfinite(duration_h) or duration_h <= 0.0:
        raise ValueError("slot_duration_h must be positive and finite")
    result = dict(forecast or {})
    devices = set()
    reserved_kwh = 0.0
    for slot_start in result:
        if slot_start.tzinfo is None or slot_start.utcoffset() is None:
            continue
        slot_start_ts = slot_start.timestamp()
        slot_end_ts = slot_start_ts + duration_h * 3600.0
        for reservation in reservations or []:
            parsed = _valid(reservation)
            if parsed is None:
                continue
            device_energy = 0.0
            for start_ts, end_ts, load_kw in _load_segments(reservation, parsed):
                overlap_seconds = min(slot_end_ts, end_ts) - max(slot_start_ts, start_ts)
                if overlap_seconds > 0.0:
                    device_energy += load_kw * overlap_seconds / 3600.0
            if device_energy > 0.0:
                result[slot_start] = (
                    float(result.get(slot_start) or 0.0) + device_energy)
                reserved_kwh += device_energy
                devices.add(str(reservation["device"]))
    return result, {
        "devices": sorted(devices),
        "reserved_kwh": reserved_kwh,
        "active_reservations": len(reservations or []),
    }


__all__ = [
    "active",
    "default_path",
    "overlay_forecast",
    "remove",
    "upsert",
]
