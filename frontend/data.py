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
from datetime import datetime, timedelta

from dotenv import dotenv_values

from frontend.config_schema import CONFIG_SCHEMA

DEFAULT_PLAN_PATH = "/dev/shm/cerbo_ai_plan.json"


def _env():
    # Read fresh each call so config edits show up without a restart.
    return dotenv_values(".env")


def plan_path() -> str:
    return _env().get("AI_PLAN_EXPORT_PATH") or DEFAULT_PLAN_PATH


def history_dir() -> str:
    return _env().get("HISTORY_DIR") or "data/history"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _day_totals(path: str):
    """A day's cumulative grid cost/reward/kWh as the MAX of each counter across the
    day's cycle records. The daily counters only increase within a day, so the max is
    the end-of-day total — and unlike reading just the final record, this is robust to
    a malformed or partial last record (one written with e.g. day_export_reward=null
    previously dropped the WHOLE day out of the month total)."""
    imp_cost = exp_rev = imp_kwh = exp_kwh = None

    def _mx(cur, v):
        v = _f(v)
        if v is None:
            return cur
        return v if (cur is None or v > cur) else cur

    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("kind") not in (None, "cycle"):
                    continue
                imp_cost = _mx(imp_cost, r.get("day_import_cost"))
                exp_rev = _mx(exp_rev, r.get("day_export_reward"))
                imp_kwh = _mx(imp_kwh, r.get("day_import_kwh"))
                exp_kwh = _mx(exp_kwh, r.get("day_export_kwh"))
    except (FileNotFoundError, OSError):
        return None
    if imp_cost is None and exp_rev is None:
        return None
    return {"import_cost": imp_cost, "export_reward": exp_rev,
            "import_kwh": imp_kwh, "export_kwh": exp_kwh}


def monthly_history() -> list:
    """Per-day net totals for the current calendar month (Trends monthly chart).
    net_eur = export_reward - import_cost (profit positive). Days with no data are
    skipped."""
    today = datetime.now().date()
    d = today.replace(day=1)
    out = []
    while d <= today:
        t = _day_totals(os.path.join(history_dir(), f"ess-{d.strftime('%Y-%m-%d')}.ndjson"))
        if t is not None:
            imp_cost = t["import_cost"] or 0.0
            exp_rev = t["export_reward"] or 0.0
            out.append({
                "date": d.strftime("%Y-%m-%d"),
                "day": d.day,
                "net_eur": round(exp_rev - imp_cost, 2),   # profit positive
                "import_cost": round(imp_cost, 2),
                "export_reward": round(exp_rev, 2),
                "import_kwh": t["import_kwh"],
                "export_kwh": t["export_kwh"],
                "is_today": d == today,
            })
        d += timedelta(days=1)
    return out


def mtd_net_eur() -> dict:
    """Month-to-date result for the header chip: the sum of our settled daily totals
    for the current calendar month (profit positive = Σexport_reward − Σimport_cost),
    including today's running total. Sourced from our own history — deterministic and
    always available (we tried Tibber's monthly GraphQL but it only exposes completed
    months and didn't reflect mid-month bonuses, so it was dropped)."""
    days = monthly_history()
    imp = sum(d["import_cost"] for d in days)
    exp = sum(d["export_reward"] for d in days)
    return {
        "net": round(exp - imp, 2),
        "import_cost": round(imp, 2),
        "export_reward": round(exp, 2),
        "days": len(days),
        "month": datetime.now().strftime("%b"),
    }


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


def _today_history_path() -> str:
    today = datetime.now().date().isoformat()
    return os.path.join(history_dir(), f"ess-{today}.ndjson")


def _slot_key(dt) -> str:
    """A slot's 'HH:MM' start, rounded down to the 15-minute boundary."""
    return f"{dt.hour:02d}:{(dt.minute // 15) * 15:02d}"


