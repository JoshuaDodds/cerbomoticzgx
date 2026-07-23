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
import logging
import inspect
import math
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from importlib import import_module

from dotenv import dotenv_values

from frontend.config_schema import CONFIG_SCHEMA
from lib.config_paths import env_path as runtime_env_path
from lib import history_store as _hist
from lib import tesla_budget as _tesla_budget

DEFAULT_PLAN_PATH = "/dev/shm/cerbo_ai_plan.json"
MIN_FORECAST_BOX_SAMPLES = 8
FORECAST_BUCKETS_PER_DAY = 96
MIN_COMPLETE_FORECAST_COVERAGE = 0.75
LATEST_COMPLETE_START_BUCKET = 2   # a first observation no later than 00:30
EARLIEST_COMPLETE_END_BUCKET = 94  # a final observation no earlier than 23:30


def _env():
    # Read fresh each call so config edits show up without a restart.
    return dotenv_values(runtime_env_path())


def plan_path() -> str:
    return _env().get("AI_PLAN_EXPORT_PATH") or DEFAULT_PLAN_PATH


def history_dir() -> str:
    return _env().get("HISTORY_DIR") or "data/history"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _records_from_path(path: str) -> list:
    """Parse an NDJSON history file into records, tolerant of blank/torn lines.

    Retained for the path-based ``_day_totals`` API; day-oriented reads elsewhere go
    through ``history_store.read_day`` so Parquet-compacted months are served too."""
    recs = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        return []
    return recs


def _day_totals_from_records(records):
    """A day's cumulative grid cost/reward/kWh, taken as the LAST valid reading of each
    daily counter across the day's cycle records.

    Tibber resets these counters at midnight and they climb monotonically through the
    day, so the final reading IS the day total. We deliberately do NOT take the MAX:
    the first cycle written just after midnight still holds the PREVIOUS day's
    cumulative total (Tibber resets a moment later), so a MAX picks up yesterday's
    higher value and corrupts the day — e.g. reporting a loss on a day that was
    actually profitable, and skewing the month total. Taking the last non-null reading
    skips that stale opening record and stays robust to a malformed/partial LAST
    record: a trailing null is ignored and the prior valid reading is used instead of
    dropping the whole day."""
    imp_cost = exp_rev = imp_kwh = exp_kwh = None

    def _last(cur, v):
        v = _f(v)
        return v if v is not None else cur

    for r in records:
        if r.get("kind") not in (None, "cycle"):
            continue
        imp_cost = _last(imp_cost, r.get("day_import_cost"))
        exp_rev = _last(exp_rev, r.get("day_export_reward"))
        imp_kwh = _last(imp_kwh, r.get("day_import_kwh"))
        exp_kwh = _last(exp_kwh, r.get("day_export_kwh"))
    if imp_cost is None and exp_rev is None:
        return None
    return {"import_cost": imp_cost, "export_reward": exp_rev,
            "import_kwh": imp_kwh, "export_kwh": exp_kwh}


def _day_totals(path: str):
    """Back-compat path-based wrapper around :func:`_day_totals_from_records`."""
    return _day_totals_from_records(_records_from_path(path))


def _forecast_box_stats(values) -> dict:
    """Return the middle spread and full observed range of daily forecasts.

    These are successive revisions, not independent random observations. Every
    valid snapshot therefore remains visible in the low/high range; labelling a
    late, accurate revision as a statistical outlier would be misleading.
    """
    ordered = sorted(
        value for raw in values
        if (value := _f(raw)) is not None and math.isfinite(value)
    )
    # Eight observations provide at least two samples per quartile. Below this,
    # quartile interpolation and Tukey outlier labels look authoritative without
    # representing a useful intraday forecast distribution.
    if len(ordered) < MIN_FORECAST_BOX_SAMPLES:
        return {}

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    q1 = percentile(0.25)
    median = percentile(0.50)
    q3 = percentile(0.75)
    return {
        "forecast_q1_eur": round(q1, 2),
        "forecast_median_eur": round(median, 2),
        "forecast_q3_eur": round(q3, 2),
        "forecast_range_low_eur": round(ordered[0], 2),
        "forecast_range_high_eur": round(ordered[-1], 2),
    }


