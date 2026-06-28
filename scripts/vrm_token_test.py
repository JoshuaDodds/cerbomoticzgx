#!/usr/bin/env python3
"""
VRM access-token auth TEST (read-only).

Victron deprecated VRM username/password auth on 2026-06-01 in favour of personal
access tokens. This script verifies that an access token works for the exact calls
cerbomoticzGx makes, using the supported header:

    X-Authorization: Token <token>      (single space; "Token", not "Bearer")

It is strictly read-only — it only issues GET requests to the VRM API and writes
nothing to the Victron system or to local state.

Usage:
    python3 scripts/vrm_token_test.py                 # use VRM_API_TOKEN from .secrets/.env
    python3 scripts/vrm_token_test.py --token <TOKEN>  # test a specific token
"""
import sys
import os
import argparse
from datetime import datetime, timedelta

sys.path.append(os.getcwd())

import pytz
import requests

from lib.config_retrieval import retrieve_setting

API_URL = retrieve_setting('VRM_API_URL') or "https://vrmapi.victronenergy.com/v2"
SITE_ID = retrieve_setting('VRM_SITE_ID')
TIMEZONE = pytz.timezone(retrieve_setting('TIMEZONE') or "UTC")
BANNER = "=" * 78


def _hdr(token):
    return {'Content-Type': 'application/json', 'x-authorization': f'Token {token}'}


def main():
    ap = argparse.ArgumentParser(description="Test VRM access-token auth (read-only).")
    ap.add_argument("--token", default=None, help="Access token to test (default: VRM_API_TOKEN).")
    args = ap.parse_args()

    token = args.token or retrieve_setting('VRM_API_TOKEN')
    print(BANNER)
    print("VRM ACCESS-TOKEN AUTH TEST (read-only)")
    print(BANNER)
    if not token:
        print("!! No token. Set VRM_API_TOKEN in .secrets or pass --token <TOKEN>.")
        return 1

    masked = f"{token[:6]}…{token[-4:]} (len {len(token)})"
    print(f"API base : {API_URL}")
    print(f"Site ID  : {SITE_ID!r}")
    print(f"Token    : {masked}")
    print(f"Header   : X-Authorization: Token <token>")
    print("-" * 78)

    ok = True

    # 1) Basic identity check — proves the token authenticates at all.
    try:
        r = requests.get(f"{API_URL}/users/me", headers=_hdr(token), timeout=10)
        print(f"[1] GET /users/me            -> HTTP {r.status_code}")
        if r.status_code == 200:
            me = r.json().get("user", {})
            print(f"    authenticated as: {me.get('name')} <{me.get('email')}> (id {me.get('idUser')})")
        else:
            ok = False
            print(f"    body: {r.text[:300]}")
    except requests.RequestException as e:
        ok = False
        print(f"[1] GET /users/me            -> ERROR {e}")

    # 2) The ACTUAL call cerbomoticzGx makes: 2-day solar/consumption forecast.
    if SITE_ID:
        now_tz = datetime.now(TIMEZONE)
        start = int(now_tz.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        end = int((now_tz + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        url = f"{API_URL}/installations/{SITE_ID}/stats"
        params = {'type': 'forecast', 'start': start, 'end': end, 'interval': 'days'}
        try:
            r = requests.get(url, headers=_hdr(token), params=params, timeout=10)
            print(f"[2] GET installations/stats  -> HTTP {r.status_code}")
            if r.status_code == 200:
                recs = r.json().get("records", {})
                has_solar = bool(recs.get('solar_yield_forecast'))
                print(f"    forecast records present: solar_yield_forecast={has_solar}, "
                      f"keys={list(recs.keys())[:6]}")
                if not has_solar:
                    print("    (auth OK, but no solar forecast in payload — check the site has the feature)")
            else:
                ok = False
                print(f"    body: {r.text[:300]}")
        except requests.RequestException as e:
            ok = False
            print(f"[2] GET installations/stats  -> ERROR {e}")
    else:
        print("[2] skipped — VRM_SITE_ID not set.")

    print("-" * 78)
    print("RESULT:", "TOKEN WORKS ✅" if ok else "TOKEN FAILED ❌ (see above)")
    print(BANNER)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
