#!/usr/bin/env python3
"""Evaluate recorded weather/HVAC forecast shadows without changing control.

Examples:
    python scripts/validate_forecasts.py --dir data/history
    python scripts/validate_forecasts.py --json
    python scripts/validate_forecasts.py --require-pass

An evidence failure is expected while data is still being collected, so it is
reported as ``KEEP_APPLY_OFF`` and exits successfully by default.  HVAC/load
and PV evidence are independently gated: ``--require-pass`` requires at least
one branch to be ready for a deliberate human review, never automatic enablement.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.forecast_validation import (  # noqa: E402
    HistoryReadError,
    ValidationCriteria,
    analyze_records,
    load_history_records,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="data/history", help="ESS history directory")
    parser.add_argument("--date", help="only one date, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help=("exit 2 unless at least one independently evaluated gate is ready "
              "for a human activation review"),
    )
    parser.add_argument("--min-complete-days", type=int, default=14)
    parser.add_argument("--min-slots-per-day", type=int, default=80)
    parser.add_argument("--min-relative-mae-improvement", type=float, default=0.05)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260805)
    parser.add_argument("--max-bias-degradation-kwh", type=float, default=0.005)
    parser.add_argument("--max-shadow-adjustment-kwh", type=float, default=0.5)
    parser.add_argument("--min-slot-coverage-ratio", type=float, default=0.80)
    return parser


def _criteria(args: argparse.Namespace) -> ValidationCriteria:
    return ValidationCriteria(
        min_complete_days=args.min_complete_days,
        min_slots_per_day=args.min_slots_per_day,
        min_relative_mae_improvement=args.min_relative_mae_improvement,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed,
        max_bias_degradation_kwh=args.max_bias_degradation_kwh,
        max_abs_shadow_adjustment_kwh=args.max_shadow_adjustment_kwh,
        min_slot_coverage_ratio=args.min_slot_coverage_ratio,
    )


def _metric_line(name: str, report: dict) -> str:
    baseline = (report.get("baseline") or {}).get("mae_kwh")
    shadow = (report.get("shadow") or {}).get("mae_kwh")
    days = report.get("eligible_days", 0)
    slots = report.get("eligible_slots", 0)
    gate = report.get("gate") or {}
    recommendation = gate.get("recommendation", "KEEP_APPLY_OFF")
    decision = "Keep forecast adjustment OFF" if recommendation == "KEEP_APPLY_OFF" else recommendation
    return (
        f"{name}: current forecast MAE={baseline if baseline is not None else '—'} kWh/slot, "
        f"weather/HVAC trial MAE={shadow if shadow is not None else '—'} kWh/slot, "
        f"{days} complete day(s) / {slots} slot(s), "
        f"{decision}"
    )


def _print_human(report: dict) -> None:
    print("FORECAST VALIDATION — read-only; no optimizer settings were changed")
    print("Current forecast = the forecast used by the optimizer.")
    print("Weather/HVAC trial = the comparison forecast; it is not applied.")
    print(_metric_line("Load", report["load"]))
    matched = report["pv"]["matched"]
    if matched.get("status") == "ready":
        print(_metric_line("PV matched final-nowcast", matched))
    else:
        recommendation = (matched.get("gate", {}) or {}).get("recommendation", "KEEP_APPLY_OFF")
        decision = "Keep forecast adjustment OFF" if recommendation == "KEEP_APPLY_OFF" else recommendation
        print(
            "PV matched final-nowcast: "
            f"{matched.get('status')} — {decision}"
        )
    raw = report["pv"]["raw_pre_nowcast_diagnostic"]
    print(_metric_line("PV raw diagnostic only", raw))
    overall = report["overall"]["recommendation"]
    decision = "Keep forecast adjustments OFF" if overall == "KEEP_APPLY_OFF" else overall
    print(f"Overall: {decision}")
    for reason in report["load"]["gate"]["reasons"]:
        print(f"  load: {reason}")
    for reason in matched.get("gate", {}).get("reasons", []):
        print(f"  pv: {reason}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        criteria = _criteria(args)
    except ValueError as error:
        print(f"Invalid validation criteria: {error}", file=sys.stderr)
        return 2
    try:
        records = load_history_records(args.dir, args.date)
    except HistoryReadError as error:
        print(f"Cannot safely read forecast-validation history: {error}", file=sys.stderr)
        return 2
    report = analyze_records(records, criteria=criteria)
    report["history_dir"] = str(args.dir)
    report["date_filter"] = args.date
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    if args.require_pass and not report["overall"]["pass"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
