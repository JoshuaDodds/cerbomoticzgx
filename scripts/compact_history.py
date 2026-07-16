#!/usr/bin/env python3
"""Compact ESS history NDJSON day files into immutable per-month Parquet.

The store keeps the current month as hot append-only NDJSON and rolls *complete past
months* into one ZSTD-compressed Parquet each (via DuckDB). Reads are transparent —
`history_store.read_day` serves either format — so this is safe to run any time.

Because Parquet files are written to a temp file, verified, then atomically renamed,
and the source NDJSON is only removed after that succeeds, an interruption never loses
or corrupts data (re-running simply resumes).

Usage:
    python scripts/compact_history.py --status            # show what's stored, in which format
    python scripts/compact_history.py --dry-run           # list past months that would compact
    python scripts/compact_history.py                     # compact all complete past months
    python scripts/compact_history.py --keep-ndjson       # also keep the source NDJSON
    python scripts/compact_history.py --month 2026-05     # just this month
    python scripts/compact_history.py --dir data/history  # override the history directory
"""
import os
import sys
import glob
import argparse
from datetime import date

sys.path.append(os.getcwd())

from lib import history_store as hs   # noqa: E402


def _env_history_dir():
    try:
        from dotenv import dotenv_values
        return (dotenv_values(".env").get("HISTORY_DIR") or "data/history")
    except Exception:
        return "data/history"


def _past_ndjson_months(hist_dir):
    """(year, month) of every NDJSON day file older than the current month."""
    cutoff = (date.today().year, date.today().month)
    months = set()
    for p in glob.glob(os.path.join(hist_dir, "ess-*.ndjson")):
        base = os.path.basename(p)
        m = hs._DAY_RE.search(base)
        if m:
            ym = (int(m.group(1)), int(m.group(2)))
            if ym < cutoff:
                months.add(ym)
    return sorted(months)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None, help="history directory (default: HISTORY_DIR or data/history)")
    ap.add_argument("--month", default=None, help="compact only this month (YYYY-MM)")
    ap.add_argument("--keep-ndjson", action="store_true", help="keep source NDJSON after compaction")
    ap.add_argument("--dry-run", action="store_true", help="show what would be compacted, do nothing")
    ap.add_argument("--status", action="store_true", help="print store status and exit")
    ap.add_argument("--force", action="store_true",
                    help="allow compacting the CURRENT month (unsafe: it's still being appended)")
    args = ap.parse_args()

    hist_dir = args.dir or _env_history_dir()

    if not hs.duckdb_available():
        print("ERROR: duckdb is not installed — cannot compact. `pip install duckdb`.", file=sys.stderr)
        return 2

    if args.status:
        st = hs.store_status(hist_dir)
        print(f"history dir : {st['dir']}")
        print(f"duckdb      : {st['duckdb']}")
        print(f"days total  : {st['days']}  ({st['earliest']} .. {st['latest']})")
        print(f"latest ts   : {st['latest_ts']}")
        print(f"parquet mon : {', '.join(st['parquet_months']) or '—'}")
        print(f"ndjson days : {len(st['ndjson_days'])} hot day file(s)")
        return 0

    remove = not args.keep_ndjson

    current = (date.today().year, date.today().month)
    if args.month:
        y, m = args.month.split("-")
        ym = (int(y), int(m))
        if ym >= current and not args.force:
            print(f"Refusing to compact the current month {y}-{m} (still being appended). "
                  f"Use --force to override.", file=sys.stderr)
            return 2
        months = [ym]
    else:
        months = _past_ndjson_months(hist_dir)   # never includes the current month

    if not months:
        print("Nothing to compact (no complete past-month NDJSON found).")
        return 0

    if args.dry_run:
        print("Would compact (remove_ndjson=%s):" % remove)
        for (y, m) in months:
            n = len(glob.glob(os.path.join(hist_dir, f"ess-{y:04d}-{m:02d}-*.ndjson")))
            print(f"  {y:04d}-{m:02d}  ({n} day files)")
        return 0

    for (y, m) in months:
        out = hs.compact_month(y, m, hist_dir, remove_ndjson=remove)
        if out:
            print(f"compacted {y:04d}-{m:02d} -> {out}")
        else:
            print(f"skipped {y:04d}-{m:02d} (nothing to do)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
