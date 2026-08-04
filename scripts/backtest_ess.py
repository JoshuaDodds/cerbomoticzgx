#!/usr/bin/env python3
"""Side-effect-free ESS optimizer comparison for representative winter days.

The replay is deterministic and never imports the broker, publishes MQTT, or
writes Victron settings.  It is intended as an activation review aid, not as a
claim that synthetic prices reproduce a particular tariff day exactly.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _quarter_hour_start():
    now = datetime.now().astimezone()
    minute = ((now.minute // 15) + 1) * 15
    if minute == 60:
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now.replace(minute=minute, second=0, microsecond=0)


def representative_winter_inputs(exceptional=False):
    """Return a 48-hour low-PV profile at 15-minute resolution."""
    start = _quarter_hour_start()
    prices = []
    loads = {}
    pv = {}
    for index in range(48 * 4):
        at = start + timedelta(minutes=15 * index)
        hour = at.hour + at.minute / 60.0
        if 2 <= hour < 5:
            price = 0.05 if exceptional else 0.10
        elif 7 <= hour < 9:
            price = 0.32
        elif 17 <= hour < 20:
            price = 1.00 if exceptional else 0.34
        else:
            price = 0.24
        prices.append({"start": at, "total": price, "level": "NORMAL"})

        load_kw = 1.2 if 6 <= hour < 9 or 17 <= hour < 22 else 0.5
        loads[at] = load_kw * 0.25
        # About 3 kWh/day: representative of the measured deep-winter median.
        pv[at] = 0.1875 if 10 <= hour < 18 else 0.0
    return prices, loads, pv


def _module_for(mode):
    if mode == "active":
        return importlib.import_module("lib.ess_optimizer_selector")
    name = "lib.ai_powered_ess_winter" if mode == "winter" else "lib.ai_powered_ess"
    return importlib.import_module(name)


def _invariant_errors(result, engine):
    errors = []
    schedule = result.get("schedule") or []
    victron_slots = result.get("victron_slots") or []
    if len(victron_slots) > 5:
        errors.append("more than five Victron charge windows")
    for previous, current in zip(schedule, schedule[1:]):
        if abs(float(previous["soc_end"]) - float(current["soc_start"])) > 1e-6:
            errors.append(f"SoC discontinuity at {current['time']}")
            break
    for step in schedule:
        start = float(step["soc_start"])
        end = float(step["soc_end"])
        if not (0 <= start <= 100 and 0 <= end <= 100):
            errors.append(f"SoC outside physical bounds at {step['time']}")
            break
        slot_h = float(result.get("slot_duration_h") or 0.25)
        grid_energy = float(step.get("grid_energy") or 0.0)
        grid_kw = abs(grid_energy) / slot_h
        limit = engine.max_power_import if grid_energy >= 0 else engine.max_power_export
        if grid_kw > limit + 1e-5:
            errors.append(f"grid power limit exceeded at {step['time']}")
            break
        dc_change = (end - start) / 100.0 * engine.battery_capacity
        battery_limit = (
            engine.max_charge_power if dc_change >= 0 else engine.max_discharge_power
        )
        if abs(dc_change) / slot_h > battery_limit + 1e-5:
            errors.append(f"battery power limit exceeded at {step['time']}")
            break
        expected_grid = float(step.get("load") or 0.0) - float(step.get("pv") or 0.0)
        expected_grid += (
            dc_change / engine.charge_efficiency
            if dc_change >= 0 else dc_change * engine.discharge_efficiency
        )
        if abs(expected_grid - grid_energy) > 2e-4:
            errors.append(f"energy balance mismatch at {step['time']}")
            break
    ordered = sorted(victron_slots, key=lambda slot: slot["start"])
    for slot in ordered:
        if int(slot["duration"]) <= 0:
            errors.append("non-positive Victron slot duration")
        if float(slot["target_soc"]) > engine.max_grid_charge_soc + 1e-6:
            errors.append("Victron target exceeds user grid-charge cap")
    for previous, current in zip(ordered, ordered[1:]):
        if previous["start"] + timedelta(seconds=previous["duration"]) > current["start"]:
            errors.append("overlapping Victron charge windows")
    for step in schedule:
        if step.get("control_action") != "BUY":
            continue
        if not any(
            slot["start"] <= step["time"]
            < slot["start"] + timedelta(seconds=slot["duration"])
            for slot in ordered
        ):
            errors.append(f"BUY is not covered by a Victron slot at {step['time']}")
            break
    return errors


def replay(mode, start_soc, exceptional):
    module = _module_for(mode)
    engine = module.OptimizationEngine()
    prices, loads, pv = representative_winter_inputs(exceptional)
    result = engine.optimize(start_soc, prices, loads, pv)
    if not result:
        return {"mode": mode, "feasible": False, "errors": ["optimizer returned no plan"]}

    schedule = result["schedule"]
    with_ess = sum(
        max(0.0, step["grid_energy"]) * step["price"]
        - max(0.0, -step["grid_energy"]) * step.get("sell", step["price"])
        for step in schedule
    )
    without_ess = sum(
        max(0.0, step.get("load", 0.0) - step.get("pv", 0.0)) * step["price"]
        - max(0.0, step.get("pv", 0.0) - step.get("load", 0.0))
        * step.get("sell", step["price"])
        for step in schedule
    )
    return {
        "mode": mode,
        "feasible": True,
        "scenario": "exceptional" if exceptional else "ordinary",
        "start_soc_percent": start_soc,
        "final_soc_percent": schedule[-1]["soc_end"],
        "net_cost_eur": round(with_ess, 3),
        "no_ess_net_cost_eur": round(without_ess, 3),
        "savings_eur": round(without_ess - with_ess, 3),
        "charge_windows": len(result.get("victron_slots") or []),
        "active_export_slots": sum(
            step.get("control_action") == "SELL" for step in schedule
        ),
        "selected_candidate": (result.get("winter_policy") or {}).get("selected_candidate"),
        "warning": (result.get("winter_policy") or {}).get("warning"),
        "runtime_optimizer_mode": result.get("optimizer_mode", mode),
        "errors": _invariant_errors(result, engine),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("active", "summer", "winter", "compare"), default="compare",
        help="Engine to replay; compare runs the isolated summer and winter engines.",
    )
    parser.add_argument("--soc", type=float, default=50.0, help="Starting SoC percent.")
    parser.add_argument(
        "--exceptional", action="store_true",
        help="Use the obvious high-spread scenario instead of the ordinary winter curve.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    args = parser.parse_args()

    modes = ("summer", "winter") if args.mode == "compare" else (args.mode,)
    reports = [replay(mode, args.soc, args.exceptional) for mode in modes]
    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        print("ESS WINTER REPLAY — read-only; no MQTT or Victron writes")
        for report in reports:
            print(
                f"{report['mode']:>7}: feasible={report['feasible']} "
                f"net=€{report.get('net_cost_eur', 0):.3f} "
                f"saving=€{report.get('savings_eur', 0):.3f} "
                f"final={report.get('final_soc_percent', 0):.1f}% "
                f"charge_windows={report.get('charge_windows', 0)} "
                f"exports={report.get('active_export_slots', 0)} "
                f"candidate={report.get('selected_candidate') or 'n/a'}"
            )
            for error in report["errors"]:
                print(f"         INVARIANT FAILURE: {error}")
    return 1 if any(report["errors"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
