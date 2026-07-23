#!/usr/bin/env python3
"""Push the Fleet Telemetry config to the vehicle so it starts streaming to our receiver.

Run ONCE after the fleet-telemetry server is deployed and reachable at the hostname/port
(and the EdgeRouter port-forward is in place). Reads Tesla credentials from the app
settings/.secrets; VIN comes from --vin or the TESLA_TELEMETRY_VIN / TESLA_FLEET_VIN setting.

The --ca file must be the CA chain that signed the server's (wildcard) TLS cert, so the car
trusts our endpoint. mTLS client verification (the car's cert) is handled server-side by
fleet-telemetry using Tesla's vehicle CA that is BUILT INTO the binary — we supply nothing.

Usage:
    python scripts/tesla_push_telemetry_config.py \
        --host fleet.hs.mfis.net --port 6443 \
        --ca /path/to/hs-mfis-net-ca-chain.pem --vin 5YJ...
"""
import os
import sys
import json
import argparse

sys.path.append(os.getcwd())

# Complete charging-focused field set for smart solar amp-limiting + scheduling. Everything is
# change-only (with a minimum interval / delta), so an idle/parked car emits almost nothing and
# cost stays near zero. This is intended to be set-and-forget — it covers every value the EV
# controller, home geofence, and dashboard consume, so we don't have to re-push to add fields.
FIELDS = {
    # --- plugged + charge state: the trigger for everything (on-change, up to 1 Hz) ---
    "DetailedChargeState": {"interval_seconds": 1},   # Disconnected/Charging/Stopped/Complete/...
    "ChargePortLatch": {"interval_seconds": 1},        # Engaged/Disengaged = connector seated
    "FastChargerPresent": {"interval_seconds": 5},     # supercharging -> never command charge

    # --- location -> is_home geofence (matches HOME_ADDRESS_LAT/LONG); silent while parked ---
    "Location": {"interval_seconds": 10},

    # --- amp-rate control: actual, requested, and the ceiling the car will accept ---
    "ChargeAmps": {"interval_seconds": 5, "minimum_delta": 0.5},              # actual amps drawn
    "ChargeCurrentRequest": {"interval_seconds": 5, "minimum_delta": 0.5},    # requested amps
    "ChargeCurrentRequestMax": {"interval_seconds": 30},                      # max amps accepted
    "ACChargingPower": {"interval_seconds": 5, "minimum_delta": 0.1},         # kW
    "ChargerVoltage": {"interval_seconds": 30, "minimum_delta": 2},
    "ChargerPhases": {"interval_seconds": 30},
    "ACChargingEnergyIn": {"interval_seconds": 30, "minimum_delta": 0.1},     # session kWh added

    # --- SoC, target, ETA ---
    "Soc": {"interval_seconds": 60, "minimum_delta": 0.1},
    "ChargeLimitSoc": {"interval_seconds": 60},
    "TimeToFullCharge": {"interval_seconds": 60, "minimum_delta": 0.1},

    # --- cosmetic (dashboard label) ---
    "VehicleName": {"interval_seconds": 600},
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="public hostname the car connects to (fleet.hs.mfis.net)")
    ap.add_argument("--port", type=int, default=6443)
    ap.add_argument("--ca", required=True, help="PEM: CA chain that signed the server cert")
    ap.add_argument("--vin", default=None)
    ap.add_argument("--dry-run", action="store_true", help="print the config, don't send it")
    # fleet_telemetry_config must be SIGNED, so it has to be sent via Tesla's vehicle-command
    # HTTP proxy (tesla-http-proxy) rather than the Fleet API directly. Point --proxy-base at
    # the running proxy and --proxy-cert at its TLS cert so requests trusts the self-signed cert.
    ap.add_argument("--proxy-base", default=None,
                    help="route through the tesla-http-proxy, e.g. https://127.0.0.1:4443")
    ap.add_argument("--proxy-cert", default=None,
                    help="PEM: the proxy's own TLS cert (so we trust its self-signed endpoint)")
    args = ap.parse_args()

    from lib.config_retrieval import retrieve_setting
    vin = args.vin or retrieve_setting("TESLA_TELEMETRY_VIN") or retrieve_setting("TESLA_FLEET_VIN")
    if not vin:
        print("ERROR: no VIN (pass --vin or set TESLA_TELEMETRY_VIN).", file=sys.stderr)
        return 2
    with open(args.ca) as f:
        ca_pem = f.read()

    config = {
        "vins": [vin],
        "config": {
            "hostname": args.host,
            "port": args.port,
            "ca": ca_pem,
            "fields": FIELDS,
        },
    }

    if args.dry_run:
        print(json.dumps({**config, "config": {**config["config"], "ca": "<omitted>"}}, indent=2))
        return 0

    if args.proxy_cert:
        # Trust BOTH the public CA roots (needed for Tesla Fleet Auth token refresh) AND the
        # proxy's self-signed cert. Setting REQUESTS_CA_BUNDLE to only the proxy cert would
        # break verification of every real Tesla endpoint, so build a combined bundle.
        import certifi
        import tempfile
        bundle = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        with open(certifi.where()) as sys_ca:
            bundle.write(sys_ca.read())
        bundle.write("\n")
        with open(args.proxy_cert) as proxy_ca:
            bundle.write(proxy_ca.read())
        bundle.close()
        os.environ["REQUESTS_CA_BUNDLE"] = bundle.name

    from lib.tesla_api import TeslaApi
    api = TeslaApi()
    if args.proxy_base:
        api._base_url = args.proxy_base.rstrip("/")
        print(f"Routing fleet_telemetry_config through proxy: {api._base_url}")
    resp = api.set_fleet_telemetry_config(config)
    print(json.dumps(resp, indent=2) if resp is not None else "no response (budget/blocked/error)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
