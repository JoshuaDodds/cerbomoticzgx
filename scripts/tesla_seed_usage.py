#!/usr/bin/env python3
"""Reconcile locally-counted Tesla usage with the authoritative developer portal.

Tesla exposes usage only in the developer portal (there is no usage API), and our local
counter's primary job is to enforce the spend guard. Run this with one portal Billing & Usage
snapshot so the dashboard matches its category counts; the guard then accumulates new paid
calls and approximate live streaming signals from that dated baseline.

Only the categories you pass are touched — e.g. `--signals 1334` alone leaves command/data/wake
exactly as they were (and vice versa), so you can reconcile one category at a time.

Usage:
    python scripts/tesla_seed_usage.py --commands 13 --data 30 --wakes 2
    python scripts/tesla_seed_usage.py --signals 1334
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
    ap.add_argument("--commands", type=int, default=None)
    ap.add_argument("--data", type=int, default=None)
    ap.add_argument("--wakes", type=int, default=None)
    ap.add_argument("--signals", type=int, default=None,
                    help="set (not add to) this cycle's Streaming Signals count")
    ap.add_argument("--dir", default=None, help="budget state file (default: TESLA_BUDGET_STATE_PATH)")
    args = ap.parse_args()

    path = args.dir or _env_path()
    counts = {}
    if args.commands is not None:
        counts["command"] = args.commands
    if args.data is not None:
        counts["data"] = args.data
    if args.wakes is not None:
        counts["wake"] = args.wakes
    if not counts and args.signals is None:
        ap.error("nothing to seed — pass at least one of --commands/--data/--wakes/--signals")

    snap = tb.reconcile_usage(counts, signals=args.signals, state_path=path)
    print(f"Reconciled {path} for {snap['month']}:")
    for cat, v in snap["categories"].items():
        print(f"  {cat:<8} {v['count']:>5}   €{v['cost']:.3f}")
    sig = snap["streaming"]
    print(f"  {'signals':<8} {sig['count']:>5}   €{sig['cost']:.3f}  (~ after baseline)")
    print(f"  total this cycle: €{snap['total']:.2f} of €{snap['monthly_credit']:.0f}")
    print(f"  reconciled at: {snap['reconciled_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