def _canonical_forecast_snapshots(records, *, day, extra=None) -> dict:
    """Keep the latest forecast in each local 15-minute period.

    Optimizer replans can also be event-triggered, so raw history may contain
    several revisions in one quarter and no revision in another. Treating each
    write as an equally weighted sample lets restarts/button presses distort the
    chart. Canonical buckets give elapsed time—not replan frequency—the weight.
    """
    raw_values = []
    buckets = {}

    def add(value, timestamp):
        numeric = _f(value)
        if numeric is None or not math.isfinite(numeric):
            return
        try:
            parsed = (
                timestamp if isinstance(timestamp, datetime)
                else datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            )
        except (TypeError, ValueError):
            raw_values.append(numeric)
            return
        if parsed.date() != day:
            return
        raw_values.append(numeric)
        bucket = parsed.hour * 4 + parsed.minute // 15
        previous = buckets.get(bucket)
        if previous is None or parsed >= previous[0]:
            buckets[bucket] = (parsed, numeric)

    for record in records:
        if record.get("kind") not in (None, "cycle"):
            continue
        add(record.get("forecast_day_net_eur"), record.get("ts"))
    if extra is not None:
        add(extra[0], extra[1])

    ordered = [buckets[key][1] for key in sorted(buckets)]
    if not ordered:
        # Preserve compatibility fields for old timestamp-less history, but do
        # not claim coverage or distribution statistics from it.
        ordered = raw_values
    bucket_count = len(buckets)
    coverage = bucket_count / FORECAST_BUCKETS_PER_DAY
    complete = bool(
        bucket_count >= MIN_FORECAST_BOX_SAMPLES
        and coverage >= MIN_COMPLETE_FORECAST_COVERAGE
        and min(buckets, default=FORECAST_BUCKETS_PER_DAY) <= LATEST_COMPLETE_START_BUCKET
        and max(buckets, default=-1) >= EARLIEST_COMPLETE_END_BUCKET
    )
    return {
        "values": ordered,
        "raw_samples": len(raw_values),
        "samples": bucket_count if buckets else len(ordered),
        "coverage_pct": round(coverage * 100.0, 1),
        "complete": complete,
    }


def monthly_history() -> list:
    """Per-day settled net and prospective intraday forecast distributions.

    ``net_eur`` is export reward minus import cost (profit positive). Forecast
    Box-plot and compatibility range fields are emitted only when newer cycle
    records contain a same-day final-net forecast; legacy history remains an
    honest actual-only point instead of receiving an invented distribution.
    """
    today = datetime.now().date()
    d = today.replace(day=1)
    out = []
    today_projection = projected_today_net_eur()
    while d <= today:
        records = _hist.read_day(d, history_dir())
        t = _day_totals_from_records(records)
        if t is not None:
            imp_cost = t["import_cost"] or 0.0
            exp_rev = t["export_reward"] or 0.0
            row = {
                "date": d.strftime("%Y-%m-%d"),
                "day": d.day,
                "net_eur": round(exp_rev - imp_cost, 2),   # profit positive
                "import_cost": round(imp_cost, 2),
                "export_reward": round(exp_rev, 2),
                "import_kwh": t["import_kwh"],
                "export_kwh": t["export_kwh"],
                "is_today": d == today,
                "settled": d < today,
            }
            extra = None
            if d == today and today_projection is not None:
                row["projected_net_eur"] = today_projection
                extra = (today_projection, datetime.now().astimezone())
            snapshot_data = _canonical_forecast_snapshots(
                records, day=d, extra=extra)
            forecasts = snapshot_data["values"]
            if forecasts:
                row.update({
                    "forecast_open_eur": round(forecasts[0], 2),
                    "forecast_low_eur": round(min(forecasts), 2),
                    "forecast_high_eur": round(max(forecasts), 2),
                    "forecast_close_eur": round(forecasts[-1], 2),
                    "forecast_samples": snapshot_data["samples"],
                    "forecast_raw_samples": snapshot_data["raw_samples"],
                    "forecast_coverage_pct": snapshot_data["coverage_pct"],
                    "forecast_complete": snapshot_data["complete"],
                    "forecast_box_min_samples": MIN_FORECAST_BOX_SAMPLES,
                })
                if d == today or snapshot_data["complete"]:
                    row.update(_forecast_box_stats(forecasts))
            out.append(row)
        d += timedelta(days=1)
    return out