def _actual_load_by_slot(day) -> dict:
    """Per-slot actual house consumption (kWh) for a day, derived from the cumulative
    load_actual_today_wh counter in the CYCLE records. This is available for ALL days —
    the counter predates the per-slot actual_load_kwh settlement field — so previous
    days can show real consumption too. Keyed by the slot's start 'HH:MM'."""
    path = os.path.join(history_dir(), f"ess-{day.isoformat()}.ndjson")
    cycles = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("kind") not in (None, "cycle"):
                    continue
                ts = _parse_time(r.get("ts"))
                lw = _f(r.get("load_actual_today_wh"))
                if ts is not None and lw is not None:
                    cycles.append((ts, lw))
    except (FileNotFoundError, OSError):
        return {}
    cycles.sort(key=lambda x: x[0])
    out = {}
    for i in range(1, len(cycles)):
        prev_ts, prev_lw = cycles[i - 1]
        delta = cycles[i][1] - prev_lw
        if delta < -1e-6:          # midnight reset / counter restart -> skip this gap
            continue
        out[_slot_key(prev_ts)] = round(delta / 1000.0, 3)   # load for the slot starting then
    return out


def _settled_slots_for_day(day, cutoff=None) -> list:
    """Read one day's settled slots from history as schedule-shaped rows.

    Settlement records are actuals for a slot that has closed. We expose them in
    the same hour tree as the forward plan, using ``time`` as the original slot
    start. ``cutoff`` (a datetime) drops rows at/after it — used for today so the
    live forward plan owns the active and future slots.
    """
    path = os.path.join(history_dir(), f"ess-{day.isoformat()}.ndjson")
    slots = []
    load_map = _actual_load_by_slot(day)   # per-slot consumption (works for old days too)

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "settlement":
                    continue
                start = _parse_time(rec.get("slot_start"))
                if start is None or start.date() != day:
                    continue
                if cutoff is not None and start >= cutoff:
                    continue

                imp_f = _num(rec.get("actual_import_kwh"))
                exp_f = _num(rec.get("actual_export_kwh"))
                grid = None
                if imp_f is not None or exp_f is not None:
                    grid = (imp_f or 0.0) - (exp_f or 0.0)

                load_val = _num(rec.get("actual_load_kwh"))     # stored from today on
                if load_val is None:
                    load_val = load_map.get(_slot_key(start))   # derived for older days

                slots.append({
                    "time": start.isoformat(),
                    "settled": True,
                    "closed_at": rec.get("slot_end"),
                    "control_action": rec.get("predicted_control_action") or "IDLE",
                    "reason": "Settled actuals from history",
                    "reason_code": "SETTLED_ACTUAL",
                    "grid_energy": grid,
                    "price": _num(rec.get("price_buy")) or 0.0,
                    "sell": _num(rec.get("price_sell")) or _num(rec.get("price_buy")) or 0.0,
                    "pv": _num(rec.get("actual_pv_kwh")),
                    "load": load_val,
                    "soc_start": _num(rec.get("soc_start")),
                    "soc_end": _num(rec.get("soc_end")),
                    "actual_import_kwh": imp_f,
                    "actual_export_kwh": exp_f,
                    "actual_cost": _num(rec.get("actual_cost")),
                    "actual_reward": _num(rec.get("actual_reward")),
                    "actual_net_eur": _num(rec.get("actual_net_eur")),
                    "incomplete": bool(rec.get("incomplete")),
                })
    except (FileNotFoundError, OSError):
        return []

    slots.sort(key=lambda s: s["time"])
    return slots


def settled_slots_for_today(forecast_start: str | None = None) -> list:
    """Today's settled slots (history rows before the live plan takes over)."""
    return _settled_slots_for_day(datetime.now().date(),
                                  cutoff=_parse_time(forecast_start))


