"""Pure planning and durable state for deadline-based EV smart charging.

This module deliberately has no Tesla, MQTT, optimizer, or notification side
effects.  It turns one persistent charge-by job and quarter-hour cost inputs into
a JSON-serialisable requested load profile.  The requested power is a ceiling:
site equipment such as Maxem remains free to reduce actual EVSE delivery, while
the coordinator can replan from measured SoC and energy on its next cycle.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence


SCHEMA_VERSION = 1
SLOT_MINUTES = 15
SLOT_SECONDS = SLOT_MINUTES * 60
DEFAULT_USABLE_CAPACITY_KWH = 100.0
DEFAULT_CHARGE_EFFICIENCY = 0.90
DEFAULT_REQUESTED_CEILING_KW = 16.0
DEFAULT_COMPLETION_BUFFER_MINUTES = 30.0
DEFAULT_BLOCK_START_PENALTY_EUR = 0.02
DAILY_PACING_MIN_HORIZON_HOURS = 48.0
# Forecast headroom is inherently approximate; a 0.25 kWh optimization quantum
# bounds seven-day dynamic-programming state while never exceeding a slot cap.
PLANNING_ENERGY_QUANTUM_KWH = 0.25
DEFAULT_UNKNOWN_PRICE_EUR_PER_KWH = 0.30
DEFAULT_JOB_PATH = Path("data/ev_charge_job.json")
DEFAULT_PLAN_PATH = Path("/dev/shm/cerbo_ev_charge_plan.json")
ACTIVE_JOB_STATUSES = frozenset({"active", "paused"})

_LOCK = threading.RLock()
_EPSILON = 1e-9


def _aware_datetime(value, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid ISO-8601 datetime") from exc
    else:
        raise TypeError(f"{field_name} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


def _number(value, field_name: str, *, minimum=None, maximum=None, positive=False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"{field_name} must be positive")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return parsed


def _soc(value, field_name: str) -> float:
    try:
        return _number(value, field_name, minimum=0.0, maximum=100.0)
    except ValueError as exc:
        raise ValueError(f"{field_name} SoC must be between 0 and 100") from exc


def _target_soc(value) -> float:
    try:
        return _number(value, "target_soc", minimum=50.0, maximum=100.0)
    except ValueError as exc:
        raise ValueError("target_soc SoC must be between 50 and 100") from exc


def _elapsed_add(value: datetime, *, seconds: float) -> datetime:
    """Add elapsed time without manufacturing or stretching a DST hour."""
    return datetime.fromtimestamp(value.timestamp() + seconds, tz=value.tzinfo)


def _floor_quarter(value: datetime) -> datetime:
    timestamp = math.floor(value.timestamp() / SLOT_SECONDS) * SLOT_SECONDS
    return datetime.fromtimestamp(timestamp, tz=value.tzinfo)


def _ceil_quarter(value: datetime) -> datetime:
    timestamp = math.ceil(value.timestamp() / SLOT_SECONDS) * SLOT_SECONDS
    return datetime.fromtimestamp(timestamp, tz=value.tzinfo)


@dataclass(frozen=True)
class EVChargeJob:
    """Validated, persistent representation of the one active charge-by job."""

    id: str
    current_soc: float
    target_soc: float
    ready_by: datetime
    created_at: datetime
    updated_at: datetime
    status: str = "active"

    @classmethod
    def from_payload(cls, payload: Mapping) -> "EVChargeJob":
        if not isinstance(payload, Mapping):
            raise TypeError("job must be a mapping")
        job_id = str(payload.get("id") or "").strip()
        if not job_id:
            raise ValueError("job id must be non-empty")
        status = str(payload.get("status") or "").strip().lower()
        if status not in ACTIVE_JOB_STATUSES:
            raise ValueError("job status must be active or paused")
        created_at = _aware_datetime(payload.get("created_at"), "created_at")
        updated_at = _aware_datetime(payload.get("updated_at"), "updated_at")
        ready_by = _aware_datetime(payload.get("ready_by"), "ready_by")
        if updated_at.timestamp() < created_at.timestamp():
            raise ValueError("updated_at cannot precede created_at")
        if ready_by.timestamp() <= created_at.timestamp():
            raise ValueError("ready_by must be later than created_at")
        return cls(
            id=job_id,
            current_soc=_soc(payload.get("current_soc"), "current_soc"),
            target_soc=_target_soc(payload.get("target_soc")),
            ready_by=ready_by,
            created_at=created_at,
            updated_at=updated_at,
            status=status,
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "ev_charge_job",
            "id": self.id,
            "current_soc": self.current_soc,
            "target_soc": self.target_soc,
            "ready_by": self.ready_by.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status": self.status,
        }


def create_job(
    *,
    current_soc,
    target_soc,
    ready_by,
    now=None,
    job_id=None,
) -> dict:
    """Create a validated JSON-ready charge job without persisting it."""
    created = _aware_datetime(now or datetime.now().astimezone(), "now")
    deadline = _aware_datetime(ready_by, "ready_by")
    if deadline.timestamp() <= created.timestamp():
        raise ValueError("ready_by must be in the future")
    job = EVChargeJob(
        id=str(job_id or uuid.uuid4()),
        current_soc=_soc(current_soc, "current_soc"),
        target_soc=_target_soc(target_soc),
        ready_by=deadline,
        created_at=created,
        updated_at=created,
    )
    return job.to_dict()


def _atomic_json_write(path: Path, payload: Mapping) -> None:
    """Publish one complete JSON document with write/fsync/rename semantics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The file itself is safely published even on filesystems that do
            # not permit fsync on a directory handle.
            pass
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def save_job(payload, *, path=None) -> dict:
    """Validate and atomically replace the one active persisted job."""
    job = EVChargeJob.from_payload(
        payload.to_dict() if isinstance(payload, EVChargeJob) else payload)
    serialised = job.to_dict()
    with _LOCK:
        _atomic_json_write(Path(path) if path is not None else DEFAULT_JOB_PATH, serialised)
    return serialised


