#!/usr/bin/env python3
"""
AI ESS optimizer DRY RUN.

Prints the charge/discharge plan the AI scheduling algorithm WOULD apply for the
upcoming period, using the current live data available to the system (battery
SoC, Tibber prices, and the PV-remaining forecast). It is strictly read-only:
it does NOT publish setpoints, charge schedules, or feed-in limits to the
Victron bus, so it is safe to run against the live system at any time.

Usage:
    python3 scripts/ai_ess_dryrun.py            # use live battery SoC from STATE
    python3 scripts/ai_ess_dryrun.py --soc 62   # override starting SoC (%)
    python3 scripts/ai_ess_dryrun.py --json      # also dump the raw result as JSON
"""
import sys
import os
import argparse
import json
from datetime import datetime

sys.path.append(os.getcwd())

from lib.global_state import GlobalStateClient
from lib.tibber_api import get_all_price_points
from lib.ai_powered_ess import OptimizationEngine, format_plan_summary
from lib.energy_broker import (
    _build_pv_forecast_by_slot,
    _build_load_forecast_by_slot,
    _grid_assist_setpoint_watts,
    get_today_energy_actuals,
)

STATE = GlobalStateClient()

BANNER = "=" * 78


def main():
    parser = argparse.ArgumentParser(description="AI ESS optimizer dry run (read-only).")
    parser.add_argument("--soc", type=float, default=None,
                        help="Override starting battery SoC in percent (default: live STATE value).")
    parser.add_argument("--json", action="store_true", help="Also print the raw result as JSON.")
    parser.add_argument("--hours", type=float, default=None,
                        help="Limit the per-slot table to the next N hours (default: full horizon).")
    args = parser.parse_args()

    print(BANNER)
    print("AI ESS OPTIMIZER — DRY RUN (read-only, no changes written to Victron)")
    print(BANNER)

    # --- Inputs -------------------------------------------------------------
    if args.soc is not None:
        batt_soc = args.soc
        soc_source = "CLI override"
    else:
        batt_soc = STATE.get("batt_soc")
        soc_source = "live STATE"

    # A real 0% SoC is valid (STATE returns 0 for both 0% and missing); use
    # battery voltage to tell them apart so a true 0% doesn't block the dry run.
    battery_reporting = bool(STATE.get("batt_voltage")) or args.soc is not None
    if batt_soc is None or (batt_soc == 0 and not battery_reporting):
        print(f"!! Battery data unavailable from {soc_source} (SoC={batt_soc!r}, no voltage).")
        print("   Pass --soc <percent> to run with an assumed value.")
        return 1

    prices = get_all_price_points()
    if not prices:
        print("!! No Tibber price points available. Is TIBBER_UPDATES_ENABLED=1 and the feed live?")
        return 1

    # Mirror run_ai_optimizer()'s PV forecast construction.
    from lib.ai_powered_ess import _coerce_datetime
    normalised_slots = []
    for p in prices:
        try:
            normalised_slots.append({"start": _coerce_datetime(p["start"])})
        except (KeyError, TypeError, ValueError):
            continue
    slot_duration_h = 1.0
    if len(normalised_slots) > 1:
        normalised_slots.sort(key=lambda x: x["start"])
        gaps = [
            (normalised_slots[i]["start"] - normalised_slots[i - 1]["start"]).total_seconds()
            for i in range(1, len(normalised_slots))
        ]
        positive_gaps = [g for g in gaps if g > 0]
        if positive_gaps:
            slot_duration_h = min(positive_gaps) / 3600.0

    pv_forecast = _build_pv_forecast_by_slot(normalised_slots, slot_duration_h)
    load_forecast = _build_load_forecast_by_slot(normalised_slots, slot_duration_h)

    pv_remaining = STATE.get("pv_projected_remaining")
    horizon_load_kwh = sum(load_forecast.values()) if load_forecast else 0.0
    print(f"Native price resolution: ~{slot_duration_h:.2f}h | PV remaining: {pv_remaining} Wh "
          f"-> {len(pv_forecast)} daylight slots | forecast house load (horizon): {horizon_load_kwh:.1f} kWh")

    # --- Engine tunables (what the optimizer ACTUALLY loaded at runtime) -----
    # Surfaced so we can confirm .env values (e.g. ESS_BATTERY_CYCLE_COST) are
    # really reaching the DP rather than silently falling back to defaults.
    engine = OptimizationEngine()
    print("-" * 78)
    print("ENGINE TUNABLES (resolved via .secrets > STATE > .env):")
    print(f"  battery_cycle_cost   : {engine.cycle_cost:.4f} €/kWh   "
          f"(.env ESS_BATTERY_CYCLE_COST)")
    print(f"  export_price_factor  : {engine.export_price_factor:.3f}")
    print(f"  export_fee           : {getattr(engine, 'export_fee', 0.0):.4f} €/kWh")
    print(f"  min_sell_price       : {engine.min_sell_price:.3f} €/kWh")
    print(f"  expected_peak_price  : {engine.expected_peak_price:.3f} €/kWh")
    print(f"  terminal_value_factor: {getattr(engine, 'terminal_value_factor', 1.0):.3f}")
    print(f"  min_soc_reserve      : {engine.min_soc:.1f} %   soc_step: {engine.soc_step:.1f} %")
    print(f"  max charge/discharge : {engine.max_charge_power:.1f} / {engine.max_discharge_power:.1f} kW")
    print(f"  max import/export    : {engine.max_power_import:.1f} / {engine.max_power_export:.1f} kW")
    print("-" * 78)

    # --- Optimize -----------------------------------------------------------
    t0 = datetime.now()
    result = engine.optimize(batt_soc, prices, load_forecast, pv_forecast)
    elapsed = (datetime.now() - t0).total_seconds()

    if not result:
        print("!! Optimizer returned no feasible plan. Check reserve/power limits and price data.")
        return 1

    # Would-be applied setpoint (PV-aware for HOLD), matching the live service.
    if result.get('grid_assist'):  # HOLD
        applied_setpoint = _grid_assist_setpoint_watts()
    else:
        applied_setpoint = result.get('setpoint', 0.0)

    # Same plan view as the live service log.
    print(format_plan_summary(result, batt_soc=batt_soc, source=soc_source,
                              price_points=len(prices), pv_remaining=pv_remaining,
                              max_hours=args.hours, today_actuals=get_today_energy_actuals(),
                              applied_setpoint=applied_setpoint))
    print(f"Optimizer runtime      : {elapsed:.3f}s")
    print(BANNER)

    if args.json:
        serialisable = {
            "mode": result["mode"],
            "reason": result.get("reason"),
            "reason_code": result.get("reason_code"),
            "current_price": result["current_price"],
            "setpoint": result["setpoint"],
            "applied_setpoint": applied_setpoint,
            "grid_assist": result.get("grid_assist"),
            "limit_feed_in": result["limit_feed_in"],
            "victron_slots": [
                {"start": s["start"].isoformat(), "duration": s["duration"], "target_soc": s["target_soc"]}
                for s in result["victron_slots"]
            ],
            "schedule": [
                {**step, "time": step["time"].isoformat()} for step in result["schedule"]
            ],
        }
        print(json.dumps(serialisable, indent=2))

    print("DRY RUN COMPLETE — nothing was written to the Victron system.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