def previous_day_schedule(days_back: int = 1) -> dict:
    """Build a settled hour-tree for a prior day (default yesterday) so the UI can
    show it collapsed beneath today's schedule, giving a continuous 2-3 day view."""
    day = datetime.now().date() - timedelta(days=days_back)
    slots = _settled_slots_for_day(day)
    hours = group_by_hour(slots) if slots else []
    imp_cost = sum((s.get("actual_cost") or 0.0) for s in slots)
    exp_rev = sum((s.get("actual_reward") or 0.0) for s in slots)
    imp_kwh = sum((s.get("actual_import_kwh") or 0.0) for s in slots)
    exp_kwh = sum((s.get("actual_export_kwh") or 0.0) for s in slots)
    return {
        "date": day.isoformat(),
        "label": day.strftime("%a %d %b"),
        "available": bool(slots),
        "hours": hours,
        "summary": {
            "import_kwh": round(imp_kwh, 2), "import_cost": round(imp_cost, 3),
            "export_kwh": round(exp_kwh, 2), "export_rev": round(exp_rev, 3),
            "net": round(imp_cost - exp_rev, 2), "slots": len(slots),
        },
    }


def group_by_hour(schedule: list) -> list:
    """Group 15-minute slots into hour buckets with aggregates for the tree view.

    The first slot in the schedule is "now"; its slot and hour are flagged
    ``is_current`` so the UI can highlight and jump to it.
    """
    current_slot = next((s for s in schedule if not s.get("settled")), None)
    current_time = current_slot.get("time") if current_slot else None
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
        slot["is_current"] = (slot.get("time") == current_time and not slot.get("settled"))
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
                "grid_kwh": 0.0, "production_kwh": 0.0, "consumption_kwh": 0.0,
                "prices": [],
                "mode_counts": {},
            }
            order.append(key)
        h = hours[key]
        g = slot.get("grid_energy", 0.0) or 0.0
        buy = slot.get("price", 0.0) or 0.0
        sell = slot.get("sell", buy) or buy
        # Physical energy balance per hour (signed grid: + import / − export).
        h["grid_kwh"] += g
        h["production_kwh"] += slot.get("pv", 0.0) or 0.0
        h["consumption_kwh"] += slot.get("load", 0.0) or 0.0
        if slot.get("settled"):
            imp = slot.get("actual_import_kwh")
            exp = slot.get("actual_export_kwh")
            imp_cost = slot.get("actual_cost")
            exp_rev = slot.get("actual_reward")
            if imp is not None:
                h["import_kwh"] += imp
            if imp_cost is not None:
                h["import_cost"] += imp_cost
            if exp is not None:
                h["export_kwh"] += exp
            if exp_rev is not None:
                h["export_rev"] += exp_rev
        elif is_idle(slot):
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
            "grid_kwh": round(h["grid_kwh"], 2),
            "production_kwh": round(h["production_kwh"], 2),
            "consumption_kwh": round(h["consumption_kwh"], 2),
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
    forecast_start = schedule[0].get("time") if schedule else None
    timeline_schedule = settled_slots_for_today(forecast_start) + schedule
    return {
        "available": True,
        "generated_at": raw.get("generated_at"),
        "age_seconds": age_s,
        "stale": (age_s is not None and age_s > 1800),  # >30 min old
        "battery_soc": raw.get("battery_soc"),
        "pv_remaining_wh": raw.get("pv_remaining_wh"),
        "pv_today_total_kwh": raw.get("pv_today_total_kwh"),
        "pv_tomorrow_wh": raw.get("pv_tomorrow_wh"),
        "price_points": raw.get("price_points"),
        "slot_duration_h": raw.get("slot_duration_h"),
        "current": raw.get("current", {}),
        "today": raw.get("today", {}),
        "victron_slots": raw.get("victron_slots", []),
        "hours": group_by_hour(timeline_schedule),
        "day_summary": day_summary(schedule, raw.get("today_actuals")),
        "mtd_net": mtd_net_eur(),
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
