#!/usr/bin/env python3
"""Probe Tibber for the correct month-to-date (MTD) query.

Run this on the ESS host. It reads TIBBER_ACCESS_TOKEN from .secrets/.env (same as
the service) and tries several query shapes for the CURRENT calendar month, printing
each result with the exact date range it covers. Compare the NET values to what the
Tibber app shows for THIS month and tell me which candidate is correct — I'll lock
that one into the dashboard.

    python scripts/tibber_mtd_probe.py
"""
import os
import sys
from datetime import datetime

import requests
from dotenv import dotenv_values

GQL = "https://api.tibber.com/v1-beta/gql"


def _token() -> str:
    for fname in (".secrets", ".env"):
        try:
            v = (dotenv_values(fname) or {}).get("TIBBER_ACCESS_TOKEN")
        except Exception:
            v = None
        if v and v.strip():
            return v.strip()
    return (os.environ.get("TIBBER_ACCESS_TOKEN") or "").strip()


def _run(query: str):
    r = requests.post(
        GQL, json={"query": query},
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        timeout=15,
    )
    try:
        j = r.json()
    except ValueError:
        print(f"   HTTP {r.status_code}: {r.text[:200]}")
        return None
    if j.get("errors"):
        print("   GraphQL errors:", j["errors"])
    return ((((j.get("data") or {}).get("viewer") or {}).get("homes")) or [None])[0]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sum_month(nodes, field, prefix):
    tot, n, lo, hi = 0.0, 0, None, None
    for nd in nodes or []:
        frm = str(nd.get("from") or "")
        if not frm.startswith(prefix):
            continue
        v = _f(nd.get(field))
        if v is None:
            continue
        tot += v
        n += 1
        d = frm[:10]
        lo = d if (lo is None or d < lo) else lo
        hi = d if (hi is None or d > hi) else hi
    return tot, n, lo, hi


def main():
    if not _token():
        print("No TIBBER_ACCESS_TOKEN found in .secrets/.env/environment.")
        sys.exit(1)
    prefix = datetime.now().strftime("%Y-%m-")
    print(f"Current month prefix: {prefix}   (now: {datetime.now().isoformat(timespec='seconds')})\n")

    print("== A) MONTHLY last:2  (what the broken version used) ==")
    h = _run("{ viewer { homes { "
             "consumption(resolution: MONTHLY, last: 2){ nodes { from to cost currency } } "
             "production(resolution: MONTHLY, last: 2){ nodes { from to profit currency } } } } }")
    if h:
        for nd in (h.get("consumption") or {}).get("nodes") or []:
            print(f"   consumption {str(nd.get('from'))[:10]}..{str(nd.get('to'))[:10]}  cost={nd.get('cost')}")
        for nd in (h.get("production") or {}).get("nodes") or []:
            print(f"   production  {str(nd.get('from'))[:10]}..{str(nd.get('to'))[:10]}  profit={nd.get('profit')}")

    for label, res, cap in (("B) DAILY", "DAILY", 24), ("C) HOURLY", "HOURLY", 800)):
        print(f"\n== {label} last:{cap}, summed for the current month ==")
        h = _run("{ viewer { homes { "
                 f"consumption(resolution: {res}, last: {cap}){{ nodes {{ from cost currency }} }} "
                 f"production(resolution: {res}, last: {cap}){{ nodes {{ from profit currency }} }} }} }} }}")
        if not h:
            continue
        c, cn, clo, chi = _sum_month((h.get("consumption") or {}).get("nodes"), "cost", prefix)
        p, pn, plo, phi = _sum_month((h.get("production") or {}).get("nodes"), "profit", prefix)
        unit = "days" if res == "DAILY" else "hrs"
        print(f"   consumption cost   = {c:7.2f}   ({cn} {unit}: {clo}..{chi})")
        print(f"   production  profit = {p:7.2f}   ({pn} {unit}: {plo}..{phi})")
        print(f"   NET (profit-cost)  = {p - c:7.2f}")

    print("\nCompare the NET (and cost/profit) above to the Tibber app's figure for THIS")
    print("month. Tell me which candidate matches — if none do, the bonuses likely settle")
    print("monthly and won't appear mid-month, and we'll decide how to handle that.")


if __name__ == "__main__":
    main()
