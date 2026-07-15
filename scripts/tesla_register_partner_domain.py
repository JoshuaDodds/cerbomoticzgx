#!/usr/bin/env python3
"""Register a partner DOMAIN with the Tesla Fleet API (one-time).

The fleet-telemetry receiver's hostname must live under a domain registered to your
partner account. This is NOT the OAuth "Allowed Origins/Redirect" list in the developer
portal (that's for the browser login flow) — it's the Fleet API partner_accounts endpoint.

This script:
  1. gets a PARTNER token via the client_credentials grant (client id/secret from settings), and
  2. POSTs {"domain": <domain>} to <base>/api/1/partner_accounts.

Tesla then fetches your public key from
  https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem
to verify you control the domain, so that endpoint must be live first.

Usage:
    python scripts/tesla_register_partner_domain.py --domain fleet.hs.mfis.net
"""
import os
import sys
import json
import argparse

sys.path.append(os.getcwd())

TESLA_AUTH_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
# Partner-token scopes. Override with --scope if your app is granted a different set.
DEFAULT_SCOPE = "vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True, help="e.g. fleet.hs.mfis.net")
    ap.add_argument("--scope", default=DEFAULT_SCOPE)
    ap.add_argument("--dry-run", action="store_true", help="get the token but don't register")
    args = ap.parse_args()

    import requests
    from lib.config_retrieval import retrieve_setting
    client_id = retrieve_setting("TESLA_FLEET_CLIENT_ID")
    client_secret = retrieve_setting("TESLA_FLEET_CLIENT_SECRET")
    base_url = (retrieve_setting("TESLA_FLEET_API_BASE_URL") or "").rstrip("/")
    if not (client_id and client_secret and base_url):
        print("ERROR: missing TESLA_FLEET_CLIENT_ID/SECRET or TESLA_FLEET_API_BASE_URL.", file=sys.stderr)
        return 2

    # 1) Partner (machine-to-machine) token. audience MUST be the regional Fleet API base.
    tok = requests.post(TESLA_AUTH_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": args.scope,
        "audience": base_url,
    }, timeout=30)
    if tok.status_code != 200:
        print(f"token request failed ({tok.status_code}):\n{tok.text}", file=sys.stderr)
        return 1
    access = tok.json()["access_token"]
    print("Got partner token.")

    if args.dry_run:
        return 0

    # 2) Register the domain.
    r = requests.post(f"{base_url}/api/1/partner_accounts",
                      headers={"Authorization": f"Bearer {access}",
                               "Content-Type": "application/json"},
                      json={"domain": args.domain}, timeout=30)
    print(f"register status: {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
