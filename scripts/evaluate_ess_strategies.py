#!/usr/bin/env python3
"""Compare read-only ESS strategy candidates against an exported AI plan.

This command deliberately does *not* import the energy broker, settings,
GlobalState, MQTT, or Victron modules.  It only reads a JSON plan, evaluates
the pure candidate model, and writes its report to stdout.

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
    CandidateConfig,
    DeterministicSlot,
    evaluate_shadow_candidates,
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
}


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


def _build_slots(plan: Mapping[str, Any]) -> tuple[tuple[DeterministicSlot, ...], float]:
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

    duration = _duration_from_plan(plan, rows, starts)
    indexed: list[tuple[datetime, DeterministicSlot]] = []
    for index, (row, start) in enumerate(zip(rows, starts)):
        row_duration = row.get("duration_h")
        if row_duration is not None:
            effective_duration = _number(row_duration, label=f"schedule[{index}].duration_h")
            if effective_duration <= 0:
                raise EvaluationInputError(f"schedule[{index}].duration_h must be positive")
        else:
            effective_duration = duration
        indexed.append((
            start,
            DeterministicSlot(
                start=start,
                duration_h=effective_duration,
                buy_price=_number(
                    _slot_field(row, index, "price", "buy_price", required_name="buy price"),
                    label=f"schedule[{index}].price",
                ),
                sell_price=(
                    _number(row["sell"], label=f"schedule[{index}].sell")
                    if row.get("sell") is not None
                    else _number(row["sell_price"], label=f"schedule[{index}].sell_price")
                    if row.get("sell_price") is not None
                    else None
                ),
                load_kwh=_number(
                    _slot_field(row, index, "load", "load_kwh", required_name="load energy"),
                    label=f"schedule[{index}].load",
                ),
                pv_kwh=_number(
                    _slot_field(row, index, "pv", "pv_kwh", required_name="PV energy"),
                    label=f"schedule[{index}].pv",
                ),
            ),
        ))
    indexed.sort(key=lambda item: item[0])
    for previous, current in zip(indexed, indexed[1:]):
        if current[0] == previous[0]:
            raise EvaluationInputError(f"plan.schedule contains duplicate timestamp {current[0].isoformat()}")
    return tuple(slot for _, slot in indexed), duration


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


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    """Build a fully serialisable read-only report for tests and the CLI."""
    plan_path, plan = _read_json(args.plan, label="plan")
    config, assumptions = _build_config(args, plan)
    slots, inferred_duration_h = _build_slots(plan)
    initial_soc = _initial_soc(args, plan)
    candidates = evaluate_shadow_candidates(
        slots,
        initial_soc_percent=initial_soc,
        config=config,
    )
    return {
        "schema_version": 1,
        "read_only": True,
        "plan_path": str(plan_path),
        "plan_generated_at": plan.get("generated_at"),
        "optimizer_mode": plan.get("optimizer_mode"),
        "slot_count": len(slots),
        "slot_duration_h": inferred_duration_h,
        "initial_soc_percent": initial_soc,
        "assumptions": assumptions,
        "candidates": {
            candidate_id: _candidate_report(result)
            for candidate_id, result in candidates.items()
        },
    }


def _print_human(report: Mapping[str, Any], *, include_schedule: bool) -> None:
    print("ESS STRATEGY SHADOW EVALUATION — READ ONLY")
    print("No settings, MQTT, Victron, or live optimizer state were read or changed.")
    print("Net grid result = export reward − import cost. "
          "After battery wear subtracts the estimated battery cycle cost.")
    print(f"Plan: {report['plan_path']} | slots: {report['slot_count']} "
          f"× {report['slot_duration_h']:.3g}h | initial SoC: {report['initial_soc_percent']:.1f}%")
    assumptions = report["assumptions"]
    defaults = assumptions["defaulted_fields"]
    if defaults:
        print("Research defaults used for: " + ", ".join(defaults))
    else:
        print("All candidate assumptions supplied by the plan/config/CLI (no research defaults).")
    print("Candidate results (observational; no candidate is selected or executed):")
    for candidate_id, candidate in report["candidates"].items():
        label = DISPLAY_CANDIDATE_NAMES.get(candidate_id, candidate_id)
        if not candidate["feasible"]:
            print(f"  {label}: INFEASIBLE — {candidate['rejection_reason']}")
            continue
        print(
            f"  {label}: Grid result {candidate['cash_net_eur']:+.2f} €, "
            f"After battery wear {candidate['economic_net_eur']:+.2f} €, "
            f"grid import/export {candidate['grid_import_kwh']:.2f}/{candidate['grid_export_kwh']:.2f} kWh, "
            f"battery use {candidate['dc_throughput_kwh']:.2f} kWh "
            f"({candidate['full_equivalent_cycles']:.3f} full cycles), "
            f"ending SoC {candidate['terminal_soc_percent']:.1f}%"
        )
        if include_schedule:
            for step in candidate["schedule"]:
                print(
                    f"    {step['start']}: {step['soc_start_percent']:.1f}% → "
                    f"{step['soc_end_percent']:.1f}%, grid {step['grid_energy_kwh']:+.3f} kWh"
                )


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
