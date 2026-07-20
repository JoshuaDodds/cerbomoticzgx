"""Process-lifetime ESS mode selected once during startup.

The optimizer implementation and its logical reserve must never observe different
modes. Config changes request a supervised restart; this module keeps the running
process on its startup mode until that restart occurs. Victron's hardware minimum
SoC is deliberately independent because that setting can trigger Recharge.
"""
from lib.config_retrieval import retrieve_setting


def _setting_enabled(value) -> bool:
    """Parse the boolean forms accepted by dotenv and the dashboard."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


WINTER_MODE = _setting_enabled(retrieve_setting("WINTER_MODE"))
OPTIMIZER_MODE = "winter" if WINTER_MODE else "summer"


__all__ = ["OPTIMIZER_MODE", "WINTER_MODE"]
