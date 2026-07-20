"""Restart-frozen configuration for season-independent appliance scheduling.

This deliberately does not import :mod:`lib.ess_mode`. Appliance price
optimization and the selected ESS policy are independent operator decisions;
the existing supervised restart applies a changed setting coherently across the
Home Connect coordinator and EnergyBroker.
"""

from lib.config_retrieval import retrieve_setting


def _setting_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


APPLIANCE_OPTIMIZATION_ENABLED = _setting_enabled(
    retrieve_setting("APPLIANCE_OPTIMIZATION_ENABLED")
)


__all__ = ["APPLIANCE_OPTIMIZATION_ENABLED"]