def load_job(path=None) -> dict | None:
    """Load the active job, failing closed for missing or malformed state."""
    target = Path(path) if path is not None else DEFAULT_JOB_PATH
    with _LOCK:
        try:
            with target.open(encoding="utf-8") as stream:
                payload = json.load(stream)
            return EVChargeJob.from_payload(payload).to_dict()
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return None


def delete_job(path=None) -> bool:
    """Delete the one active job.  Returns whether a job file existed."""
    target = Path(path) if path is not None else DEFAULT_JOB_PATH
    with _LOCK:
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False


clear_job = delete_job


def update_job_status(action: str, *, path=None, now=None) -> dict:
    """Persist an explicit pause or resume action for the active job."""
    normalised = str(action or "").strip().lower()
    statuses = {"pause": "paused", "resume": "active"}
    if normalised not in statuses:
        raise ValueError("action must be pause or resume")
    with _LOCK:
        payload = load_job(path=path)
        if payload is None:
            raise FileNotFoundError("no active EV charge job")
        job = EVChargeJob.from_payload(payload)
        changed_at = _aware_datetime(now or datetime.now().astimezone(), "now")
        if changed_at.timestamp() < job.updated_at.timestamp():
            raise ValueError("now cannot precede the last job update")
        updated = replace(job, status=statuses[normalised], updated_at=changed_at)
        return save_job(updated, path=path)


def save_plan_snapshot(plan: Mapping, *, path=None) -> dict:
    """Atomically publish a JSON-ready plan for UI and execution consumers."""
    if not isinstance(plan, Mapping):
        raise TypeError("plan must be a mapping")
    payload = dict(plan)
    with _LOCK:
        _atomic_json_write(Path(path) if path is not None else DEFAULT_PLAN_PATH, payload)
    return payload


def load_plan_snapshot(path=None) -> dict | None:
    """Read the latest complete plan snapshot, or ``None`` if unavailable."""
    target = Path(path) if path is not None else DEFAULT_PLAN_PATH
    with _LOCK:
        try:
            with target.open(encoding="utf-8") as stream:
                payload = json.load(stream)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
    return payload if isinstance(payload, dict) else None


def _normalise_slots(slots: Sequence[Mapping], provisional_price: float,
                     global_slot_capacity_kwh: float) -> list[dict]:
    parsed = []
    seen = set()
    for index, raw in enumerate(slots or ()):
        if not isinstance(raw, Mapping):
            raise TypeError(f"slots[{index}] must be a mapping")
        if "start" not in raw:
            raise ValueError(f"slots[{index}] is missing 'start'")
        start = _aware_datetime(raw["start"], f"slots[{index}].start")
        if start.minute % SLOT_MINUTES or start.second or start.microsecond:
            raise ValueError("EV charge slots must align to quarter-hour boundaries")
        key = round(start.timestamp())
        if key in seen:
            raise ValueError(f"duplicate EV charge slot at {start.isoformat()}")
        seen.add(key)

        price_raw = raw.get("grid_price_eur_per_kwh", raw.get("total"))
        grid_price = None if price_raw is None else _number(
            price_raw, f"slots[{index}].grid_price_eur_per_kwh")
        pv_surplus = _number(
            raw.get("pv_surplus_kwh", 0.0),
            f"slots[{index}].pv_surplus_kwh",
            minimum=0.0,
        )
        opportunity_raw = raw.get("pv_opportunity_cost_eur_per_kwh")
        opportunity = None if opportunity_raw is None else _number(
            opportunity_raw,
            f"slots[{index}].pv_opportunity_cost_eur_per_kwh",
        )
        safe_capacity = global_slot_capacity_kwh
        if raw.get("expected_delivery_kw") is not None:
            slot_power = _number(
                raw["expected_delivery_kw"],
                f"slots[{index}].expected_delivery_kw",
                minimum=0.0,
            )
            safe_capacity = min(
                safe_capacity, slot_power * SLOT_MINUTES / 60.0)
        if raw.get("max_energy_kwh") is not None:
            safe_capacity = min(
                safe_capacity,
                _number(
                    raw["max_energy_kwh"],
                    f"slots[{index}].max_energy_kwh",
                    minimum=0.0,
                ),
            )
        parsed.append({
            "start_dt": start,
            "end_dt": _elapsed_add(start, seconds=SLOT_SECONDS),
            "grid_price": grid_price,
            "pv_surplus_kwh": pv_surplus,
            "pv_opportunity_cost": opportunity,
            "provisional_price": provisional_price,
            "safe_capacity_kwh": max(0.0, safe_capacity),
            "planning_capacity_kwh": max(0.0, safe_capacity),
            # Price and supply horizons are independent. A future quarter may
            # have a provisional grid price while its PV/source is not forecast
            # yet; never present that uncertainty as a confirmed grid choice.
            "supply_forecast_known": bool(raw.get("supply_forecast_known", True)),
        })
    parsed.sort(key=lambda item: item["start_dt"].timestamp())
    return parsed


