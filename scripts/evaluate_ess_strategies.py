#!/usr/bin/env python3
"""Compare read-only ESS strategy candidates against an exported AI plan.

This command deliberately does *not* import the energy broker, settings,
GlobalState, MQTT, or Victron modules.  It only reads a JSON plan, evaluates
the pure candidate model, and writes its report to stdout.

Every row of the report — including the active plan's own ``plan_baseline`` row
— covers one calendar day, midnight to midnight, matching the dashboard's Today
tile: the already-settled part of today plus that policy's planned remainder.
Candidates still optimize over the *whole* known horizon; only their reported
result is limited to today.  Truncating the horizon instead would remove the
terminal value of retained energy and let every candidate empty the battery by
midnight for free.  Because a today-only total never debits the emptier battery
a policy hands to tomorrow, each row also reports the energy it carries past
midnight and what that energy is worth.

Examples:
    # A plan that already carries explicit strategy_candidate_config metadata.
    python scripts/evaluate_ess_strategies.py --plan /dev/shm/cerbo_ai_plan.json

    # Make all assumptions explicit in a replay file, then emit JSON.
    python scripts/evaluate_ess_strategies.py --plan saved-plan.json \
        --config research-assumptions.json --json

    # Exploratory only: use clearly labelled, static research defaults.
    python scripts/evaluate_ess_strategies.py --plan saved-plan.json \
        --use-research-defaults --json

The final form is intentionally opt-in.  Research defaults are not read from
the live .env and are never a substitute for a reviewed physical model.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.ess_strategy_candidates import (  # noqa: E402
    WINTER_APPROXIMATION_CAVEATS,
    CandidateConfig,
    CandidateStep,
    DeterministicSlot,
    evaluate_shadow_candidates,
    first_local_day_steps,
    summarize_steps,
)


DEFAULT_PLAN_PATH = "/dev/shm/cerbo_ai_plan.json"

# These values intentionally live here rather than being read from .env.  They
# make the exploratory mode reproducible and visibly different from a live
# optimizer configuration.  A report names every field that came from here.
RESEARCH_DEFAULTS = {
    "battery_capacity_kwh": 42.0,
    "min_soc_percent": 5.0,
    "protected_soc_percent": 20.0,
    "charge_efficiency": 0.96,
    "discharge_efficiency": 0.96,
    "max_charge_kw": 13.0,
    "max_discharge_kw": 15.0,
    "max_import_kw": 13.0,
    "max_export_kw": 13.0,
    # Five percent keeps the observational comparison fast on Pi-class hosts.
    # It is an evaluator resolution, never a live optimizer setting.
    "soc_step_percent": 5.0,
    "grid_charge_soc_cap_percent": 100.0,
    "cycle_cost_eur_per_dc_kwh": 0.03,
    "arbitrage_margin_eur_per_dc_kwh": 0.03,
    "export_price_factor": 1.0,
    "export_fee_eur_per_kwh": 0.0,
    "terminal_value_eur_per_dc_kwh": 0.0,
}
CONFIG_FIELDS = tuple(RESEARCH_DEFAULTS)

CLI_FIELD_OPTIONS = {
    "battery_capacity_kwh": "battery_capacity_kwh",
    "min_soc_percent": "min_soc_percent",
    "protected_soc_percent": "protected_soc_percent",
    "charge_efficiency": "charge_efficiency",
    "discharge_efficiency": "discharge_efficiency",
    "max_charge_kw": "max_charge_kw",
    "max_discharge_kw": "max_discharge_kw",
    "max_import_kw": "max_import_kw",
    "max_export_kw": "max_export_kw",
    "soc_step_percent": "soc_step_percent",
    "grid_charge_soc_cap_percent": "grid_charge_soc_cap_percent",
    "cycle_cost_eur_per_dc_kwh": "cycle_cost_eur_per_dc_kwh",
    "arbitrage_margin_eur_per_dc_kwh": "arbitrage_margin_eur_per_dc_kwh",
    "export_price_factor": "export_price_factor",
    "export_fee_eur_per_kwh": "export_fee_eur_per_kwh",
    "terminal_value_eur_per_dc_kwh": "terminal_value_eur_per_dc_kwh",
}

DISPLAY_CANDIDATE_NAMES = {
    "market_arbitrage": "Market arbitrage",
    "pv_first_self_sufficiency": "PV-first self-sufficiency",
    "protected_hybrid": "Protected hybrid",
    "winter_self_sufficiency": "Winter-style self-sufficiency (approx.)",
}

# One line per row, in the vocabulary the rest of the dashboard already uses
# (BUY/SELL/RETAIN/IDLE are the literal control_action values shown elsewhere;
# "reserve"/"grid charging"/"export" match the .env and Settings wording) rather
# than the euro-denominated columns above, which describe outcome, not policy.
# Kept identical to ADVISOR_STRATEGY_LEGEND in frontend/static/js/app.js so the
# CLI and the dashboard explain each strategy the same way.
LIVE_PLAN_LEGEND = (
    "The AI optimizer actually controlling the battery right now — chooses "
    "BUY / SELL / RETAIN / IDLE each cycle from live prices and forecasts."
)
CANDIDATE_LEGEND = {
    "market_arbitrage": (
        "No protected reserve — BUYs and SELLs freely down to the minimum SoC "
        "reserve, whichever the price favors."
    ),
    "protected_hybrid": (
        "BUYs only enough to hold a protected reserve, then SELLs freely from "
        "whatever is stored above it."
    ),
    "pv_first_self_sufficiency": (
        "Never BUYs or SELLs — solar and the protected reserve cover household "
        "load alone."
    ),
    "winter_self_sufficiency": (
        "BUYs up to the reserve to cover household load, like Winter Mode's "
        "routine policy, but never SELLs."
    ),
}


def _live_plan_label(report: Mapping[str, Any]) -> str:
    mode = str(report.get("optimizer_mode") or "").strip().lower()
    if mode == "winter":
        return "Live plan (Winter mode, active)"
    if mode == "summer":
        return "Live plan (Summer mode, active)"
    return "Live plan (active)"


class EvaluationInputError(ValueError):
    """A plan or assumptions file cannot support a deterministic replay."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        default=DEFAULT_PLAN_PATH,
        help=f"exported AI plan JSON (default: {DEFAULT_PLAN_PATH})",
    )
    parser.add_argument(
        "--config",
        help=("optional JSON file containing CandidateConfig fields; command-line "
              "values override it"),
    )
    parser.add_argument(
        "--initial-soc-percent",
        type=float,
        help="override plan battery_soc; required if the plan has no battery_soc",
    )
    parser.add_argument(
        "--use-research-defaults",
        action="store_true",
        help=("fill missing assumptions from static, labelled research defaults; "
              "never reads the live .env"),
    )
    parser.add_argument(
        "--include-schedule",
        action="store_true",
        help="include per-slot candidate schedules in the human-readable report",
    )
    parser.add_argument(
        "--winter-reserve-soc-percent",
        type=float,
        default=None,
        help=("add an approximate Winter-Mode comparison row held above this "
              "reserve; defaults to the plan's winter_reserve_soc_percent when "
              "present. Never the winter engine itself"),
    )
    parser.add_argument(
        "--no-winter-candidate",
        action="store_true",
        help="omit the approximate Winter-Mode comparison row",
    )
    parser.add_argument("--json", action="store_true", help="emit full report as JSON")

    for field, dest in CLI_FIELD_OPTIONS.items():
        parser.add_argument(
            "--" + field.replace("_", "-"),
            dest=dest,
            type=float,
            default=None,
            help=f"override {field} for this read-only evaluation",
        )
    return parser


