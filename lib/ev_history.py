"""Measured EV charging history and conservative cost attribution.

The ABB charger meter provides authoritative delivered energy.  The site meter
provides total grid import and cost, but cannot identify which simultaneous load
consumed a particular imported kWh.  We therefore attribute grid import to the
EV in proportion to its share of measured site load and label the result as an
attribution rather than claiming source-level metering.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from lib import history_store


_ACTIVE_SLOT_KWH = 0.02
_SESSION_GAP_SECONDS = 5 * 60
_EV_START_POWER_W = 500.0
_EV_STOP_POWER_W = 100.0
_POWER_STATE_FILENAME = ".ev-power-state.json"
_POWER_STATE_LOCK = threading.Lock()
_PROCESS_TOKEN = f"{os.getpid()}-{id(_POWER_STATE_LOCK)}"


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _power_state_path(history_dir: str) -> str:
    return os.path.join(history_dir, _POWER_STATE_FILENAME)


def _read_power_state(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_power_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def record_ev_power_observation(
    power_w,
    *,
    now: datetime | None = None,
    history_dir: str | None = None,
) -> dict | None:
    """Persist ABB-meter charging start/stop observations.

    The high-frequency power stream is reduced to transitions, avoiding needless
    disk writes on small systems. Hysteresis rejects the ABB meter's few watts of
    idle electronics and prevents threshold chatter. The first active reading
    after state loss is retained but explicitly labelled as an observation
    boundary rather than an exact start time.
    """
    power = _number(power_w)
    if power is None or power < 0.0:
        return None
    now = now or datetime.now().astimezone()
    history_dir = history_store.resolve_history_dir(history_dir)
    state_path = _power_state_path(history_dir)

    with _POWER_STATE_LOCK:
        prior = _read_power_state(state_path)
        prior_active = prior.get("active") if prior is not None else None
        first_observation_this_process = (
            prior is not None and prior.get("writer_token") != _PROCESS_TOKEN
        )
        if prior_active is True:
            active = power > _EV_STOP_POWER_W
        else:
            active = power >= _EV_START_POWER_W

        transition = None
        if prior_active is None:
            # Establish the durable baseline. An already-active charger must be
            # visible, but its true start predates this first observation.
            if active:
                transition = {
                    "ts": now.isoformat(),
                    "kind": "ev_charge_transition",
                    "event": "started",
                    "source": "abb_meter",
                    "timing_quality": "first_active_observation",
                    "ev_w": round(power, 1),
                }
        elif bool(prior_active) != active:
            timing_quality = "meter_transition"
            if first_observation_this_process:
                timing_quality = (
                    "first_active_observation" if active
                    else "first_idle_observation"
                )
            transition = {
                "ts": now.isoformat(),
                "kind": "ev_charge_transition",
                "event": "started" if active else "stopped",
                "source": "abb_meter",
                "timing_quality": timing_quality,
                "ev_w": round(power, 1),
            }

        # Append first. If the subsequent state replace fails, the next
        # observation may repeat this edge, but the session reducer safely
        # coalesces duplicate starts/stops. The reverse ordering could lose an
        # event permanently after a transient history-volume failure.
        if transition is not None:
            history_store.append(now.date(), transition, history_dir)
        if (
            prior_active is None
            or bool(prior_active) != active
            or first_observation_this_process
        ):
            _write_power_state(state_path, {
                "schema_version": 1,
                "active": active,
                "changed_at": now.isoformat(),
                "power_w": round(power, 1),
                "writer_token": _PROCESS_TOKEN,
            })
        return transition


def measured_ev_sessions(records: list[dict]) -> list[dict]:
    """Pair measured ABB power transitions into charging sessions.

    An unmatched start represents a session still active at the end of the
    supplied history. A stop without a preceding start is ignored because its
    beginning cannot be represented truthfully from the available records.
    """
    transitions = []
    for record in records:
        if (
            record.get("kind") != "ev_charge_transition"
            or record.get("source") != "abb_meter"
            or record.get("event") not in {"started", "stopped"}
        ):
            continue
        try:
            timestamp = datetime.fromisoformat(
                str(record.get("ts")).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        transitions.append((timestamp, record))
    transitions.sort(key=lambda item: item[0])

    sessions = []
    active = None
    for timestamp, record in transitions:
        if record["event"] == "started":
            if active is None:
                active = {
                    "start": timestamp,
                    "timing_quality": record.get("timing_quality")
                    or "meter_transition",
                }
            continue
        if active is None:
            continue
        stop_quality = record.get("timing_quality") or "meter_transition"
        session_quality = active["timing_quality"]
        if stop_quality != "meter_transition":
            session_quality = stop_quality
        sessions.append({
            "start": active["start"].isoformat(),
            "end": timestamp.isoformat(),
            "timing_quality": session_quality,
        })
        active = None
    if active is not None:
        sessions.append({
            "start": active["start"].isoformat(),
            "end": None,
            "timing_quality": active["timing_quality"],
        })
    return sessions


def attribute_ev_grid_cost(
    *,
    ev_charge_kwh,
    site_load_kwh,
    site_import_kwh,
    site_import_cost_eur,
) -> dict:
    """Attribute measured site import/cost to EV load for one settled interval.

    ``ev_charge_kwh`` remains the source-of-truth energy measurement.  Imported
    energy is allocated proportionally by EV share of site load, capped at the
    measured EV energy.  The remainder is labelled non-grid: it may be direct PV
    or home-battery energy, so it is not described as free and no invented cost
    is assigned to it.
    """
    ev_kwh = _number(ev_charge_kwh)
    load_kwh = _number(site_load_kwh)
    import_kwh = _number(site_import_kwh)
    import_cost = _number(site_import_cost_eur)

    unavailable = {
        "ev_grid_import_kwh": None,
        "ev_non_grid_kwh": None,
        "ev_grid_cost_eur": None,
        "ev_cost_quality": "insufficient_site_data",
    }
    if ev_kwh is None:
        return unavailable

    ev_kwh = max(0.0, ev_kwh)
    if ev_kwh <= 1e-9:
        return {
            "ev_grid_import_kwh": 0.0,
            "ev_non_grid_kwh": 0.0,
            "ev_grid_cost_eur": 0.0,
            "ev_cost_quality": "no_ev_charge",
        }
    if import_kwh is None:
        return unavailable

    import_kwh = max(0.0, import_kwh)
    if import_kwh <= 1e-9:
        return {
            "ev_grid_import_kwh": 0.0,
            "ev_non_grid_kwh": round(ev_kwh, 3),
            "ev_grid_cost_eur": 0.0,
            "ev_cost_quality": "no_grid_import",
        }
    if load_kwh is None or load_kwh <= 0.0 or import_cost is None:
        return unavailable

    ev_share = min(1.0, ev_kwh / load_kwh)
    ev_grid_kwh = min(ev_kwh, import_kwh * ev_share)
    unit_import_cost = max(0.0, import_cost) / import_kwh
    return {
        "ev_grid_import_kwh": round(ev_grid_kwh, 3),
        "ev_non_grid_kwh": round(max(0.0, ev_kwh - ev_grid_kwh), 3),
        "ev_grid_cost_eur": round(ev_grid_kwh * unit_import_cost, 4),
        "ev_cost_quality": "proportional_site_load",
    }


def _stored_or_derived_attribution(record: dict) -> dict:
    keys = (
        "ev_grid_import_kwh",
        "ev_non_grid_kwh",
        "ev_grid_cost_eur",
        "ev_cost_quality",
    )
    if any(record.get(key) is not None for key in keys[:3]):
        return {key: record.get(key) for key in keys}
    return attribute_ev_grid_cost(
        ev_charge_kwh=record.get("ev_charge_kwh"),
        site_load_kwh=record.get("actual_load_kwh"),
        site_import_kwh=record.get("actual_import_kwh"),
        site_import_cost_eur=record.get("actual_cost"),
    )


def summarize_ev_day(records: list[dict]) -> dict:
    """Roll cycle/settlement rows into durable, backwards-compatible EV totals."""
    daily_meter_kwh = None
    settlements = []
    for record in records:
        if record.get("kind") in (None, "cycle"):
            value = _number(record.get("ev_actual_today_kwh"))
            if value is not None:
                daily_meter_kwh = max(0.0, value)
        elif record.get("kind") == "settlement":
            settlements.append(record)

    measured_total = 0.0
    grid_total = non_grid_total = cost_total = 0.0
    measured_slots = missing_slots = active_slots = 0
    grid_slots = non_grid_slots = cost_slots = active_cost_slots = 0
    active_intervals = []

    for record in settlements:
        ev_kwh = _number(record.get("ev_charge_kwh"))
        measured = record.get("ev_meter_quality") == "measured" and ev_kwh is not None
        if not measured:
            missing_slots += 1
            continue

        ev_kwh = max(0.0, ev_kwh)
        measured_slots += 1
        measured_total += ev_kwh
        is_active = ev_kwh > _ACTIVE_SLOT_KWH
        if is_active:
            active_slots += 1
            try:
                start = datetime.fromisoformat(
                    str(record.get("slot_start")).replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    str(record.get("slot_end")).replace("Z", "+00:00")
                )
                active_intervals.append((start, end))
            except (TypeError, ValueError):
                pass

        attribution = _stored_or_derived_attribution(record)
        grid_kwh = _number(attribution.get("ev_grid_import_kwh"))
        non_grid_kwh = _number(attribution.get("ev_non_grid_kwh"))
        cost_eur = _number(attribution.get("ev_grid_cost_eur"))
        if grid_kwh is not None:
            grid_total += max(0.0, grid_kwh)
            grid_slots += 1
        if non_grid_kwh is not None:
            non_grid_total += max(0.0, non_grid_kwh)
            non_grid_slots += 1
        if cost_eur is not None:
            cost_total += max(0.0, cost_eur)
            cost_slots += 1
        if (
            is_active and grid_kwh is not None
            and non_grid_kwh is not None and cost_eur is not None
        ):
            active_cost_slots += 1

    transition_sessions = measured_ev_sessions(records)
    active_intervals.sort(key=lambda interval: interval[0])
    sessions = 0
    if transition_sessions:
        sessions = len(transition_sessions)
    else:
        session_end = None
        for start, end in active_intervals:
            if (
                session_end is None
                or (start - session_end).total_seconds() > _SESSION_GAP_SECONDS
            ):
                sessions += 1
            if session_end is None or end > session_end:
                session_end = end

    energy_kwh = daily_meter_kwh if daily_meter_kwh is not None else measured_total
    has_any_data = daily_meter_kwh is not None or measured_slots > 0
    history_quality = (
        "no_data" if not has_any_data
        else "partial" if missing_slots or not settlements
        else "complete"
    )
    cost_quality = (
        "no_data" if not has_any_data
        else "partial" if missing_slots or active_cost_slots < active_slots
        else "no_ev_charge" if active_slots == 0
        else "complete"
    )
    return {
        "ev_charge_kwh": round(energy_kwh, 3) if has_any_data else None,
        "ev_energy_source": "daily_meter" if daily_meter_kwh is not None else (
            "settlement_sum" if measured_slots else None
        ),
        "ev_grid_import_kwh_attributed": (
            round(grid_total, 3) if grid_slots else None
        ),
        "ev_non_grid_kwh_attributed": (
            round(non_grid_total, 3) if non_grid_slots else None
        ),
        "ev_grid_cost_eur_attributed": round(cost_total, 4) if cost_slots else None,
        "ev_cost_attribution_quality": cost_quality,
        "ev_active_slots": active_slots,
        "ev_sessions": sessions,
        "ev_session_timing_source": (
            "abb_power_transitions" if transition_sessions
            else "settlement_intervals" if active_intervals
            else None
        ),
        "ev_measured_slots": measured_slots,
        "ev_missing_slots": missing_slots,
        "ev_history_quality": history_quality,
    }