def _slot_cost(slot: dict, energy_kwh: float) -> dict:
    pv_kwh = min(slot["pv_surplus_kwh"], energy_kwh)
    grid_kwh = max(0.0, energy_kwh - pv_kwh)
    opportunity = slot["pv_opportunity_cost"]
    if opportunity is None:
        opportunity = slot["grid_price"]
    tentative = (
        not slot.get("supply_forecast_known", True)
        or
        (pv_kwh > _EPSILON and opportunity is None)
        or (grid_kwh > _EPSILON and slot["grid_price"] is None)
    )
    provisional_pv_price = (
        opportunity if opportunity is not None else slot["provisional_price"])
    provisional_grid_price = (
        slot["grid_price"]
        if slot["grid_price"] is not None
        else slot["provisional_price"]
    )
    provisional_cost = pv_kwh * provisional_pv_price + grid_kwh * provisional_grid_price
    known_cost = None if tentative else provisional_cost
    if not slot.get("supply_forecast_known", True):
        supply = "pending"
    elif pv_kwh >= energy_kwh - _EPSILON:
        supply = "solar"
    elif pv_kwh > _EPSILON:
        supply = "mixed"
    else:
        supply = "grid"
    return {
        "pv_kwh": pv_kwh,
        "grid_kwh": grid_kwh,
        "tentative": tentative,
        "estimated_cost": known_cost,
        "provisional_cost": provisional_cost,
        "supply": supply,
    }


def _allocation_starts(selected) -> int:
    starts = 0
    previous_end = None
    for slot, _energy, _costing in sorted(
            selected, key=lambda item: item[0]["start_dt"].timestamp()):
        start = slot["start_dt"].timestamp()
        if previous_end is None or abs(start - previous_end) > _EPSILON:
            starts += 1
        previous_end = slot["end_dt"].timestamp()
    return starts


def _allocate(candidates: list[dict], required_kwh: float,
              block_start_penalty_eur: float):
    """Select exact energy under variable per-slot capacities.

    Energy state is quantized for bounded Pi-class runtime, but each path retains
    its exact energy and every selected slot uses the unrounded safe cap. The
    final slot carries the exact residual, so accounting never over- or under-
    charges the target and there is at most one partial-power tail.
    """
    usable = [slot for slot in candidates if slot["planning_capacity_kwh"] > _EPSILON]
    total_capacity = sum(slot["planning_capacity_kwh"] for slot in usable)
    if required_kwh >= total_capacity - _EPSILON:
        selected = [
            (slot, slot["planning_capacity_kwh"],
             _slot_cost(slot, slot["planning_capacity_kwh"]))
            for slot in usable
        ]
        return selected, max(0.0, required_kwh - total_capacity)

    required_units = int(math.ceil(
        (required_kwh - _EPSILON) / PLANNING_ENERGY_QUANTUM_KWH))
    # state=(energy_units, previous_selected). A completed state cannot select
    # later slots, making the exact residual the chronological block tail.
    # A compact integer bitmask retains the chosen slots for deterministic
    # reconstruction without copying a growing Python path in every DP state.
    states = {(0, False): (0.0, 0.0, 0, 0, 0.0)}

    def retain_best(target, state, candidate):
        existing = target.get(state)
        candidate_key = (candidate[0], candidate[1], candidate[2], -candidate[3])
        existing_key = (
            (existing[0], existing[1], existing[2], -existing[3])
            if existing is not None else None
        )
        if existing is None or candidate_key < existing_key:
            target[state] = candidate

    previous_slot = None
    candidate_count = len(candidates)
    for slot_index, slot in enumerate(candidates):
        contiguous = bool(
            previous_slot is not None
            and abs(previous_slot["end_dt"].timestamp()
                    - slot["start_dt"].timestamp()) <= _EPSILON
        )
        next_states = {}
        for (used_units, previous_selected), value in states.items():
            objective, energy_cost, starts, selected_mask, exact_used = value
            retain_best(
                next_states,
                (used_units, False),
                (objective, energy_cost, starts, selected_mask, exact_used),
            )
            if used_units >= required_units or slot["planning_capacity_kwh"] <= _EPSILON:
                continue
            remaining_exact = required_kwh - exact_used
            energy = min(slot["planning_capacity_kwh"], remaining_exact)
            if energy <= _EPSILON:
                continue
            new_exact = min(required_kwh, exact_used + energy)
            new_units = (
                required_units
                if new_exact >= required_kwh - _EPSILON
                else int(math.floor(
                    (new_exact + _EPSILON) / PLANNING_ENERGY_QUANTUM_KWH))
            )
            costing = _slot_cost(slot, energy)
            new_start = not previous_selected or not contiguous
            added_penalty = block_start_penalty_eur if new_start else 0.0
            cost = costing["provisional_cost"]
            bit = 1 << (candidate_count - slot_index - 1)
            retain_best(
                next_states,
                (new_units, True),
                (
                    objective + cost + added_penalty,
                    energy_cost + cost,
                    starts + int(new_start),
                    selected_mask | bit,
                    new_exact,
                ),
            )
        states = next_states
        previous_slot = slot

    finalists = [
        value for (used_units, _previous), value in states.items()
        if used_units == required_units
    ]
    if not finalists:
        # Very small (< half-quantum) headroom can be useful only when many
        # consecutive slots accumulate. Coarse optimization states may merge
        # those paths; retain exact feasibility with a deterministic contiguous
        # earliest fallback rather than failing or overstating capacity.
        selected = []
        remaining = required_kwh
        for slot in usable:
            energy = min(slot["planning_capacity_kwh"], remaining)
            selected.append((slot, energy, _slot_cost(slot, energy)))
            remaining -= energy
            if remaining <= _EPSILON:
                break
        return selected, max(0.0, remaining)
    finalists.sort(key=lambda value: (value[0], value[1], value[2], -value[3]))
    selected_mask = finalists[0][3]
    selected = []
    remaining = required_kwh
    for slot_index, slot in enumerate(candidates):
        bit = 1 << (candidate_count - slot_index - 1)
        if not selected_mask & bit:
            continue
        energy = min(slot["planning_capacity_kwh"], remaining)
        selected.append((slot, energy, _slot_cost(slot, energy)))
        remaining -= energy
        if remaining <= _EPSILON:
            break
    return selected, 0.0


