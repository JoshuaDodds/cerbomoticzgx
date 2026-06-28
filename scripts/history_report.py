#!/usr/bin/env python3
"""
Roll up the per-cycle ESS history (data/history/ess-YYYY-MM-DD.ndjson) into
analytics-ready summaries:

  * realised daily economics (import/export kWh + €, net profit/cost)
  * the house-load fingerprint (median load by clock hour)
  * the realised PV generation shape by clock hour (for learning this
    installation's unique curve vs the forecast)
  * mode distribution

Stdlib only — no pandas — so it runs anywhere. Human-readable by default;
`--json` emits a machine-readable rollup (the format a future Claude SDK pass
would consume).

Usage:
    python scripts/history_report.py                 # all days found
    python scripts/history_report.py --date 2026-06-15
    python scripts/history_report.py --json
    python scripts/history_report.py --dir data/history
"""
import os
import sys
import json
import glob
import argparse
from collections import defaultdict, Counter
from datetime import datetime
from statistics import median, mean

sys.path.append(os.getcwd())


def _env_history_dir():
    try:
        from dotenv import dotenv_values
        return (dotenv_values(".env").get("HISTORY_DIR") or "data/history")
    except Exception:
        return "data/history"


def load_records(history_dir, only_date=None):
    pattern = f"ess-{only_date}.ndjson" if only_date else "ess-*.ndjson"
    records = []
    for path in sorted(glob.glob(os.path.join(history_dir, pattern))):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _hour(rec):
    try:
        return datetime.fromisoformat(rec["ts"]).hour
    except Exception:
        return None


def _date(rec):
    try:
        return datetime.fromisoformat(rec["ts"]).date().isoformat()
    except Exception:
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_rollup(records):
    by_date = defaultdict(list)
    for r in records:
        d = _date(r)
        if d:
            by_date[d].append(r)

    days = []
    for d in sorted(by_date):
        recs = by_date[d]
        # Daily realised totals are the running cumulative day_* fields (Tibber
        # resets them at midnight), so the day's realised total is their max.
        def _mx(key):
            vals = [_num(r.get(key)) for r in recs]
            vals = [v for v in vals if v is not None]
            return max(vals) if vals else 0.0
        imp_kwh, imp_cost = _mx("day_import_kwh"), _mx("day_import_cost")
        exp_kwh, exp_rev = _mx("day_export_kwh"), _mx("day_export_reward")
        days.append({
            "date": d,
            "records": len(recs),
            "import_kwh": round(imp_kwh, 2),
            "import_cost": round(imp_cost, 2),
            "export_kwh": round(exp_kwh, 2),
            "export_reward": round(exp_rev, 2),
            "net": round(imp_cost - exp_rev, 2),
            "modes": dict(Counter(r.get("mode") for r in recs)),
        })

    # Per-clock-hour aggregates across all loaded records.
    load_by_hour = defaultdict(list)
    pv_by_hour = defaultdict(list)
    price_by_hour = defaultdict(list)
    for r in records:
        h = _hour(r)
        if h is None:
            continue
        load = _num(r.get("load_w"))
        batt = _num(r.get("batt_w"))
        pv = _num(r.get("pv_w"))
        price = _num(r.get("price_buy"))
        # Exclude heavy charge/discharge cycles from the LOAD baseline: ac_out_power
        # is unreliable while the inverter is pushing/pulling big power.
        if load is not None and (batt is None or abs(batt) < 4000):
            load_by_hour[h].append(load)
        if pv is not None:
            pv_by_hour[h].append(pv)
        if price is not None:
            price_by_hour[h].append(price)

    hours = []
    for h in range(24):
        ld = load_by_hour.get(h, [])
        pv = pv_by_hour.get(h, [])
        pr = price_by_hour.get(h, [])
        hours.append({
            "hour": h,
            "load_w_median": round(median(ld)) if ld else None,
            "pv_w_median": round(median(pv)) if pv else None,
            "price_buy_mean": round(mean(pr), 4) if pr else None,
            "samples": len(ld),
        })

    def _action(r):
        return r.get("control_action") or r.get("mode")

    return {"days": days, "hours": hours,
            "actions_overall": dict(Counter(_action(r) for r in records))}