def _read_json(path_text: str, *, label: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_text)
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as error:
        raise EvaluationInputError(f"{label} not found: {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationInputError(f"{label} is not valid JSON: {path} ({error.msg})") from error
    except OSError as error:
        raise EvaluationInputError(f"cannot read {label}: {path} ({error})") from error
    if not isinstance(data, dict):
        raise EvaluationInputError(f"{label} must be a JSON object: {path}")
    return path, data


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise EvaluationInputError(f"{label} must be a JSON object")
    return dict(value)


def _plan_config_metadata(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return optional explicit assumptions embedded in a saved plan.

    ``strategy_candidate_config`` is the durable public shape.  The two nested
    fallbacks make future observational plan payloads easy to consume without
    reintroducing a dependency on the live broker.
    """
    for key in ("strategy_candidate_config", "strategy_shadow_config"):
        if key in plan:
            return _mapping(plan[key], label=f"plan.{key}")
    shadow = plan.get("strategy_shadow")
    if isinstance(shadow, Mapping) and "config" in shadow:
        return _mapping(shadow["config"], label="plan.strategy_shadow.config")
    return {}


def _config_file_values(path_text: str | None) -> dict[str, Any]:
    if not path_text:
        return {}
    _, data = _read_json(path_text, label="assumptions file")
    if "strategy_candidate_config" in data:
        data = _mapping(data["strategy_candidate_config"], label="config.strategy_candidate_config")
    unknown = sorted(set(data) - set(CONFIG_FIELDS))
    if unknown:
        raise EvaluationInputError(
            "assumptions file contains unknown CandidateConfig field(s): "
            + ", ".join(unknown)
        )
    return data


def _number(value: Any, *, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise EvaluationInputError(f"{label} must be a finite number") from error
    if not math.isfinite(parsed):
        raise EvaluationInputError(f"{label} must be a finite number")
    return parsed


def _build_config(args: argparse.Namespace, plan: Mapping[str, Any]) -> tuple[CandidateConfig, dict[str, Any]]:
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}

    def apply(source_values: Mapping[str, Any], source: str) -> None:
        for field in CONFIG_FIELDS:
            if field in source_values and source_values[field] is not None:
                values[field] = source_values[field]
                sources[field] = source

    plan_metadata = _plan_config_metadata(plan)
    unknown_metadata = sorted(set(plan_metadata) - set(CONFIG_FIELDS))
    if unknown_metadata:
        raise EvaluationInputError(
            "plan strategy assumptions contain unknown field(s): "
            + ", ".join(unknown_metadata)
        )
    apply(plan_metadata, "plan metadata")

    # Existing plan exports already preserve these two economics guardrails.
    # They are merely replay assumptions here; this command never consults live
    # settings or modifies the active strategy.
    guardrails = plan.get("optimizer_guardrails")
    if isinstance(guardrails, Mapping):
        if guardrails.get("battery_cycle_cost") is not None:
            values["cycle_cost_eur_per_dc_kwh"] = guardrails["battery_cycle_cost"]
            sources["cycle_cost_eur_per_dc_kwh"] = "plan optimizer_guardrails"
        if guardrails.get("arbitrage_margin") is not None:
            values["arbitrage_margin_eur_per_dc_kwh"] = guardrails["arbitrage_margin"]
            sources["arbitrage_margin_eur_per_dc_kwh"] = "plan optimizer_guardrails"

    apply(_config_file_values(args.config), "--config")
    apply(
        {
            field: getattr(args, dest)
            for field, dest in CLI_FIELD_OPTIONS.items()
            if getattr(args, dest) is not None
        },
        "command line",
    )

    missing = [field for field in CONFIG_FIELDS if field not in values]
    defaulted_fields: list[str] = []
    if missing and args.use_research_defaults:
        for field in missing:
            values[field] = RESEARCH_DEFAULTS[field]
            sources[field] = "research default"
            defaulted_fields.append(field)
    elif missing:
        raise EvaluationInputError(
            "Missing physical/economic assumptions: "
            + ", ".join(missing)
            + ". Supply them through plan.strategy_candidate_config, --config, "
              "or explicit flags; use --use-research-defaults only for labelled exploration."
        )

    numeric_values = {
        field: _number(values[field], label=field)
        for field in CONFIG_FIELDS
    }
    try:
        config = CandidateConfig(**numeric_values)
    except ValueError as error:
        raise EvaluationInputError(f"invalid candidate assumptions: {error}") from error
    return config, {
        "values": numeric_values,
        "sources": sources,
        "defaulted_fields": defaulted_fields,
        "uses_research_defaults": bool(defaulted_fields),
        "live_configuration_read": False,
    }


def _parse_time(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationInputError(f"{label} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvaluationInputError(f"{label} is not a valid ISO timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise EvaluationInputError(f"{label} must include a timezone offset")
    return parsed


def _slot_field(row: Mapping[str, Any], index: int, *names: str, required_name: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    raise EvaluationInputError(f"schedule[{index}] is missing {required_name}")


def _duration_from_plan(plan: Mapping[str, Any], rows: list[Mapping[str, Any]], starts: list[datetime]) -> float:
    value = plan.get("slot_duration_h")
    if value is not None:
        duration = _number(value, label="plan.slot_duration_h")
        if duration <= 0:
            raise EvaluationInputError("plan.slot_duration_h must be positive")
        return duration
    deltas = [
        (current - previous).total_seconds() / 3600.0
        for previous, current in zip(starts, starts[1:])
        if current > previous
    ]
    if not deltas:
        raise EvaluationInputError(
            "plan.slot_duration_h is missing and a duration cannot be inferred from fewer than two timestamps"
        )
    duration = float(median(deltas))
    if duration <= 0:
        raise EvaluationInputError("inferred plan slot duration must be positive")
    return duration


def _row_sell_price(row: Mapping[str, Any], index: int) -> float | None:
    if row.get("sell") is not None:
        return _number(row["sell"], label=f"schedule[{index}].sell")
    if row.get("sell_price") is not None:
        return _number(row["sell_price"], label=f"schedule[{index}].sell_price")
    return None


def _parse_schedule_rows(plan: Mapping[str, Any]) -> tuple[list[dict[str, Any]], float]:
    """Return chronologically sorted plan rows plus the plan's slot duration.

    Shared by the candidate slot builder and the live-plan baseline so both read
    exactly the same timestamps, durations, and ordering.  Each entry keeps its
    original schedule index so validation errors still name the row the user has
    in front of them.
    """
    raw_schedule = plan.get("schedule")
    if not isinstance(raw_schedule, list) or not raw_schedule:
        raise EvaluationInputError("plan.schedule must be a non-empty JSON array")
    rows: list[Mapping[str, Any]] = []
    starts: list[datetime] = []
    for index, raw in enumerate(raw_schedule):
        if not isinstance(raw, Mapping):
            raise EvaluationInputError(f"schedule[{index}] must be a JSON object")
        row = dict(raw)
        rows.append(row)
        starts.append(_parse_time(_slot_field(row, index, "time", "start", required_name="time"),
                                  label=f"schedule[{index}].time"))

    # Inferred from the supplied order, before sorting, so a plan that already
    # lists its slots chronologically keeps its previous inferred duration.
    duration = _duration_from_plan(plan, rows, starts)
    parsed: list[dict[str, Any]] = []
    for index, (row, start) in enumerate(zip(rows, starts)):
        row_duration = row.get("duration_h")
        if row_duration is not None:
            effective_duration = _number(row_duration, label=f"schedule[{index}].duration_h")
            if effective_duration <= 0:
                raise EvaluationInputError(f"schedule[{index}].duration_h must be positive")
        else:
            effective_duration = duration
        parsed.append({
            "index": index,
            "start": start,
            "row": row,
            "duration_h": effective_duration,
        })
    parsed.sort(key=lambda item: item["start"])
    for previous, current in zip(parsed, parsed[1:]):
        if current["start"] == previous["start"]:
            raise EvaluationInputError(
                f"plan.schedule contains duplicate timestamp {current['start'].isoformat()}")
    return parsed, duration


def _build_slots(parsed: list[dict[str, Any]]) -> tuple[DeterministicSlot, ...]:
    slots = []
    for item in parsed:
        row, index = item["row"], item["index"]
        slots.append(DeterministicSlot(
            start=item["start"],
            duration_h=item["duration_h"],
            buy_price=_number(
                _slot_field(row, index, "price", "buy_price", required_name="buy price"),
                label=f"schedule[{index}].price",
            ),
            sell_price=_row_sell_price(row, index),
            load_kwh=_number(
                _slot_field(row, index, "load", "load_kwh", required_name="load energy"),
                label=f"schedule[{index}].load",
            ),
            pv_kwh=_number(
                _slot_field(row, index, "pv", "pv_kwh", required_name="PV energy"),
                label=f"schedule[{index}].pv",
            ),
        ))
    return tuple(slots)


def _initial_soc(args: argparse.Namespace, plan: Mapping[str, Any]) -> float:
    value = args.initial_soc_percent
    source = "--initial-soc-percent"
    if value is None:
        value = plan.get("battery_soc")
        source = "plan.battery_soc"
    if value is None:
        raise EvaluationInputError("initial SoC is missing; supply --initial-soc-percent")
    soc = _number(value, label=source)
    if not 0.0 <= soc <= 100.0:
        raise EvaluationInputError(f"{source} must be between 0 and 100")
    return soc


def _winter_reserve(args: argparse.Namespace, plan: Mapping[str, Any]) -> float | None:
    """Resolve the reserve for the approximate Winter-Mode row, or None.

    Read from a plan key that sits *beside* ``strategy_candidate_config`` rather
    than inside it, because that mapping is validated against a closed field
    list.  A plan published before this key existed simply yields no winter row.
    """
    if args.no_winter_candidate:
        return None
    explicit = args.winter_reserve_soc_percent is not None
    value = args.winter_reserve_soc_percent
    source = "--winter-reserve-soc-percent"
    if value is None:
        value = plan.get("winter_reserve_soc_percent")
        source = "plan.winter_reserve_soc_percent"
    if value is None:
        return None
    try:
        reserve = _number(value, label=source)
        if not 0.0 <= reserve <= 100.0:
            raise EvaluationInputError(f"{source} must be between 0 and 100")
    except EvaluationInputError:
        # An operator who typed the flag deserves the error. A malformed value
        # in the published plan must only cost the optional winter row, never
        # the whole comparison the rest of the report exists to provide.
        if explicit:
            raise
        return None
    return reserve


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _candidate_report(result: Any) -> dict[str, Any]:
    return _json_safe(asdict(result))


def _settled_today(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return today's already-metered grid economics, or None when absent.

    This part of the day has already happened, so it is identical for the live
    plan and for every candidate.  Adding the same constant to each row puts
    them all on the dashboard's 00:00-23:59 basis without implying that any
    strategy could still have changed it.
    """
    actuals = plan.get("today_actuals")
    if not isinstance(actuals, Mapping):
        return None

    def _optional(name: str) -> float | None:
        value = actuals.get(name)
        if value is None:
            return None
        return _number(value, label=f"plan.today_actuals.{name}")

    import_cost = _optional("imp_cost")
    export_reward = _optional("exp_rev")
    if import_cost is None or export_reward is None:
        return None
    return {
        "import_cost_eur": import_cost,
        "export_reward_eur": export_reward,
        "cash_net_eur": export_reward - import_cost,
        "import_kwh": _optional("imp_kwh"),
        "export_kwh": _optional("exp_kwh"),
    }


def _plan_baseline_steps(
    parsed: list[dict[str, Any]],
    config: CandidateConfig,
) -> tuple[CandidateStep, ...] | None:
    """Re-express the live plan's own per-slot flows as candidate-shaped steps.

    Summing these through :func:`summarize_steps` is what makes the active plan
    and the candidates comparable: identical arithmetic over identically shaped
    data, so the only difference left between the rows is the policy.  Returns
    None when the plan omits a field this needs, so the report shows no baseline
    rather than an invented one.
    """
    steps: list[CandidateStep] = []
    for item in parsed:
        row, index = item["row"], item["index"]
        if row.get("grid_energy") is None or row.get("soc_start") is None or row.get("soc_end") is None:
            return None
        grid_energy = _number(row["grid_energy"], label=f"schedule[{index}].grid_energy")
        soc_start = _number(row["soc_start"], label=f"schedule[{index}].soc_start")
        soc_end = _number(row["soc_end"], label=f"schedule[{index}].soc_end")
        buy = _number(
            _slot_field(row, index, "price", "buy_price", required_name="buy price"),
            label=f"schedule[{index}].price",
        )
        sell = _row_sell_price(row, index)
        if sell is None:
            sell = buy * config.export_price_factor - config.export_fee_eur_per_kwh
        dc_change = (soc_end - soc_start) / 100.0 * config.battery_capacity_kwh
        steps.append(CandidateStep(
            start=item["start"],
            duration_h=item["duration_h"],
            soc_start_percent=soc_start,
            soc_end_percent=soc_end,
            dc_change_kwh=dc_change,
            grid_energy_kwh=grid_energy,
            grid_import_kwh=max(0.0, grid_energy),
            grid_export_kwh=max(0.0, -grid_energy),
            buy_price=buy,
            sell_price=sell,
            active_grid_charge=(dc_change > 0.0 and grid_energy > 0.0),
            active_battery_export=(dc_change < 0.0 and grid_energy < 0.0),
        ))
    return tuple(steps)


def _today_block(
    steps: Any,
    config: CandidateConfig,
    settled: Mapping[str, Any] | None,
    *,
    floor_soc_percent: float,
) -> dict[str, Any]:
    """Summarise the remainder of today and fold in the shared settled morning.

    ``whole_day_economic_net_eur`` charges battery wear only for the planned
    remainder: the morning's wear is unknown to this report and is in any case
    the same for every row, so it cannot change their ranking.

    A today-only total credits a policy for selling stored energy without
    debiting the emptier battery it hands to tomorrow, which flatters whichever
    policy discharges hardest before midnight.  ``carried_energy_*`` disclose
    that explicitly, using the same terminal valuation the evaluator itself
    applies, so the reader can see what today's figure borrows from tomorrow.
    """
    all_steps = tuple(steps)
    today_steps = first_local_day_steps(all_steps)
    window = summarize_steps(today_steps, config)
    block = _json_safe(asdict(window))
    # Carried energy is measured above each policy's *own* floor, so a row
    # holding a higher reserve is not carrying less by choice. Publish the floor
    # with the figure rather than relying on a footnote to explain the offset.
    block["floor_soc_percent"] = floor_soc_percent
    if settled is None:
        block["whole_day_cash_net_eur"] = None
        block["whole_day_economic_net_eur"] = None
    else:
        block["whole_day_cash_net_eur"] = settled["cash_net_eur"] + window.cash_net_eur
        block["whole_day_economic_net_eur"] = settled["cash_net_eur"] + window.economic_net_eur

    closing = window.closing_soc_percent
    horizon_extends = len(today_steps) < len(all_steps)
    if horizon_extends and closing is not None:
        carried_kwh = max(
            0.0,
            (closing - floor_soc_percent) / 100.0 * config.battery_capacity_kwh,
        )
        block["carried_energy_kwh"] = carried_kwh
        block["carried_energy_value_eur"] = (
            carried_kwh * config.terminal_value_eur_per_dc_kwh
        )
    else:
        block["carried_energy_kwh"] = None
        block["carried_energy_value_eur"] = None
    return block


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    """Build a fully serialisable read-only report for tests and the CLI."""
    plan_path, plan = _read_json(args.plan, label="plan")
    config, assumptions = _build_config(args, plan)
    parsed, inferred_duration_h = _parse_schedule_rows(plan)
    slots = _build_slots(parsed)
    initial_soc = _initial_soc(args, plan)
    winter_reserve = _winter_reserve(args, plan)
    candidates = evaluate_shadow_candidates(
        slots,
        initial_soc_percent=initial_soc,
        config=config,
        winter_reserve_soc_percent=winter_reserve,
    )
    settled = _settled_today(plan)
    baseline_steps = _plan_baseline_steps(parsed, config)
    winter_result = candidates.get("winter_self_sufficiency")
    return {
        "schema_version": 2,
        "read_only": True,
        "plan_path": str(plan_path),
        "plan_generated_at": plan.get("generated_at"),
        "optimizer_mode": plan.get("optimizer_mode"),
        "slot_count": len(slots),
        "slot_duration_h": inferred_duration_h,
        "initial_soc_percent": initial_soc,
        "assumptions": assumptions,
        # Every candidate still plans over the whole known horizon; only the
        # reporting window is today.  Truncating the horizon instead would
        # remove the terminal value of retained energy and let each candidate
        # empty the battery by midnight for free.
        "comparison_basis": (
            "Whole calendar day: the already-metered part of today plus each "
            "policy's planned remainder of today. Candidates still optimize over "
            "the full known horizon; only their reported result is limited to today."
        ),
        "settled_today": settled,
        # Present only when a winter row was produced, so a consumer can tell an
        # absent row from a suppressed one and can always show the caveats next
        # to the figure rather than in separate documentation.
        "winter_candidate": (
            None if winter_result is None else {
                # The evaluator raises the requested reserve to the configured
                # physical minimum when it sits below it, so report the floor the
                # row was actually held above rather than the one asked for.
                "reserve_soc_percent": winter_result.protected_soc_percent,
                "requested_reserve_soc_percent": winter_reserve,
                "reserve_was_raised": (
                    winter_reserve is not None
                    and winter_result.protected_soc_percent > winter_reserve + 1e-9
                ),
                "is_approximation": True,
                "engine_module": "lib.ai_powered_ess_winter",
                "caveats": list(WINTER_APPROXIMATION_CAVEATS),
            }
        ),
        "plan_baseline": {
            "available": baseline_steps is not None,
            "unavailable_reason": (
                None if baseline_steps is not None
                else "plan schedule rows lack grid_energy/soc_start/soc_end"
            ),
            "today": (
                _today_block(
                    baseline_steps, config, settled,
                    # The live optimizer's own planning reserve.
                    floor_soc_percent=config.min_soc_percent,
                )
                if baseline_steps is not None else None
            ),
        },
        "candidates": {
            candidate_id: {
                **_candidate_report(result),
                "today": _today_block(
                    result.schedule, config, settled,
                    floor_soc_percent=result.protected_soc_percent,
                ),
            }
            for candidate_id, result in candidates.items()
        },
    }


def _eur(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2f} €"


def _print_today_row(label: str, today: Mapping[str, Any] | None) -> None:
    if today is None:
        print(f"  {label}: unavailable for this plan")
        return
    closing = today.get("closing_soc_percent")
    carried_kwh = today.get("carried_energy_kwh")
    carried = (
        "" if carried_kwh is None
        else (f", carries {carried_kwh:.1f} kWh into tomorrow "
              f"(worth {_eur(today.get('carried_energy_value_eur'))})")
    )
    print(
        f"  {label}: Grid result {_eur(today['whole_day_cash_net_eur'])}, "
        f"After battery wear {_eur(today['whole_day_economic_net_eur'])}, "
        f"remaining-day grid import/export "
        f"{today['grid_import_kwh']:.2f}/{today['grid_export_kwh']:.2f} kWh, "
        f"battery use {today['dc_throughput_kwh']:.2f} kWh "
        f"({today['full_equivalent_cycles']:.3f} full cycles), "
        f"SoC at midnight {'—' if closing is None else f'{closing:.1f}%'}"
        f"{carried}"
    )


def _print_human(report: Mapping[str, Any], *, include_schedule: bool) -> None:
    print("ESS STRATEGY SHADOW EVALUATION — READ ONLY")
    print("No settings, MQTT, Victron, or live optimizer state were read or changed.")
    print("Net grid result = export reward − import cost. "
          "After battery wear subtracts the estimated battery cycle cost.")
    print(f"Plan: {report['plan_path']} | slots: {report['slot_count']} "
          f"× {report['slot_duration_h']:.3g}h | initial SoC: {report['initial_soc_percent']:.1f}%")
    print(report["comparison_basis"])
    settled = report.get("settled_today")
    if settled is None:
        print("Today's settled totals are missing from this plan, so only the "
              "planned remainder of today is shown below.")
    else:
        print(f"Settled so far today (identical in every row below): "
              f"{_eur(settled['cash_net_eur'])}.")
    assumptions = report["assumptions"]
    defaults = assumptions["defaulted_fields"]
    if defaults:
        print("Research defaults used for: " + ", ".join(defaults))
    else:
        print("All candidate assumptions supplied by the plan/config/CLI (no research defaults).")

    baseline = report.get("plan_baseline") or {}
    live_label = _live_plan_label(report)
    print("Whole-day result per policy (observational; no candidate is selected or executed):")
    if baseline.get("available"):
        _print_today_row(live_label, baseline.get("today"))
    else:
        print(f"  {live_label}: unavailable — {baseline.get('unavailable_reason')}")
    for candidate_id, candidate in report["candidates"].items():
        label = DISPLAY_CANDIDATE_NAMES.get(candidate_id, candidate_id)
        if not candidate["feasible"]:
            print(f"  {label}: INFEASIBLE — {candidate['rejection_reason']}")
            continue
        _print_today_row(label, candidate.get("today"))
        if include_schedule:
            for step in candidate["schedule"]:
                print(
                    f"    {step['start']}: {step['soc_start_percent']:.1f}% → "
                    f"{step['soc_end_percent']:.1f}%, grid {step['grid_energy_kwh']:+.3f} kWh"
                )

    # Rows in the same order as the table above, so this reads as a caption for
    # it rather than a second, separately-ordered list.
    print("What drives each strategy:")
    print(f"  {live_label}: {LIVE_PLAN_LEGEND}")
    for candidate_id in report["candidates"]:
        legend = CANDIDATE_LEGEND.get(candidate_id)
        if legend:
            label = DISPLAY_CANDIDATE_NAMES.get(candidate_id, candidate_id)
            print(f"  {label}: {legend}")

    winter = report.get("winter_candidate")
    if winter:
        raised = ""
        if winter.get("reserve_was_raised"):
            raised = (f" (raised from the requested "
                      f"{winter['requested_reserve_soc_percent']:.0f}% to the "
                      f"configured minimum)")
        print(f"The Winter-style row is an approximation held above a "
              f"{winter['reserve_soc_percent']:.0f}% reserve{raised}, not the "
              f"{winter['engine_module']} engine. It differs in that it:")
        for caveat in winter["caveats"]:
            print(f"  - {caveat}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(args)
    except EvaluationInputError as error:
        print(f"ESS strategy evaluation failed: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        # CandidateConfig/evaluator errors are input-validation failures too.
        print(f"ESS strategy evaluation failed: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report, include_schedule=args.include_schedule)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