def _allocate_daily_baseline(candidates: list[dict], required_kwh: float,
                             block_start_penalty_eur: float, local_timezone):
    """Return feasibility-protected even daily progress without solar pull-forward."""
    by_date = {}
    for slot in candidates:
        local_date = slot["start_dt"].astimezone(local_timezone).date()
        by_date.setdefault(local_date, []).append(slot)
    days = list(by_date.values())
    capacities = [
        sum(slot["planning_capacity_kwh"] for slot in day)
        for day in days
    ]
    selected = []
    remaining = required_kwh
    for index, day in enumerate(days):
        if remaining <= _EPSILON:
            break
        even_share = remaining / (len(days) - index)
        deadline_share = max(0.0, remaining - sum(capacities[index + 1:]))
        target = min(capacities[index], max(even_share, deadline_share))
        day_selected, _ = _allocate(day, target, block_start_penalty_eur)
        selected.extend(day_selected)
        remaining -= sum(energy for _slot, energy, _costing in day_selected)
    return selected, max(0.0, remaining)


def _allocate_daily_paced(candidates: list[dict], required_kwh: float,
                          block_start_penalty_eur: float, local_timezone):
    """Spread a long-horizon job across local dates, then optimise within each date.

    The equal daily share is raised when necessary to protect feasibility. Cheap
    forecast solar may additionally pull energy forward from later grid days,
    but only when its export opportunity cost (including a conservative new-block
    penalty) does not exceed the future planned energy it displaces. This preserves
    gentle progress without wasting valuable exports or weakening the deadline.
    """
    by_date = {}
    for slot in candidates:
        local_date = slot["start_dt"].astimezone(local_timezone).date()
        by_date.setdefault(local_date, []).append(slot)
    days = list(by_date.values())
    capacities = [
        sum(slot["planning_capacity_kwh"] for slot in day)
        for day in days
    ]
    selected = []
    remaining = required_kwh
    for index, day in enumerate(days):
        if remaining <= _EPSILON:
            break
        remaining_days = len(days) - index
        even_share = remaining / remaining_days
        future_capacity = sum(capacities[index + 1:])
        deadline_protecting_share = max(0.0, remaining - future_capacity)
        day_target = min(
            capacities[index],
            max(even_share, deadline_protecting_share),
        )
        if day_target <= _EPSILON:
            continue
        day_selected, _day_shortfall = _allocate(
            day, day_target, block_start_penalty_eur)
        delivered = sum(energy for _slot, energy, _costing in day_selected)

        # Once today's protected share is covered, use additional *known* solar
        # only when it is no dearer than the best remaining future alternative.
        # The baseline allocation consumes the PV portion of a slot first, so
        # only unconsumed PV is eligible here; grid capacity can never be pulled
        # forward by this bonus pass.
        future = [slot for future_day in days[index + 1:] for slot in future_day]
        future_required = max(0.0, remaining - delivered)
        future_selected, _ = _allocate_daily_baseline(
            future, future_required, block_start_penalty_eur, local_timezone)
        replacement_segments = []
        for slot, energy, costing in future_selected:
            if costing["pv_kwh"] > _EPSILON:
                pv_price = slot["pv_opportunity_cost"]
                if pv_price is None:
                    pv_price = slot["provisional_price"]
                replacement_segments.append([pv_price, costing["pv_kwh"]])
            if costing["grid_kwh"] > _EPSILON:
                grid_price = slot["grid_price"]
                if grid_price is None:
                    grid_price = slot["provisional_price"]
                replacement_segments.append([grid_price, costing["grid_kwh"]])
        replacement_segments.sort(key=lambda item: item[0], reverse=True)
        used_pv = {
            round(slot["start_dt"].timestamp()): costing["pv_kwh"]
            for slot, _energy, costing in day_selected
        }
        bonus_candidates = []
        for slot in day:
            if not slot.get("supply_forecast_known", True):
                continue
            remaining_pv = max(
                0.0,
                min(slot["pv_surplus_kwh"], slot["planning_capacity_kwh"])
                - used_pv.get(round(slot["start_dt"].timestamp()), 0.0),
            )
            if remaining_pv <= _EPSILON or slot["pv_opportunity_cost"] is None:
                continue
            conservative_unit_cost = (
                slot["pv_opportunity_cost"]
                + block_start_penalty_eur / remaining_pv
            )
            bonus_candidates.append((conservative_unit_cost, slot, remaining_pv))
        bonus_candidates.sort(
            key=lambda item: (item[0], item[1]["start_dt"].timestamp()))
        bonus_limit = max(0.0, remaining - delivered)
        bonus_selected = []
        if bonus_candidates and bonus_limit > _EPSILON and replacement_segments:
            for solar_cost, slot, solar_capacity in bonus_candidates:
                solar_remaining = min(solar_capacity, bonus_limit)
                taken = 0.0
                while solar_remaining > _EPSILON and replacement_segments:
                    replacement_cost, replacement_energy = replacement_segments[0]
                    if solar_cost > replacement_cost + _EPSILON:
                        break
                    amount = min(solar_remaining, replacement_energy)
                    taken += amount
                    solar_remaining -= amount
                    bonus_limit -= amount
                    replacement_energy -= amount
                    if replacement_energy <= _EPSILON:
                        replacement_segments.pop(0)
                    else:
                        replacement_segments[0][1] = replacement_energy
                if taken > _EPSILON:
                    bonus_selected.append((slot, taken))
                if bonus_limit <= _EPSILON:
                    break
        if bonus_selected:
            originals = {
                round(slot["start_dt"].timestamp()): slot for slot in day
            }
            combined = {
                round(slot["start_dt"].timestamp()): [slot, energy]
                for slot, energy, _costing in day_selected
            }
            for slot, energy in bonus_selected:
                key = round(slot["start_dt"].timestamp())
                original = originals[key]
                if key in combined:
                    combined[key][1] += energy
                else:
                    combined[key] = [original, energy]
            day_selected = [
                (slot, energy, _slot_cost(slot, energy))
                for slot, energy in sorted(
                    combined.values(), key=lambda item: item[0]["start_dt"].timestamp())
            ]
            delivered = sum(energy for _slot, energy, _costing in day_selected)
        selected.extend(day_selected)
        remaining -= delivered
    return selected, max(0.0, remaining)