def projected_today_net_eur() -> float | None:
    """Projected full-day profit-positive net from the current plan JSON."""
    raw = load_raw_plan()
    if not raw:
        return None
    schedule = raw.get("schedule") or []
    if not schedule:
        return None
    try:
        today = datetime.now().date().isoformat()
        summary = day_summary(schedule, raw.get("today_actuals"))
        row = next((d for d in summary.get("days", []) if d.get("date") == today), None)
        if not row or row.get("net") is None:
            return None
        return round(-float(row["net"]), 2)  # day_summary net is cost-positive.
    except Exception as exc:
        logging.debug("Projected today net unavailable: %s", exc)
        return None


def forecast_accuracy(days: int = 3) -> dict:
    """Recent actual-vs-forecast PV/load settlement rows for the Trends overlay."""
    try:
        days = max(1, min(14, int(days)))
    except (TypeError, ValueError):
        days = 3

    today = datetime.now().date()
    slots = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        load_map = None
        for rec in _hist.read_day(day, history_dir()):
            if rec.get("kind") != "settlement" or rec.get("incomplete"):
                continue
            start = _parse_time(rec.get("slot_start"))
            if start is None:
                continue

            predicted_load = _f(rec.get("predicted_load_kwh"))
            actual_load = _f(rec.get("actual_load_kwh"))
            if actual_load is None:
                if load_map is None:
                    load_map = _actual_load_by_slot(day)
                actual_load = load_map.get(_slot_key(start))

            predicted_pv = _f(rec.get("predicted_pv_kwh"))
            actual_pv = _f(rec.get("actual_pv_kwh"))
            if (predicted_load is None or actual_load is None) and (
                predicted_pv is None or actual_pv is None
            ):
                continue

            row = {
                "time": start.isoformat(),
                "label": start.strftime("%a %H:%M"),
                "predicted_load_kwh": predicted_load,
                "actual_load_kwh": actual_load,
                "predicted_pv_kwh": predicted_pv,
                "actual_pv_kwh": actual_pv,
            }
            if predicted_load is not None and actual_load is not None:
                row["load_error_kwh"] = round(actual_load - predicted_load, 3)
            if predicted_pv is not None and actual_pv is not None:
                row["pv_error_kwh"] = round(actual_pv - predicted_pv, 3)
            slots.append(row)

    slots.sort(key=lambda s: s["time"])

    def _metric_summary(prefix):
        pairs = [
            (s.get(f"predicted_{prefix}_kwh"), s.get(f"actual_{prefix}_kwh"))
            for s in slots
        ]
        pairs = [(p, a) for p, a in pairs if p is not None and a is not None]
        if not pairs:
            return {f"{prefix}_points": 0, f"{prefix}_mae_kwh": None, f"{prefix}_bias_kwh": None}
        errors = [a - p for p, a in pairs]
        return {
            f"{prefix}_points": len(pairs),
            f"{prefix}_mae_kwh": round(sum(abs(e) for e in errors) / len(errors), 3),
            f"{prefix}_bias_kwh": round(sum(errors), 3),
        }

    summary = {"slots": len(slots), "days": days}
    summary.update(_metric_summary("load"))
    summary.update(_metric_summary("pv"))
    return {"available": bool(slots), "slots": slots, "summary": summary}


