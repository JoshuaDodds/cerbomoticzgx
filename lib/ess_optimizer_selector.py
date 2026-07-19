"""Restart-isolated selector for the active ESS optimizer implementation.

``WINTER_MODE`` is deliberately frozen by :mod:`lib.ess_mode` during startup.
The selected optimizer is then the only implementation imported into the
service process. Changing the setting therefore requires the existing
supervised restart rather than swapping control policy in a running
critical-power loop.
"""
from importlib import import_module

from lib.ess_mode import OPTIMIZER_MODE, WINTER_MODE
OPTIMIZER_MODULE_NAME = (
    "lib.ai_powered_ess_winter" if WINTER_MODE else "lib.ai_powered_ess"
)

_optimizer = import_module(OPTIMIZER_MODULE_NAME)

_REQUIRED_EXPORTS = (
    "OptimizationEngine",
    "format_plan_summary",
    "optimize_schedule",
    "_coerce_datetime",
)
_missing_exports = [name for name in _REQUIRED_EXPORTS if not hasattr(_optimizer, name)]
if _missing_exports:
    raise ImportError(
        f"{OPTIMIZER_MODULE_NAME} is missing required optimizer API: "
        + ", ".join(_missing_exports)
    )

# Keep the broker-facing API intentionally narrow and identical in both modes.
OptimizationEngine = _optimizer.OptimizationEngine
format_plan_summary = _optimizer.format_plan_summary
optimize_schedule = _optimizer.optimize_schedule
_coerce_datetime = _optimizer._coerce_datetime


__all__ = [
    "OPTIMIZER_MODE",
    "OPTIMIZER_MODULE_NAME",
    "OptimizationEngine",
    "WINTER_MODE",
    "_coerce_datetime",
    "format_plan_summary",
    "optimize_schedule",
]