def _serialise_slots(selected):
    result = []
    for slot, energy, costing in selected:
        requested_power = energy / (SLOT_MINUTES / 60.0)
        known_unit_cost = (
            costing["estimated_cost"] / energy
            if costing["estimated_cost"] is not None else None
        )
        provisional_unit_cost = costing["provisional_cost"] / energy
        result.append({
            "start": slot["start_dt"].isoformat(),
            "end": slot["end_dt"].isoformat(),
            "energy_kwh": round(energy, 6),
            "planned_ev_kwh": round(energy, 6),
            "requested_power_kw": round(requested_power, 6),
            "safe_energy_cap_kwh": round(slot["planning_capacity_kwh"], 6),
            "safe_power_cap_kw": round(
                slot["planning_capacity_kwh"] / (SLOT_MINUTES / 60.0), 6),
            "pv_energy_kwh": round(costing["pv_kwh"], 6),
            "grid_energy_kwh": round(costing["grid_kwh"], 6),
            "grid_price_eur_per_kwh": slot["grid_price"],
            "pv_opportunity_cost_eur_per_kwh": slot["pv_opportunity_cost"],
            "effective_cost_eur_per_kwh": (
                round(known_unit_cost, 6) if known_unit_cost is not None else None
            ),
            "price_eur_kwh": (
                round(known_unit_cost, 6) if known_unit_cost is not None else None
            ),
            "provisional_cost_eur_per_kwh": round(provisional_unit_cost, 6),
            "supply": costing["supply"],
            "supply_forecast_known": slot.get("supply_forecast_known", True),
            "tentative": costing["tentative"],
            "price_known": not costing["tentative"],
            "estimated_cost_eur": (
                round(costing["estimated_cost"], 6)
                if costing["estimated_cost"] is not None else None
            ),
            "provisional_cost_eur": round(costing["provisional_cost"], 6),
        })
    return result


def _compress_blocks(slots: list[dict]) -> list[dict]:
    blocks = []
    for slot in slots:
        merge = bool(
            blocks
            and blocks[-1]["end"] == slot["start"]
            and blocks[-1]["requested_power_kw"] == slot["requested_power_kw"]
        )
        if not merge:
            blocks.append({
                "start": slot["start"],
                "end": slot["end"],
                "requested_power_kw": slot["requested_power_kw"],
                "energy_kwh": slot["energy_kwh"],
                "pv_energy_kwh": slot["pv_energy_kwh"],
                "grid_energy_kwh": slot["grid_energy_kwh"],
                "supply": slot["supply"],
                "tentative": slot["tentative"],
                "estimated_cost_eur": slot["estimated_cost_eur"],
                "provisional_cost_eur": slot["provisional_cost_eur"],
                "soc_start": slot.get("soc_start"),
                "soc_end": slot.get("soc_end"),
            })
            continue
        block = blocks[-1]
        block["end"] = slot["end"]
        block["soc_end"] = slot.get("soc_end")
        block["tentative"] = block["tentative"] or slot["tentative"]
        if block["supply"] != slot["supply"]:
            block["supply"] = "mixed"
        for field in ("energy_kwh", "pv_energy_kwh", "grid_energy_kwh",
                      "provisional_cost_eur"):
            block[field] = round(block[field] + slot[field], 6)
        if block["estimated_cost_eur"] is None or slot["estimated_cost_eur"] is None:
            block["estimated_cost_eur"] = None
        else:
            block["estimated_cost_eur"] = round(
                block["estimated_cost_eur"] + slot["estimated_cost_eur"], 6)
    return blocks


def _build_daily_plan(slots: list[dict], local_timezone) -> list[dict]:
    """Build the small, human-facing day summaries used by the Vehicle tab."""
    grouped = {}
    for slot in slots:
        start = _aware_datetime(slot["start"], "slot.start").astimezone(local_timezone)
        grouped.setdefault(start.date().isoformat(), []).append(slot)
    days = []
    for date, day_slots in grouped.items():
        blocks = _compress_blocks(day_slots)
        windows = []
        for block in blocks:
            if windows and windows[-1]["end"] == block["start"]:
                window = windows[-1]
                window["end"] = block["end"]
                window["soc_end"] = block.get("soc_end")
                window["energy_kwh"] = round(
                    window["energy_kwh"] + block["energy_kwh"], 6)
                window["tentative"] = window["tentative"] or block["tentative"]
            else:
                windows.append({
                    "start": block["start"],
                    "end": block["end"],
                    "energy_kwh": block["energy_kwh"],
                    "soc_start": block.get("soc_start"),
                    "soc_end": block.get("soc_end"),
                    "tentative": block["tentative"],
                })
        energy = sum(slot["energy_kwh"] for slot in day_slots)
        pv_energy = sum(slot["pv_energy_kwh"] for slot in day_slots)
        pending_energy = sum(
            slot["energy_kwh"] for slot in day_slots
            if slot.get("supply") == "pending"
        )
        grid_energy = sum(
            slot["grid_energy_kwh"] for slot in day_slots
            if slot.get("supply") != "pending"
        )
        estimated_costs = [slot["estimated_cost_eur"] for slot in day_slots]
        provisional_cost = sum(slot["provisional_cost_eur"] for slot in day_slots)
        supplies = {slot["supply"] for slot in day_slots}
        estimated_cost = (
            None if any(value is None for value in estimated_costs)
            else sum(estimated_costs)
        )
        days.append({
            "date": date,
            "start": day_slots[0]["start"],
            "end": day_slots[-1]["end"],
            "energy_kwh": round(energy, 6),
            "pv_energy_kwh": round(pv_energy, 6),
            "grid_energy_kwh": round(max(0.0, grid_energy), 6),
            "source_pending_kwh": round(pending_energy, 6),
            "soc_start": day_slots[0].get("soc_start"),
            "soc_end": day_slots[-1].get("soc_end"),
            "supply": next(iter(supplies)) if len(supplies) == 1 else "mixed",
            "tentative": any(slot["tentative"] for slot in day_slots),
            "estimated_cost_eur": (
                round(estimated_cost, 6) if estimated_cost is not None else None
            ),
            "provisional_cost_eur": round(provisional_cost, 6),
            "average_price_eur_per_kwh": (
                round(estimated_cost / energy, 6)
                if estimated_cost is not None and energy > _EPSILON else None
            ),
            "blocks": blocks,
            "windows": windows,
        })
    return days


