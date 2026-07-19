# Lead PR Review — `v2-roadmap-iteration-2`

**Reviewer:** Lead (original system designer)
**Base:** `origin/main` @ `3f2143d8`
**Date:** 2026-06-30
**Scope reviewed:** optimizer/control core, new modules (weather, config_paths), Flask server + guarded controls, advisor subprocess, frontend data/JS, tests, all updated docs.
**Constraint:** `git`/`pytest` were not runnable in this review environment — this is a static read of the working tree. **The test suite has not been executed here; running it is the merge gate (see Required #6).**

---

## Summary

This is a large, genuinely well-engineered branch that turns the AI ESS work into a coherent operator system. The control-critical code (the DP optimizer, economic grid charging, cost-basis sell floor, daily-settlement policy, and the reporting-vs-control separation) is careful, well-commented, and — importantly — backed by strong, targeted regression tests. The new surfaces (weather shadow mode, the AI advisor, the dashboard controls, the `.env` writer) are correctly isolated, fail-open, and read-only where they claim to be. Documentation is unusually thorough and accurate.

**I'm on board with the direction.** The architecture is sound and consistent with the system's design principles. I am **not** signing off on an unsupervised "in the wild" beta as-is: there are five targeted hardening items below — none requiring rework — that I want closed first, because this controls a 16 kW 3-phase system where a concurrency or bounds slip is high-consequence.

**Verdict: Request Changes (minor) → conditional approve for beta** once Required items 1–6 are addressed.

---

## Critical / Must-fix before a live beta

| # | File · line | Issue | Why it matters |
|---|---|---|---|
| 1 | `lib/energy_broker.py:1922` `run_ai_optimizer`; `frontend/server.py:117` `/api/replan` | **No mutex around the optimizer.** In the default `FRONTEND_ENABLED=True` (in-process) deployment, the 15-min scheduler thread and a dashboard **Replan** click can enter `run_ai_optimizer()` concurrently — both call `clear_victron_schedules()` then reprogram 5 charge slots and both write `ac_power_setpoint`. | Interleaved clear/reprogram + double setpoint writes on a live 16 kW controller. Narrow window, high consequence — exactly the concurrency risk `AGENTS.md` calls out. |
| 2 | `frontend/server.py:204` `/api/restart` → `_request_service_restart` (`:137`) | **Restart publishes a *retained* `Cerbomoticzgx/system/shutdown=True`.** Confirm the startup path clears/overwrites that retained flag on boot. | If it isn't cleared, a reconnecting subscriber re-reads `True` and shuts down again → **boot loop**. Described as the "existing" path, so likely handled, but the retained flag must be verified, not assumed. |

## High / Should-fix

| # | File · line | Issue | Recommendation |
|---|---|---|---|
| 3 | `lib/energy_broker.py:1995-2003` | **PV nowcast is nested inside `if weather_context.get('available')`.** The PR doc states the measured-production nowcast is deliberately *not* a weather-model gate, but in code it stops whenever Open-Meteo is unreachable (stale cache + failing fetch) or `WEATHER_ENABLED=False`, silently reverting to raw VRM (which collapses late-day — the very bug this fixes). | Hoist `_apply_pv_nowcast(...)` out of the `if available` block. It already degrades gracefully (`weather_context or {}`, GTI defaults to 0 → near-term ratio 1.0). One-line move; meaningful robustness. |
| 4 | `frontend/data.py:653` `_coerce_value`, `:677` `update_env_setting`; `frontend/config_schema.py` | **No min/max bounds on numeric `.env` writes.** Type is validated, range is not. A client can persist `ESS_MAX_DISCHARGE_KW=99999`, `MIN_SOC_RESERVE_SUMMER=-50`, `ESS_MAX_GRID_CHARGE_SOC=10000`, etc., straight into the optimizer's feasibility checks. | Add `min`/`max` to the `CONFIG_SCHEMA` numeric entries and clamp/reject in `_coerce_value`. This is the same bounding the Phase-2 advisor plan already calls for — apply it to the manual editor too. |

## Medium / Security posture (conscious decision needed)

| # | File · line | Issue | Recommendation |
|---|---|---|---|
| 5 | `frontend/server.py` (all POST routes); `:347` binds `0.0.0.0` | **Every state-changing endpoint is unauthenticated** — restart, replan, clear-schedule, ai-override, grid-assist, config write, advisor. `/api/restart`, `/api/replan`, `/api/victron/clear-schedule` read no body, so they're **CSRF-triggerable** by any cross-origin form POST from a page the operator visits; anything on the LAN can idle/restart the controller or drain the advisor's subscription quota. | For a trusted home LAN this may be accepted risk, but it should be a *conscious* call. Minimum: bind `127.0.0.1` + reverse-proxy auth, or a shared-secret header / `Origin`-`Referer` same-origin check on the POST routes. Document the threat model in `frontend/README.md`. |

## Low / Polish

- `frontend/data.py:519` `day_summary` still computes a today+tomorrow `total` block (not rendered). Vestigial vs the "never tally today+tomorrow" rule — remove to prevent future misuse.
- A few broad `except Exception` without logging (`frontend/advisor.py:232-237` `_conf`; `frontend/data.py:126` `projected_today_net_eur`). `AGENTS.md` prefers narrow/logged. Minor.
- Doc default nits: `README.md:75` says `OPTIMIZER_SOC_STEP_PCT` default 5.0 (env ships 1.0); `:71` `BATTERY_CAPACITY_KWH` default 45.0 (env ships 42.0). Harmless prose drift.
- `run_ai_optimizer` clears Victron slots then reprograms — brief empty window; self-heals next cycle and failing-to-charge is the safe direction, so acceptable. Worth a one-line comment.

---

## What looks good (worth calling out)

- **Reporting vs. control separation is exemplary.** `_post_process` (`ai_powered_ess.py:1044`) derives the Victron charge windows from the *original* DP trajectory **before** `_frontload_charging` re-times for display, and the BUY setpoint stays 0. `ESS_MODEL_CHARGE_RATE` genuinely cannot affect control. Tested (`test_frontload_*`).
- **Safety floors are correct and tested.** Cost-basis sell floor converts DC→AC correctly (`set_cost_basis_floor:547`), blocks only *active* discharge below cost (`optimize:705`), PV surplus still exports; SoC cap blocks only grid-sourced charge; terminal value is suppressed on same-day horizons to avoid evening over-retain. All covered in `test_ai_powered_ess.py`.
- **Economic grid charging** (removal of the hard price cap) is guarded from both sides: `test_profitable_grid_charge_is_inferred_from_path_economics` *and* `test_flat_prices_do_not_trigger_pointless_grid_charging`. The removed knobs leave no dangling references (only a regression test asserting they stay out of the schema).
- **Daily-settlement policy** is conservative, learns its thresholds from history (percentile-based, clamped, safe fallback), is transparent (`planning_policy` in the plan JSON), and is tested in both directions.
- **Advisor is safe**: subprocess via list-argv (no `shell=True`), prompt on **stdin** (user question can't inject), `_tunables()` filters to `CONFIG_SCHEMA` so **secrets never reach the model**, lock + timeouts, strictly read-only, retrieval bounded to validated existing date files.
- **Weather is fail-open**: hardcoded Open-Meteo host (no SSRF), atomic cache write, sensible degree-day/GTI shadow math, never blocks the optimizer.
- **Test quality is high** and targets exactly the risky new behavior. Server control routes are covered with proper mocking (no real MQTT/hardware touched).
- **Docs** (`README`, `frontend/README`, `.env.example`, `PR-Branch-current-iteration.md`, `config_schema.py`) are thorough and match the code.

---

## Beta-readiness — Required before "in the wild"

1. Add a non-reentrant lock around `run_ai_optimizer` (skip-if-running), and guard/serialize `/api/replan` (in-process only, single writer). *(Critical #1)*
2. Confirm the retained `system/shutdown` flag is cleared on boot so `/api/restart` can't loop. *(Critical #2)*
3. Add min/max bounds to numeric config writes. *(High #4)*
4. Decouple the PV nowcast from weather availability. *(High #3)*
5. Make a deliberate decision on endpoint auth / LAN exposure and document it. *(Medium #5)*
6. **Run the gate:** `export DEV=1 && python -m pytest -s -q` (could not run here), plus `python scripts/ai_ess_dryrun.py --json` and a browser pass per the PR doc's validation checklist.

Items 1–4 are small, localized changes. None of this is a rearchitecture — the foundation is solid and I'm happy to approve once they're in.