def weather_dashboard() -> dict:
    """Weather summary for the desktop Weather tab."""
    try:
        from lib import weather
        return weather.weather_snapshot()
    except Exception as e:
        return {"available": False, "reason": str(e)}


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
    """True when a slot is IDLE (Victron-managed, neutral setpoint)."""
    return str(slot.get("control_action") or "").upper() == "IDLE"


# At/above this SoC the battery is treated as full, so PV surplus feeds the grid.
# Below it, an IDLE slot's surplus stores into the battery instead of exporting.
PV_SURPLUS_FULL_SOC = 99.0


def _forward_grid_econ(slot):
    """Projected grid economics ``(import_kwh, import_cost, export_kwh, export_rev)``
    for an unsettled slot, matched to what will actually settle at the meter.

    A commanded import (BUY / RETAIN) or a SELL battery discharge crosses the meter
    as planned. But an IDLE slot's PV surplus only reaches the grid when the battery
    is full; below that the neutral setpoint stores it in the battery — raising SoC
    and lowering the stored-energy cost basis — so it books NO grid revenue here.
    That stored value is realised later (a SELL slot, or an avoided future import),
    so the running projection still converges to the settled day total without
    booking phantom self-consumption "profit".
    """
    g = slot.get("grid_energy", 0.0) or 0.0
    buy = slot.get("price", 0.0) or 0.0
    sell = slot.get("sell", buy) or buy
    if g > 0:
        return g, g * buy, 0.0, 0.0
    if g < 0:
        soc_end = slot.get("soc_end")
        battery_full = soc_end is not None and soc_end >= PV_SURPLUS_FULL_SOC
        if not is_idle(slot) or battery_full:
            return 0.0, 0.0, -g, -g * sell
    return 0.0, 0.0, 0.0, 0.0


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
    cycles = []
    for r in _hist.read_day(day, history_dir()):
        if r.get("kind") not in (None, "cycle"):
            continue
        ts = _parse_time(r.get("ts"))
        lw = _f(r.get("load_actual_today_wh"))
        if ts is not None and lw is not None:
            cycles.append((ts, lw))
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
    slots = []
    load_map = _actual_load_by_slot(day)   # per-slot consumption (works for old days too)

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for rec in _hist.read_day(day, history_dir()):
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
    # Headline totals come from the cumulative day counters (same authoritative source
    # as the Trends monthly chart), NOT the sum of per-slot settlements: if a slot's
    # settlement was ever dropped (e.g. a mid-slot re-optimize), the per-slot sum drifts
    # from the meter, but the cumulative counter is always right. Keeps the two views in
    # agreement. Per-slot rows (hours) are still shown for detail.
    totals = _day_totals_from_records(_hist.read_day(day, history_dir())) or {}
    imp_cost = totals.get("import_cost") or 0.0
    exp_rev = totals.get("export_reward") or 0.0
    imp_kwh = totals.get("import_kwh") or 0.0
    exp_kwh = totals.get("export_kwh") or 0.0
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
                "grid_kwh": 0.0, "production_kwh": 0.0, "consumption_kwh": 0.0,
                "prices": [],
                "mode_counts": {},
                "planned_ev_kwh": 0.0,
                "ev_target_kw": 0.0,
                "ev_supply_counts": {},
                "ev_tentative": False,
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
        h["planned_ev_kwh"] += _f(slot.get("planned_ev_kwh")) or 0.0
        h["ev_target_kw"] = max(h["ev_target_kw"], _f(slot.get("ev_target_kw")) or 0.0)
        if (_f(slot.get("planned_ev_kwh")) or 0.0) > 0:
            supply = str(slot.get("ev_supply") or "grid").lower()
            h["ev_supply_counts"][supply] = h["ev_supply_counts"].get(supply, 0) + 1
            h["ev_tentative"] = h["ev_tentative"] or bool(slot.get("ev_tentative"))
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
        else:
            # Forward slot: project the grid flow that will actually settle. IDLE
            # PV-surplus into a non-full battery charges it (no grid revenue) — see
            # _forward_grid_econ — so the forecast doesn't book phantom self-
            # consumption profit yet still converges to the settled day total.
            f_imp_kwh, f_imp_cost, f_exp_kwh, f_exp_rev = _forward_grid_econ(slot)
            h["import_kwh"] += f_imp_kwh
            h["import_cost"] += f_imp_cost
            h["export_kwh"] += f_exp_kwh
            h["export_rev"] += f_exp_rev
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
        ev_supplies = h["ev_supply_counts"]
        ev_supply = max(ev_supplies, key=ev_supplies.get) if ev_supplies else None
        result.append({
            "key": h["key"],
            "label": h["label"],
            "hour_start": h["hour_start"],
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
            "planned_ev_kwh": round(h["planned_ev_kwh"], 2),
            "ev_target_kw": round(h["ev_target_kw"], 2),
            "ev_supply": ev_supply,
            "ev_tentative": h["ev_tentative"],
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
                       "export_kwh": 0.0, "export_rev": 0.0}
            order.append(d)
        # Project the grid flow that will actually settle. IDLE PV-surplus into a
        # non-full battery charges it (SoC up / cost basis down, no grid revenue)
        # rather than exporting — see _forward_grid_econ — so the forecast stays
        # complete and converges to the settled actuals without phantom profit.
        f_imp_kwh, f_imp_cost, f_exp_kwh, f_exp_rev = _forward_grid_econ(slot)
        days[d]["import_kwh"] += f_imp_kwh
        days[d]["import_cost"] += f_imp_cost
        days[d]["export_kwh"] += f_exp_kwh
        days[d]["export_rev"] += f_exp_rev

    today = datetime.now().date().isoformat()
    rows = []
    _keys = ("import_kwh", "import_cost", "export_kwh", "export_rev")
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
        rows.append({
            "date": d, "label": row["label"], "is_today": d == today,
            "forecast": forecast, "actual": actual, "combined": combined,
            # Running net = all projected + settled grid flows (import cost − export
            # revenue). IDLE self-consumption / PV-surplus is now included so the
            # projection is complete and converges to the settled day total.
            "net": round(combined["import_cost"] - combined["export_rev"], 3),
        })

    return {
        "days": rows,
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
        "optimizer_mode": raw.get("optimizer_mode") or "summer",
        "age_seconds": age_s,
        "stale": (age_s is not None and age_s > 1800),  # >30 min old
        "battery_soc": raw.get("battery_soc"),
        "pv_remaining_wh": raw.get("pv_remaining_wh"),
        "pv_remaining_raw_wh": raw.get("pv_remaining_raw_wh"),
        "pv_remaining_raw_source": raw.get("pv_remaining_raw_source"),
        "pv_adjusted_remaining_wh": raw.get("pv_adjusted_remaining_wh"),
        "pv_adjusted_remaining_source": raw.get("pv_adjusted_remaining_source"),
        "pv_adjustment_kwh": raw.get("pv_adjustment_kwh"),
        "pv_today_total_kwh": raw.get("pv_today_total_kwh"),
        "pv_tomorrow_wh": raw.get("pv_tomorrow_wh"),
        "price_points": raw.get("price_points"),
        "slot_duration_h": raw.get("slot_duration_h"),
        "current": raw.get("current", {}),
        "winter_policy": raw.get("winter_policy"),
        "today": raw.get("today", {}),
        "victron_slots": raw.get("victron_slots", []),
        "ev_smart_charge": raw.get("ev_smart_charge") or raw.get("ev_charge_plan"),
        "hours": group_by_hour(timeline_schedule),
        "day_summary": day_summary(schedule, raw.get("today_actuals")),
        "mtd_net": mtd_net_eur(),
    }


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if is_dataclass(value):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return vars(value) if hasattr(value, "__dict__") else str(value)


def _optional_path_call(fn, path):
    parameters = inspect.signature(fn).parameters
    accepts_path = "path" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    return fn(path=path) if path and accepts_path else fn()


def ev_smart_charge_dashboard() -> dict:
    """Read the optional job lazily and combine it with the published shadow/live plan."""
    raw = load_raw_plan() or {}
    plan = raw.get("ev_smart_charge") or raw.get("ev_charge_plan")
    try:
        module = import_module("lib.ev_smart_charge")
        snapshot = getattr(module, "dashboard_snapshot", None)
        if callable(snapshot):
            value = _json_value(snapshot()) or {}
            if isinstance(value, dict):
                value.setdefault("available", True)
                if plan is not None:
                    value.setdefault("plan", plan)
                return value
        loader = next((getattr(module, name, None) for name in
                       ("load_job", "get_job", "current_job")
                       if callable(getattr(module, name, None))), None)
        env = _env()
        job = _json_value(_optional_path_call(
            loader, env.get("EV_SMART_CHARGE_JOB_PATH"))) if loader else None
        plan_loader = getattr(module, "load_plan_snapshot", None)
        if plan is None and callable(plan_loader):
            plan = _json_value(_optional_path_call(
                plan_loader, env.get("EV_SMART_CHARGE_PLAN_PATH")))
        enabled = getattr(module, "enabled", None)
        configured = str(env.get("EV_SMART_CHARGE_ENABLED", "False")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        apply_enabled = str(env.get("EV_SMART_CHARGE_APPLY", "False")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        return {
            "available": True,
            "enabled": bool(enabled()) if callable(enabled) else configured,
            "apply": apply_enabled,
            "job": job,
            "plan": plan,
        }
    except (ImportError, OSError, RuntimeError, ValueError) as e:
        return {
            "available": False,
            "job": None,
            "plan": plan,
            "message": f"Smart charging is unavailable: {e}",
        }


def tesla_usage() -> dict:
    """Current billing cycle's Tesla Fleet API spend (counts + € per category + total) for the
    Vehicle tab, including the Streaming Signals line — both durably persisted to the same
    tesla_budget state file by lib/tesla_budget.usage_snapshot(), so this survives restarts."""
    path = _env().get("TESLA_BUDGET_STATE_PATH") or _tesla_budget.DEFAULT_STATE_PATH
    return _tesla_budget.usage_snapshot(path)


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


def _format_bound(value):
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _validate_bounds(spec, value):
    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        label = spec.get("key", "value")
        if minimum is not None and maximum is not None:
            raise ValueError(
                f"{label} must be between {_format_bound(minimum)} and {_format_bound(maximum)}"
            )
        if minimum is not None:
            raise ValueError(f"{label} must be >= {_format_bound(minimum)}")
        raise ValueError(f"{label} must be <= {_format_bound(maximum)}")
    return value


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
        return str(_validate_bounds(spec, int(float(raw))))
    if t == "float":
        return str(_validate_bounds(spec, float(raw)))
    # str / enum
    if "options" in spec and raw not in spec["options"]:
        raise ValueError(f"must be one of {spec['options']}")
    return raw


def update_env_setting(key: str, value, env_path: str | None = None) -> dict:
    """Persist a single allow-listed setting to .env (durable source of truth).

    The main service's retrieve_setting() re-reads .env on every call (so the
    change applies on the next optimization/decision cycle) and republishes the
    Cerbomoticzgx/config/<KEY> bus mirror; ConfigWatcher also reacts to the file
    change. Only keys present in CONFIG_SCHEMA can be written. The write is
    atomic and preserves comments/formatting.
    """
    env_path = env_path or runtime_env_path()
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