def _build_timeline_slots(candidates, selected_slots, initial_soc,
                          target_soc, efficiency, capacity_kwh):
    """Return every eligible quarter for price context in the Vehicle timeline."""
    selected_by_timestamp = {
        round(_aware_datetime(slot["start"], "slot.start").timestamp()): slot
        for slot in selected_slots
    }
    timeline = []
    running_soc = initial_soc
    for candidate in candidates:
        key = round(candidate["start_dt"].timestamp())
        selected = selected_by_timestamp.get(key)
        if selected is not None:
            item = dict(selected)
            item["selected"] = True
            item["soc_start"] = round(running_soc, 3)
            running_soc = min(
                target_soc,
                running_soc + item["energy_kwh"] * efficiency / capacity_kwh * 100.0,
            )
            item["soc_end"] = round(running_soc, 3)
        else:
            slot_capacity = candidate["planning_capacity_kwh"]
            costing = _slot_cost(candidate, slot_capacity)
            known_unit = (
                costing["estimated_cost"] / slot_capacity
                if costing["estimated_cost"] is not None and slot_capacity > _EPSILON
                else candidate["grid_price"]
            )
            provisional_unit = (
                costing["provisional_cost"] / slot_capacity
                if slot_capacity > _EPSILON else candidate["provisional_price"]
            )
            timeline_tentative = (
                costing["tentative"]
                if slot_capacity > _EPSILON else candidate["grid_price"] is None
            )
            item = {
                "start": candidate["start_dt"].isoformat(),
                "end": candidate["end_dt"].isoformat(),
                "selected": False,
                "energy_kwh": 0.0,
                "planned_ev_kwh": 0.0,
                "requested_power_kw": 0.0,
                "safe_energy_cap_kwh": round(slot_capacity, 6),
                "safe_power_cap_kw": round(
                    slot_capacity / (SLOT_MINUTES / 60.0), 6),
                "grid_price_eur_per_kwh": candidate["grid_price"],
                "pv_opportunity_cost_eur_per_kwh": candidate["pv_opportunity_cost"],
                "effective_cost_eur_per_kwh": (
                    round(known_unit, 6) if known_unit is not None else None
                ),
                "price_eur_kwh": (
                    round(known_unit, 6) if known_unit is not None else None
                ),
                "provisional_cost_eur_per_kwh": round(
                    provisional_unit, 6),
                "supply": costing["supply"] if slot_capacity > _EPSILON else "unavailable",
                "supply_forecast_known": candidate.get("supply_forecast_known", True),
                "tentative": timeline_tentative,
                "price_known": not timeline_tentative,
                "soc_start": round(running_soc, 3),
                "soc_end": round(running_soc, 3),
            }
        timeline.append(item)
    # Keep the selected collection and timeline numerically identical so the
    # executor, optimizer overlay, and UI never show different SoC trajectories.
    timeline_by_timestamp = {
        round(_aware_datetime(item["start"], "slot.start").timestamp()): item
        for item in timeline if item["selected"]
    }
    for selected in selected_slots:
        item = timeline_by_timestamp[
            round(_aware_datetime(selected["start"], "slot.start").timestamp())]
        selected["soc_start"] = item["soc_start"]
        selected["soc_end"] = item["soc_end"]
    return timeline


def _sum_cost(selected, field):
    values = [costing[field] for _slot, _energy, costing in selected]
    if field == "estimated_cost" and any(value is None for value in values):
        return None
    return round(sum(values), 6)


def _latest_capacity_safe_start(candidates, required_kwh, fallback_start):
    """Accumulate safe slot capacity backward to find the latest viable start."""
    if required_kwh <= _EPSILON:
        return fallback_start
    remaining = required_kwh
    latest = fallback_start
    for slot in reversed(candidates):
        capacity = slot["planning_capacity_kwh"]
        if capacity <= _EPSILON:
            continue
        latest = slot["start_dt"]
        remaining -= capacity
        if remaining <= _EPSILON:
            return latest
    # Infeasible horizons must advertise the earliest possible action, never a
    # theoretical later time based on power that the site cannot deliver.
    return next(
        (slot["start_dt"] for slot in candidates
         if slot["planning_capacity_kwh"] > _EPSILON),
        fallback_start,
    )


