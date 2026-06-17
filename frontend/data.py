"""Read-only data access for the dashboard.

Sources:
  * The plan JSON published by the main service (AI_PLAN_EXPORT_PATH).
  * Configuration values read directly from .env (no MQTT/STATE side effects).

All presentation logic (hour grouping, day cost summary) lives here so the
templates/JS stay thin and this stays unit-testable.
"""
import os
import json
import time
from datetime import datetime

from dotenv import dotenv_values

from frontend.config_schema import CONFIG_SCHEMA

DEFAULT_PLAN_PATH = "/dev/shm/cerbo_ai_plan.json"


def _env():
    # Read fresh each call so config edits show up without a restart.
    return dotenv_values(".env")


def plan_path() -> str:
    return _env().get("AI_PLAN_EXPORT_PATH") or DEFAULT_PLAN_PATH


def _parse_time(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def is_idle(slot) -> bool:
    """True when a slot is IDLE (Victron-managed, neutral setpoint).

    IDLE flow (self-consumption, surplus PV) is not a commanded action — it's a
    projection that settles retroactively — so it's kept OUT of the committed net
    (Option A) and shown separately as projected.
    """
    return str(slot.get("control_action") or "").upper() == "IDLE"


def load_raw_plan() -> dict | None:
    """Return the parsed plan JSON, or None if it has not been published yet."""
    path = plan_path()
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def group_by_hour(schedule: list) -> list:
    """Group 15-minute slots into hour buckets with aggregates for the tree view.

    The first slot in the schedule is "now"; its slot and hour are flagged
    ``is_current`` so the UI can highlight and jump to it.
    """
    current_time = schedule[0].get("time") if schedule else None
    current_hour_key = None
    if current_time:
        cdt = _parse_time(current_time)
        if cdt is not None:
            current_hour_key = cdt.strftime("%Y-%m-%d %H")

    hours = {}
    order = []
    for slot in schedule:
        dt = _parse_time(slot.get("time"))
        if dt is None:
            continue
        slot["is_current"] = (slot.get("time") == current_time)
        key = dt.strftime("%Y-%m-%d %H")
        if key not in hours:
            hours[key] = {
                "key": key,
                "label": dt.strftime("%a %H:00"),
                "hour_start": dt.replace(minute=0, second=0, microsecond=0).isoformat(),
                "slots": [],
                "import_kwh": 0.0, "export_kwh": 0.0,
                "import_cost": 0.0, "export_rev": 0.0,
                "idle_imp_cost": 0.0, "idle_exp_rev": 0.0,
                "prices": [],
                "mode_counts": {},
            }
            order.append(key)
        h = hours[key]
        g = slot.get("grid_energy", 0.0) or 0.0
        buy = slot.get("price", 0.0) or 0.0
        sell = slot.get("sell", buy) or buy
        if is_idle(slot):
            # IDLE = projected only (Victron decides), kept out of committed net.
            if g > 0:
                h["idle_imp_cost"] += g * buy
            elif g < 0:
                h["idle_exp_rev"] += -g * sell
        elif g > 0:
            h["import_kwh"] += g
            h["import_cost"] += g * buy
        elif g < 0:
            h["export_kwh"] += -g
            h["export_rev"] += -g * sell
        h["prices"].append(buy)
        act = (slot.get("control_action") or "").upper()
        h["mode_counts"][act] = h["mode_counts"].get(act, 0) + 1
        h["slots"].append(slot)

    result = []
    for key in order:
        h = hours[key]
        n = len(h["prices"]) or 1
        modes = h["mode_counts"]
        dominant = max(modes, key=modes.get) if modes else ""
        result.append({
            "key": h["key"],
            "label": h["label"],
            "is_current": (h["key"] == current_hour_key),
            "dominant_action": dominant,
            "mixed": len(modes) > 1,
            "actions": sorted(modes.keys()),
            "avg_price": sum(h["prices"]) / n,
            "import_kwh": round(h["import_kwh"], 2),
            "export_kwh": round(h["export_kwh"], 2),
            "projected_idle_net": round(h["idle_exp_rev"] - h["idle_imp_cost"], 3),
            "net_kwh": round(h["import_kwh"] - h["export_kwh"], 2),
            "net_cost": round(h["import_cost"] - h["export_rev"], 3),
            "soc_start": h["slots"][0].get("soc_start"),
            "soc_end": h["slots"][-1].get("soc_end"),
            "slots": h["slots"],
        })
    return result


def day_summary(schedule: list, today_actuals: dict | None) -> dict:
    """Per-calendar-day cost forecast, folding in today's actuals."""
    days = {}
    order = []
    for slot in schedule:
        dt = _parse_time(slot.get("time"))
        if dt is None:
            continue
        d = dt.date().isoformat()
        if d not in days:
            days[d] = {"date": d, "label": dt.strftime("%a %d %b"),
                       "import_kwh": 0.0, "import_cost": 0.0,
                       "export_kwh": 0.0, "export_rev": 0.0,
                       "idle_imp_cost": 0.0, "idle_exp_rev": 0.0}
            order.append(d)
        g = slot.get("grid_energy", 0.0) or 0.0
        buy = slot.get("price", 0.0) or 0.0
        sell = slot.get("sell", buy) or buy
        if is_idle(slot):
            if g > 0:
                days[d]["idle_imp_cost"] += g * buy
            elif g < 0:
                days[d]["idle_exp_rev"] += -g * sell
        elif g > 0:
            days[d]["import_kwh"] += g
            days[d]["import_cost"] += g * buy
        elif g < 0:
            days[d]["export_kwh"] += -g
            days[d]["export_rev"] += -g * sell

    today = datetime.now().date().isoformat()
    rows = []
    _keys = ("import_kwh", "import_cost", "export_kwh", "export_rev",
             "idle_imp_cost", "idle_exp_rev")
    tot = {k: 0.0 for k in _keys}
    for d in order:
        row = days[d]
        forecast = {k: round(row[k], 3) for k in _keys}
        actual = None
        if d == today and today_actuals:
            # Actuals are realised grid flows only (no projected-idle bucket).
            actual = {
                "import_kwh": round(float(today_actuals.get("imp_kwh", 0) or 0), 3),
                "import_cost": round(float(today_actuals.get("imp_cost", 0) or 0), 3),
                "export_kwh": round(float(today_actuals.get("exp_kwh", 0) or 0), 3),
                "export_rev": round(float(today_actuals.get("exp_rev", 0) or 0), 3),
            }
        combined = dict(forecast)
        if actual:
            for k in combined:
                combined[k] = round(combined[k] + actual.get(k, 0.0), 3)
        for k in tot:
            tot[k] += combined[k]
        rows.append({
            "date": d, "label": row["label"], "is_today": d == today,
            "forecast": forecast, "actual": actual, "combined": combined,
            # Committed net (Option A): only BUY/RETAIN/SELL flows.
            "net": round(combined["import_cost"] - combined["export_rev"], 3),
            # IDLE projection (export rev − import cost), shown apart from net.
            "projected_idle_net": round(combined["idle_exp_rev"] - combined["idle_imp_cost"], 3),
        })

    return {
        "days": rows,
        "total": {**{k: round(v, 3) for k, v in tot.items()},
                  "net": round(tot["import_cost"] - tot["export_rev"], 3),
                  "projected_idle_net": round(tot["idle_exp_rev"] - tot["idle_imp_cost"], 3)},
    }


def get_plan() -> dict:
    """Return the processed plan for the API, including staleness info."""
    raw = load_raw_plan()
    if not raw:
        return {"available": False, "message": "No plan published yet. "
                "Is the AI optimizer enabled and has it run at least once?"}

    generated = _parse_time(raw.get("generated_at"))
    age_s = None
    if generated:
        try:
            age_s = max(0, int(time.time() - generated.timestamp()))
        except Exception:
            age_s = None

    schedule = raw.get("schedule", [])
    return {
        "available": True,
        "generated_at": raw.get("generated_at"),
        "age_seconds": age_s,
        "stale": (age_s is not None and age_s > 1800),  # >30 min old
        "battery_soc": raw.get("battery_soc"),
        "pv_remaining_wh": raw.get("pv_remaining_wh"),
        "pv_tomorrow_wh": raw.get("pv_tomorrow_wh"),
        "price_points": raw.get("price_points"),
        "slot_duration_h": raw.get("slot_duration_h"),
        "current": raw.get("current", {}),
        "victron_slots": raw.get("victron_slots", []),
        "hours": group_by_hour(schedule),
        "day_summary": day_summary(schedule, raw.get("today_actuals")),
    }


def get_config() -> list:
    """Return the config schema groups with current values from .env."""
    env = _env()
    groups = []
    for grp in CONFIG_SCHEMA:
        settings = []
        for s in grp["settings"]:
            settings.append({**s, "value": env.get(s["key"], "")})
        groups.append({"group": grp["group"], "settings": settings})
    return groups


def _schema_index():
    """Map key -> setting spec for validation of writes."""
    idx = {}
    for grp in CONFIG_SCHEMA:
        for s in grp["settings"]:
            idx[s["key"]] = s
    return idx


def _coerce_value(spec, raw):
    """Validate/normalise a value for a setting, returning the string to persist.

    Raises ValueError on invalid input.
    """
    t = spec.get("type", "str")
    raw = str(raw).strip()
    if t == "bool":
        low = raw.lower()
        if low in ("true", "1", "yes", "on"):
            return "True"
        if low in ("false", "0", "no", "off"):
            return "False"
        raise ValueError("expected true/false")
    if t == "int":
        return str(int(float(raw)))
    if t == "float":
        return str(float(raw))
    # str / enum
    if "options" in spec and raw not in spec["options"]:
        raise ValueError(f"must be one of {spec['options']}")
    return raw


def update_env_setting(key: str, value, env_path: str = ".env") -> dict:
    """Persist a single allow-listed setting to .env (durable source of truth).

    The main service's retrieve_setting() re-reads .env on every call (so the
    change applies on the next optimization/decision cycle) and republishes the
    Cerbomoticzgx/config/<KEY> bus mirror; ConfigWatcher also reacts to the file
    change. Only keys present in CONFIG_SCHEMA can be written. The write is
    atomic and preserves comments/formatting.
    """
    spec = _schema_index().get(key)
    if spec is None:
        raise KeyError(f"Unknown or non-writable setting: {key}")

    coerced = _coerce_value(spec, value)

    try:
        with open(env_path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []

    new_line = f"{key}={coerced}\n"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # Match an active (uncommented) assignment of this key.
        if stripped.startswith(f"{key}="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    tmp = f"{env_path}.tmp"
    with open(tmp, "w") as fh:
        fh.writelines(lines)
    os.replace(tmp, env_path)

    return {"key": key, "value": coerced}