def build_accuracy(settlements):
    """Predicted-vs-actual accuracy from per-slot settlement records."""
    net_abs_err = []
    pv_pred, pv_act = [], []
    rows_by_date = defaultdict(lambda: {"n": 0, "pred_net": 0.0, "act_net": 0.0})
    for r in settlements:
        if r.get("incomplete"):
            continue
        d = _date(r)
        pn, an = _num(r.get("predicted_net_eur")), _num(r.get("actual_net_eur"))
        if pn is not None and an is not None:
            net_abs_err.append(abs(pn - an))
            rows_by_date[d]["n"] += 1
            rows_by_date[d]["pred_net"] += pn
            rows_by_date[d]["act_net"] += an
    days = []
    for d in sorted(rows_by_date):
        v = rows_by_date[d]
        days.append({"date": d, "slots": v["n"],
                     "predicted_net": round(v["pred_net"], 2),
                     "actual_net": round(v["act_net"], 2),
                     "delta": round(v["act_net"] - v["pred_net"], 2)})
    return {
        "settled_slots": len(net_abs_err),
        "net_mae_eur": round(mean(net_abs_err), 4) if net_abs_err else None,
        "days": days,
    }


def print_report(rollup):
    line = "=" * 74
    print(line)
    print("ESS PERFORMANCE HISTORY")
    print(line)

    print("REALISED DAILY ECONOMICS")
    print(f"  {'date':<12}{'import':>16}{'export':>16}{'net':>14}{'slots':>7}")
    for d in rollup["days"]:
        net = d["net"]
        tag = f"€{abs(net):.2f} {'profit' if net < 0 else 'cost'}"
        print(f"  {d['date']:<12}{d['import_kwh']:>7.1f} kWh €{d['import_cost']:>5.2f}"
              f"{d['export_kwh']:>8.1f} kWh €{d['export_reward']:>5.2f}{tag:>14}{d['records']:>7}")
    print(line)

    print("HOUSE-LOAD FINGERPRINT & REALISED PV SHAPE  (median by clock hour)")
    print(f"  {'hour':<6}{'load':>10}{'PV':>10}{'avg buy €':>12}{'n':>6}")
    for h in rollup["hours"]:
        if h["samples"] == 0 and h["pv_w_median"] is None:
            continue
        load = f"{h['load_w_median']} W" if h["load_w_median"] is not None else "—"
        pv = f"{h['pv_w_median']} W" if h["pv_w_median"] is not None else "—"
        price = f"€{h['price_buy_mean']:.3f}" if h["price_buy_mean"] is not None else "—"
        print(f"  {h['hour']:02d}:00{load:>10}{pv:>10}{price:>12}{h['samples']:>6}")
    print(line)

    print("ACTION DISTRIBUTION (cycles)")
    for m, n in sorted(rollup["actions_overall"].items(), key=lambda x: -x[1]):
        print(f"  {str(m):<14}{n}")
    print(line)

    acc = rollup.get("accuracy")
    if acc and acc.get("settled_slots"):
        print("FORECAST ACCURACY  (settled slots: predicted vs actual)")
        mae = acc.get("net_mae_eur")
        print(f"  net €/slot MAE: {('€%.4f' % mae) if mae is not None else '—'}  "
              f"over {acc['settled_slots']} slots")
        for d in acc["days"]:
            print(f"  {d['date']:<12} predicted €{d['predicted_net']:>7.2f}  "
                  f"actual €{d['actual_net']:>7.2f}  Δ €{d['delta']:>7.2f}  ({d['slots']} slots)")
        print(line)
    print("Note: load excludes heavy charge/discharge cycles (|batt|>4kW) where the")
    print("AC-out meter is unreliable. PV median is the realised generation shape —")
    print("compare it to the forecast over time to learn this installation's curve.")
    print(line)


def main():
    ap = argparse.ArgumentParser(description="Summarise ESS history NDJSON logs.")
    ap.add_argument("--dir", default=None, help="history directory (default: HISTORY_DIR or data/history)")
    ap.add_argument("--date", default=None, help="only this date, YYYY-MM-DD")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON rollup")
    args = ap.parse_args()

    history_dir = args.dir or _env_history_dir()
    records = load_records(history_dir, args.date)
    if not records:
        print(f"No history records found in {history_dir}", file=sys.stderr)
        return 1

    # Split the per-cycle decision log from the per-slot settlement records
    # (older files have no "kind" -> treat as cycle).
    cycles = [r for r in records if r.get("kind", "cycle") == "cycle"]
    settlements = [r for r in records if r.get("kind") == "settlement"]

    rollup = build_rollup(cycles)
    rollup["accuracy"] = build_accuracy(settlements)
    if args.json:
        print(json.dumps(rollup, indent=2))
    else:
        print_report(rollup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