def _base_plan(now: datetime) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ev_charge_plan",
        "generated_at": now.isoformat(),
        "available": True,
        "active": False,
        "job": None,
        "active_job": False,
        "feasible": None,
        "status": "idle",
        "reason": "no_active_job",
        "tentative": False,
        "confidence": "none",
        "slots": [],
        "timeline_slots": [],
        "blocks": [],
        "daily_plan": [],
        "planning_strategy": None,
        "planned_ac_kwh": 0.0,
        "energy_shortfall_kwh": 0.0,
    }


def plan_charge(
    job,
    slots: Sequence[Mapping],
    *,
    current_soc=None,
    now=None,
    usable_capacity_kwh=DEFAULT_USABLE_CAPACITY_KWH,
    charge_efficiency=DEFAULT_CHARGE_EFFICIENCY,
    requested_ceiling_kw=DEFAULT_REQUESTED_CEILING_KW,
    conservative_delivery_kw=None,
    completion_buffer_minutes=DEFAULT_COMPLETION_BUFFER_MINUTES,
    block_start_penalty_eur=DEFAULT_BLOCK_START_PENALTY_EUR,
    unknown_price_eur_per_kwh=None,
) -> dict:
    """Build the minimum-cost quarter-hour EV load plan before a deadline.

    Prices may be unknown (``None``); those slots use a clearly identified
    provisional price for ranking and make the plan tentative.  PV energy is
    valued at its opportunity cost rather than treated as free.  The output
    requests a ceiling only and does not assume the EVSE will defeat external
    Maxem overload protection.
    """
    planned_at = _aware_datetime(now or datetime.now().astimezone(), "now")
    if job is None:
        return _base_plan(planned_at)
    model = EVChargeJob.from_payload(
        job.to_dict() if isinstance(job, EVChargeJob) else job)
    current = model.current_soc if current_soc is None else _soc(current_soc, "current_soc")
    capacity = _number(usable_capacity_kwh, "usable_capacity_kwh", positive=True)
    efficiency = _number(
        charge_efficiency, "charge_efficiency", positive=True, maximum=1.0)
    ceiling = _number(requested_ceiling_kw, "requested_ceiling_kw", positive=True)
    delivery = _number(
        conservative_delivery_kw if conservative_delivery_kw is not None else ceiling,
        "conservative_delivery_kw",
        positive=True,
    )
    if delivery > ceiling + _EPSILON:
        raise ValueError("conservative_delivery_kw cannot exceed requested_ceiling_kw")
    buffer_minutes = _number(
        completion_buffer_minutes, "completion_buffer_minutes", minimum=0.0)
    start_penalty = _number(
        block_start_penalty_eur, "block_start_penalty_eur", minimum=0.0)

    known_prices = []
    for raw in slots or ():
        if isinstance(raw, Mapping):
            price = raw.get("grid_price_eur_per_kwh", raw.get("total"))
            if price is not None:
                known_prices.append(_number(price, "grid_price_eur_per_kwh"))
    if unknown_price_eur_per_kwh is None:
        fallback_price = median(known_prices) if known_prices else DEFAULT_UNKNOWN_PRICE_EUR_PER_KWH
    else:
        fallback_price = _number(
            unknown_price_eur_per_kwh, "unknown_price_eur_per_kwh")
    global_slot_capacity = delivery * SLOT_MINUTES / 60.0
    normalised_slots = _normalise_slots(
        slots, fallback_price, global_slot_capacity)

    stored_required = max(0.0, (model.target_soc - current) / 100.0 * capacity)
    ac_required = stored_required / efficiency
    cutoff = _elapsed_add(model.ready_by, seconds=-buffer_minutes * 60.0)
    first_start = _ceil_quarter(planned_at)
    eligible = [
        slot for slot in normalised_slots
        if slot["start_dt"].timestamp() >= first_start.timestamp()
        and slot["end_dt"].timestamp() <= cutoff.timestamp() + _EPSILON
    ]
    latest_safe = _latest_capacity_safe_start(eligible, ac_required, first_start)

    result = _base_plan(planned_at)
    result.update({
        "job": {
            "id": model.id,
            "status": model.status,
        },
        "active": True,
        "active_job": True,
        "current_soc": current,
        "target_soc": model.target_soc,
        "ready_by": model.ready_by.isoformat(),
        "charge_cutoff": cutoff.isoformat(),
        "latest_safe_start": latest_safe.isoformat(),
        "plug_in_by": latest_safe.isoformat(),
        "completion_buffer_minutes": buffer_minutes,
        "block_start_penalty_eur": start_penalty,
        "usable_capacity_kwh": capacity,
        "charge_efficiency": efficiency,
        "requested_ceiling_kw": ceiling,
        "expected_delivery_kw": delivery,
        "required_stored_kwh": round(stored_required, 6),
        "required_ac_kwh": round(ac_required, 6),
        "unknown_price_assumption_eur_per_kwh": round(fallback_price, 6),
    })

    if model.status == "paused":
        result.update({
            "active": False,
            "status": "paused",
            "reason": "job_paused_by_user",
            "confidence": "none",
            "feasible": None,
            "energy_shortfall_kwh": round(ac_required, 6),
        })
        return result
    if ac_required <= _EPSILON:
        result.update({
            "active": False,
            "status": "completed",
            "reason": "target_soc_reached",
            "confidence": "high",
            "feasible": True,
            "expected_completion": planned_at.isoformat(),
            "planned_soc": current,
        })
        return result

    horizon_hours = max(
        0.0, (cutoff.timestamp() - first_start.timestamp()) / 3600.0)
    daily_paced = horizon_hours > DAILY_PACING_MIN_HORIZON_HOURS
    if daily_paced:
        selected, shortfall = _allocate_daily_paced(
            eligible, ac_required, start_penalty, model.ready_by.tzinfo)
    else:
        selected, shortfall = _allocate(eligible, ac_required, start_penalty)
    serialised_slots = _serialise_slots(selected)
    timeline_slots = _build_timeline_slots(
        eligible,
        serialised_slots,
        current,
        model.target_soc,
        efficiency,
        capacity,
    )
    blocks = _compress_blocks(serialised_slots)
    daily_plan = _build_daily_plan(serialised_slots, model.ready_by.tzinfo)
    known_cost = _sum_cost(selected, "estimated_cost")
    provisional_cost = _sum_cost(selected, "provisional_cost")
    tentative = any(slot["tentative"] for slot in serialised_slots)
    planned_energy = ac_required - shortfall
    forecast_pv_kwh = sum(slot["pv_energy_kwh"] for slot in serialised_slots)
    source_pending_kwh = sum(
        slot["energy_kwh"] for slot in serialised_slots
        if slot.get("supply") == "pending"
    )
    forecast_grid_kwh = sum(
        slot["grid_energy_kwh"] for slot in serialised_slots
        if slot.get("supply") != "pending"
    )
    charge_start_count = _allocation_starts(selected)
    optimization_penalty = charge_start_count * start_penalty

    # Counterfactual: fill chronologically at the same conservative rate.
    immediate_selected = []
    immediate_remaining = ac_required
    for slot in eligible:
        if immediate_remaining <= _EPSILON:
            break
        energy = min(slot["planning_capacity_kwh"], immediate_remaining)
        if energy <= _EPSILON:
            continue
        immediate_selected.append((slot, energy, _slot_cost(slot, energy)))
        immediate_remaining -= energy
    immediate_cost = (
        _sum_cost(immediate_selected, "estimated_cost")
        if immediate_remaining <= _EPSILON else None
    )
    immediate_provisional = (
        _sum_cost(immediate_selected, "provisional_cost")
        if immediate_remaining <= _EPSILON else None
    )
    saving = (
        round(immediate_cost - known_cost, 6)
        if immediate_cost is not None and known_cost is not None else None
    )
    provisional_saving = (
        round(immediate_provisional - provisional_cost, 6)
        if immediate_provisional is not None else None
    )
    expected_completion = blocks[-1]["end"] if blocks else None
    planned_soc = min(
        model.target_soc,
        current + planned_energy * efficiency / capacity * 100.0,
    )
    infeasible = shortfall > 1e-6
    result.update({
        "status": "infeasible" if infeasible else "planned",
        "reason": "insufficient_energy_capacity_before_deadline" if infeasible else (
            "daily_paced_tentative" if daily_paced and tentative else
            "daily_paced" if daily_paced else
            "price_optimised_tentative" if tentative else "price_optimised"
        ),
        "planning_strategy": (
            "daily_paced" if daily_paced else "deadline_optimised"
        ),
        "tentative": tentative,
        "confidence": "low" if tentative or infeasible else "high",
        "feasible": not infeasible,
        "slots": serialised_slots,
        "timeline_slots": timeline_slots,
        "blocks": blocks,
        "daily_plan": daily_plan,
        "planned_ac_kwh": round(planned_energy, 6),
        "forecast_pv_kwh": round(forecast_pv_kwh, 6),
        "forecast_grid_kwh": round(forecast_grid_kwh, 6),
        "source_pending_kwh": round(source_pending_kwh, 6),
        "energy_shortfall_kwh": round(shortfall, 6),
        "planned_soc": round(planned_soc, 3),
        "expected_completion": expected_completion,
        "estimated_incremental_cost_eur": known_cost,
        "provisional_incremental_cost_eur": provisional_cost,
        "charge_start_count": charge_start_count,
        "optimization_block_penalty_eur": round(optimization_penalty, 6),
        "optimization_score_eur": round(provisional_cost + optimization_penalty, 6),
        "charge_now_cost_eur": immediate_cost,
        "charge_now_provisional_cost_eur": immediate_provisional,
        "estimated_saving_eur": saving,
        "provisional_saving_eur": provisional_saving,
    })
    return result


