#!/usr/bin/env python3
"""Reconcile the locally-counted Tesla API usage with the developer portal.

Tesla exposes usage only in the developer portal (there is no usage API), and our local
counter is a forward-only estimate whose real job is to ENFORCE the hard spend cap. Run this
with the portal's current Billing & Usage numbers (e.g. at the start of a billing cycle) so
the dashboard total matches; the guard then accumulates new calls from that baseline.

Usage:
    python scripts/tesla_seed_usage.py --commands 13 --data 30 --wakes 2
"""
import os
import sys
import argparse

sys.path.append(os.getcwd())

from lib import tesla_budget as tb   # noqa: E402


def _env_path():
    try:
        from dotenv import dotenv_values
        return dotenv_values(".env").get("TESLA_BUDGET_STATE_PATH") or tb.DEFAULT_STATE_PATH
    except Exception:
        return tb.DEFAULT_STATE_PATH


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commands", type=int, default=0)
    ap.add_argument("--data", type=int, default=0)
    ap.add_argument("--wakes", type=int, default=0)
    ap.add_argument("--dir", default=None, help="budget state file (default: TESLA_BUDGET_STATE_PATH)")
    args = ap.parse_args()

    path = args.dir or _env_path()
    snap = tb.seed_month_usage(
        {"command": args.commands, "data": args.data, "wake": args.wakes}, path)
    print(f"Seeded {path} for {snap['month']}:")
    for cat, v in snap["categories"].items():
        print(f"  {cat:<8} {v['count']:>5}   €{v['cost']:.3f}")
    print(f"  total this cycle: €{snap['total']:.2f} of €{snap['monthly_credit']:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
