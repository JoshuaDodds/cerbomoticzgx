#!/usr/bin/env python3
"""Run the read-only Daikin ONECTA Phase 0 proof of concept.

Authorization is deliberately split into two commands because the registered
callback is the public ESS hostname while this script normally runs on a
development host:

    python scripts/onecta_poc.py authorize
    # Open the printed URL and approve access. Copy the complete callback URL.
    python scripts/onecta_poc.py exchange
    python scripts/onecta_poc.py discover

The exchange command prompts without terminal echo so the one-time callback
code does not enter shell history. Discovery performs no HVAC control writes.
"""

import argparse
import getpass
import os
import sys

sys.path.append(os.getcwd())

from lib.onecta_api import (  # noqa: E402
    DEFAULT_REPORT_PATH,
    OnectaError,
    build_capability_report,
    create_authorization_url,
    discover_gateway_devices,
    exchange_callback,
    write_capability_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "authorize",
        help="create a short-lived OAuth state and print the Daikin consent URL",
    )
    subparsers.add_parser(
        "exchange",
        help="validate a returned callback URL and persist its rotating tokens",
    )
    discover = subparsers.add_parser(
        "discover",
        help="make one read-only gateway-device request and save a redacted report",
    )
    discover.add_argument(
        "--output",
        default=str(DEFAULT_REPORT_PATH),
        help=f"redacted report path (default: {DEFAULT_REPORT_PATH})",
    )
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "authorize":
            url = create_authorization_url()
            print("Open this URL in your browser and approve the private integration:")
            print(url)
            print(
                "\nAfter Daikin redirects to the ESS callback, copy the complete "
                "address-bar URL and run the exchange step."
            )
            return 0

        if args.command == "exchange":
            callback_url = getpass.getpass(
                "Paste the complete callback URL (input hidden): "
            )
            exchange_callback(callback_url)
            print(
                "ONECTA authorization succeeded. Access and rotating refresh "
                "tokens were stored atomically in .secrets."
            )
            return 0

        devices, rate_limits = discover_gateway_devices()
        report = build_capability_report(devices, rate_limits)
        write_capability_report(report, args.output)
        print(
            f"Read-only discovery succeeded: {report['device_count']} gateway "
            f"device(s)."
        )
        if rate_limits:
            remaining = rate_limits.get("remaining_day", "unknown")
            limit = rate_limits.get("limit_day", "unknown")
            print(f"Daikin daily API allowance remaining: {remaining}/{limit}.")
        print(f"Redacted capability report written to {args.output}.")
        return 0
    except OnectaError as error:
        print(f"ONECTA POC failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