plan_ev_charge = plan_charge


def overlay_load_forecast(forecast: Mapping, plan: Mapping | None) -> tuple[dict, dict]:
    """Add planned EV kWh to a copy of a datetime-keyed optimizer forecast."""
    result = dict(forecast or {})
    slots = plan.get("slots", []) if isinstance(plan, Mapping) else []
    energy_by_timestamp = {}
    for slot in slots:
        try:
            start = _aware_datetime(slot.get("start"), "slot.start")
            energy = _number(slot.get("energy_kwh"), "slot.energy_kwh", minimum=0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        energy_by_timestamp[round(start.timestamp())] = energy
    planned = 0.0
    for start in result:
        if not isinstance(start, datetime) or start.tzinfo is None or start.utcoffset() is None:
            continue
        energy = energy_by_timestamp.get(round(start.timestamp()), 0.0)
        if energy:
            result[start] = float(result.get(start) or 0.0) + energy
            planned += energy
    return result, {
        "planned_ev_kwh": round(planned, 6),
        "active_job": bool(plan and plan.get("active_job")),
    }


__all__ = [
    "DEFAULT_CHARGE_EFFICIENCY",
    "DEFAULT_BLOCK_START_PENALTY_EUR",
    "DEFAULT_COMPLETION_BUFFER_MINUTES",
    "DEFAULT_REQUESTED_CEILING_KW",
    "DEFAULT_USABLE_CAPACITY_KWH",
    "EVChargeJob",
    "clear_job",
    "create_job",
    "delete_job",
    "load_job",
    "load_plan_snapshot",
    "overlay_load_forecast",
    "plan_charge",
    "plan_ev_charge",
    "save_job",
    "save_plan_snapshot",
    "update_job_status",
]
