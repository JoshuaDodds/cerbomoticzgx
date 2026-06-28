"""Self-contained read-only dashboard for the cerbomoticzGx ESS service.

The frontend consumes the plan JSON published by the main service (see
energy_broker._publish_plan_json) and reads configuration from .env. It performs
no control actions — it is purely for visibility in v1. Control "knobs" are a
planned future addition.

Run standalone:   python -m frontend
Or in-process:    from frontend.server import run_in_thread; run_in_thread()
"""

__all__ = ["server", "data", "config_schema"]
