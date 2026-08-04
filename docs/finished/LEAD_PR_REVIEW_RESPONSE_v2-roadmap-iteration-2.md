# Lead PR Review Response — `v2-roadmap-iteration-2`

This file responds to `LEAD_PR_REVIEW_v2-roadmap-iteration-2.md` after the re-review cleanup pass.

## Required Items

1. **Optimizer concurrency guard — fixed.**
   - Added a module-level non-reentrant lock around the public `run_ai_optimizer()` entry point.
   - Overlapping scheduler/UI calls now skip with `False` and log:
     `AI_ESS: Optimization already running; skipping overlapping request.`
   - `/api/replan` now returns HTTP `409` with `{ok:false, skipped:true}` if the optimizer is already running, so it no longer reports a false success.
   - Regression coverage added in `tests/test_energy_broker.py` and `tests/test_91_frontend_server.py`.

2. **Retained restart flag — confirmed and documented.**
   - Startup already clears retained `Cerbomoticzgx/system/shutdown=True` by publishing retained `False` in `main.init()`.
   - Added a clarifying comment beside that boot-time clear and a regression check in `tests/test_startup_shutdown.py`.
   - No new restart mechanism was added; `/api/restart` still uses the existing MQTT-supervised restart path.

3. **Numeric config bounds — fixed.**
   - Added schema min/max bounds for every numeric `CONFIG_SCHEMA` setting.
   - `frontend.data.update_env_setting()` now rejects out-of-range numeric writes before touching `.env`.
   - Browser number inputs now receive the same `min`/`max` attributes.
   - Regression coverage added in `tests/test_frontend_data.py`.

4. **PV nowcast/weather coupling — fixed.**
   - `_apply_pv_nowcast()` now runs after the weather block regardless of Open-Meteo availability.
   - Open-Meteo remains optional/fail-open; measured live PV correction still applies when weather is disabled or unreachable.
   - Regression coverage added in `tests/test_energy_broker.py`.

5. **Endpoint auth / LAN exposure — deliberate out-of-scope decision.**
   - The user has determined that auth is out of scope for this branch.
   - Deployment model: internally deployed trusted-LAN operator app, no egress allowed, and unauthenticated LAN users are intended to have full access without auth.
   - `frontend/README.md` now documents that threat model and still warns to add reverse-proxy/auth before exposing beyond the LAN.

6. **Verification performed.**
   - Focused tests: `72 passed, 1 warning`.
   - Full suite: `179 passed, 3 warnings`.
   - Dry-run gate: `scripts/ai_ess_dryrun.py --json` exited `0` and generated a read-only plan. Note: the script still emits log/table text before the JSON payload, so the captured output is not pure parseable JSON.
   - Browser pass: current-code frontend launched on `127.0.0.1:8099`; Configuration tab opened; numeric editor verified visible with `min=0`, `max=50`, `step=0.01`.

## Low / Polish Items

- Removed the unused today+tomorrow `day_summary.total` block from `frontend/data.py`.
- Added debug logging to the silent broad catches called out in `frontend/advisor.py::_conf()` and `frontend/data.py::projected_today_net_eur()`.
- Fixed README default drift:
  - `BATTERY_CAPACITY_KWH` default is now documented as `42.0`.
  - `OPTIMIZER_SOC_STEP_PCT` default is now documented as `1.0`.
- Added a one-line comment explaining the brief clear-then-program Victron slot window and why its failure mode is safe/self-healing.
