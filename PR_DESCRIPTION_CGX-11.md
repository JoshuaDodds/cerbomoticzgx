# CGX-11: Fix AI ESS optimizer bugs, rewrite DP engine, add 13:05 next-day planning & negative-price feed-in limit

## What / Why
The `CGX-11-ai-ess-optimizer` branch added an AI-powered ESS optimizer, but the
engine never functioned correctly in production and left several correctness and
safety gaps. This PR fixes those bugs, rewrites the optimization core, wires in
real forecasts, schedules a daily next-day re-plan at 13:05, and adds Victron
grid feed-in protection during negative prices — all aimed at maximising revenue
over the monthly Tibber settlement period without compromising system stability.

## Bugs fixed
1. **Production crash on every run** — `get_all_price_points()` returns Tibber
   ISO-8601 *strings* for `start`, but the optimizer called `.tzinfo` on them
   (`datetime.now(price_data[0]['start'].tzinfo)`), throwing on every 15-minute
   run and silently falling back to legacy logic. Added `_coerce_datetime()` so
   both strings and `datetime` objects are supported.
2. **Hourly-vs-15-minute slot mismatch** — Tibber prices are hourly, but the
   engine hardcoded `slot_duration_h = 0.25` and `duration = 900s`. Energy math
   and Victron charge windows were 4× too short. Slot duration is now detected
   from the data (defaults to 1h; still supports 15-min data).
3. **End-of-horizon battery dump** — the DP minimised cost with no terminal
   value on stored energy, so it would sell the battery down to the reserve at
   the end of the window regardless of the next day. Added a terminal valuation
   (`ESS_TERMINAL_VALUE_FACTOR` × horizon mean buy price).
4. **`IndexError` on empty schedule** — `_post_process` indexed `schedule[0]`
   unguarded. Now returns `None` cleanly when no transitions are found.
5. **Import-time crash on missing settings** — `float(retrieve_setting(...))`
   at module load in `energy_broker.py` raised `TypeError` if a setting was
   absent. Now uses the safe `_get_float_setting` helper.
6. **No buy/sell spread** — imports and exports were valued at the same price.
   Added a configurable export price model (`ESS_EXPORT_PRICE_FACTOR`,
   `ESS_EXPORT_FEE`).
7. **Inconsistent AI flag check** — `tibber_api` gated the AI price feed on
   `== 'True'` while the broker used a tolerant truthiness check, so `=1` enabled
   the optimizer but starved it of price data. Both now accept `1/true/yes/on`.

## How (high-level)
- **`lib/ai_powered_ess.py`** — full rewrite of `OptimizationEngine` to a
  SoC-to-SoC dynamic program: enumerates SoC transitions per slot, derives grid
  energy from charge/discharge efficiency + PV/load forecasts, enforces grid and
  battery power limits and the seasonal SoC reserve, prices imports/exports with
  the buy/sell spread, and selects the terminal state by net objective
  (cost − terminal value of stored energy). Forecasts may be passed as lists or
  as dicts keyed by slot start time. Victron charge windows now carry a computed
  target SoC instead of a hardcoded 100%.
- **`lib/energy_broker.py`** — `run_ai_optimizer` now builds a PV forecast from
  `pv_projected_remaining`, applies negative-price feed-in protection, sets the
  AC setpoint, and programs charge slots with per-slot target SoC. Added
  `run_daily_price_update_and_optimize` scheduled at **13:05** to refresh
  next-day prices and re-plan over the full 48h horizon. Module constants parsed
  safely.
- **`lib/victron_integration.py`** — new `limit_grid_feed_in(enabled, watts)`
  controlling `MaxFeedInPower` (0W when negative-price limit is on, `-1` to
  restore unlimited). Idempotent via global state.
- **`lib/constants.py`** — added the `max_feed_in_power` writable topic.
- **`lib/tibber_api.py`** — tolerant AI-flag truthiness check.
- **Docs/tests** — `.env.example` and `README.md` document all new settings;
  `tests/test_ai_powered_ess.py` adds regressions for string timestamps, hourly
  slot duration, terminal value, and the negative-price feed-in flag; backtest
  script updated for the new schedule shape.

## Risk / Rollback
- **Risk:** Controls a live 16kW 3-phase ESS. The optimizer rewrite changes
  charge/discharge scheduling and adds a new Victron write (`MaxFeedInPower`).
  Legacy logic remains as a health-checked fallback when the AI flag is off or
  the optimizer goes stale.
- **Verify before relying on it:** confirm the `MaxFeedInPower` dbus path
  (`Settings/CGwacs/MaxFeedInPower`) matches your Venus OS version, and that the
  feed-in toggle behaves as expected on a non-critical window.
- **Rollback:** the entire feature is gated behind `AI_POWERED_ESS_ALGORITHM`;
  set it to `False` to revert to legacy scheduling. Negative-price limiting is
  separately gated by `NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED`.

## Human Testing Criteria
1. `export DEV=1 && pytest -q` — full suite passes (I was unable to run it in
   this environment; please run before merge).
2. With `AI_POWERED_ESS_ALGORITHM=True`, confirm `run_ai_optimizer` logs an
   optimization (no `start.tzinfo`/normalisation errors) and programs charge
   slots with whole-hour durations.
3. Inject a negative current price and confirm `MaxFeedInPower` is set to 0 and
   reverts to `-1` once the price is non-negative.
4. Confirm the 13:05 job fires after next-day Tibber prices publish and plans
   across the day boundary.

## Follow-up changes (round 2)
Added after live dry-run review:
- **Configurable planning resolution** (`OPTIMIZER_SLOT_MINUTES`, default 15) —
  sub-divides hourly Tibber prices into finer planning slots and auto-uses native
  resolution once Tibber publishes quarter-hourly prices. (Note: with hourly price
  data, prices are flat within the hour, so decisions remain effectively hourly
  until 15-min prices are available.)
- **Four-action model** — slots are classified as `charge`, `discharge` (sell),
  `grid_assist` (retain: grid covers load, battery held), or `idle`. The
  PV-surplus-while-full case is now correctly `idle`, not `discharge`. The
  current-slot `grid_assist` action is applied by reusing the existing
  `grid_charging_enabled` + `ess_net_metering_overridden` control loop.
- **Peak intelligence** — `ESS_MIN_SELL_PRICE` (never actively sell battery energy
  below this floor) and `ESS_EXPECTED_PEAK_PRICE` (value end-of-horizon charge at
  the expected peak) bias the plan toward holding charge for the typical
  morning/evening peaks instead of selling into a low intra-day high.
- **Plan logging** — a shared `format_plan_summary()` renders the plan; the live
  service now logs the full plan on every optimization run (same view as
  `scripts/ai_ess_dryrun.py`) so log history shows how plans change over time.

## Follow-up changes (round 3)
- **Self-consumption forecast** — house load is now forecast per slot from the
  VRM consumption forecast (`consumption_total_projected`, falling back to
  measured-so-far extrapolation, then `DAILY_HOME_ENERGY_CONSUMPTION`) shaped by
  a diurnal `LOAD_PROFILE_HOURLY` profile, and wired into `run_ai_optimizer`.
  Previously the optimizer used only a flat daily default, so it over-estimated
  sellable capacity (e.g. predicting a full battery into the evening peak when it
  would realistically be drawn down by self-usage).
- **Tibber 15-minute prices** — `get_all_price_points()` now queries the Tibber
  GraphQL API directly with `priceInfo(resolution: QUARTER_HOURLY)` (the tibber
  python library does not expose this argument, which is why only hourly prices
  were returned). Configurable via `TIBBER_PRICE_RESOLUTION`; falls back to hourly
  GraphQL then to the library if quarter-hourly is unavailable. Combined with
  `OPTIMIZER_SLOT_MINUTES=15`, the optimizer now plans on real 15-minute prices.

## Follow-up changes (round 4)
- **Fixed: AI discharge/setpoint being clobbered by legacy control.** Two legacy
  paths overrode the optimizer's AC setpoint: (1) the AI grid-assist toggle
  published `grid_charging_enabled`, whose event handler asynchronously zeroes the
  setpoint; (2) `manage_grid_usage_based_on_current_price()` runs on every
  price/load event and was not gated by the AI. Now: AI grid-assist is tracked in
  state only (`ai_grid_assist`, no topic publish, no Tesla-flag coupling), and
  `manage_grid_usage_based_on_current_price()` stands down when the AI optimizer
  is healthy — except to maintain retain mode (matching the setpoint to live load)
  while `ai_grid_assist` is on. The optimizer's export/charge setpoint is now
  authoritative.
- **Log noise / size:** AI-health status logs only on transition; the service plan
  log shows only the next 12h of slots (cost summary still spans the full horizon).
- **Exposed tunables in config:** `MIN_SOC_RESERVE_WINTER`, `MIN_SOC_RESERVE_SUMMER`,
  and `OPTIMIZER_SOC_STEP_PCT` moved from hard-coded constants to `.env`
  (documented in `.env`, `.env.example`, README).

## Follow-up changes (round 5)
- **PV-aware grid-assist (retain) setpoint.** Retain mode now imports only the
  PV deficit (`max(0, ac_out_power - pv_power)`) instead of the full house load,
  and holds the grid setpoint at 0 when PV covers the load — so surplus PV charges
  the battery and exports when full rather than the system needlessly importing.
  Redundant setpoint writes are suppressed with a deadband.
- **Per-slot discharge setpoint** matches the plan's planned grid power (not a
  blanket max export), so real SoC tracks the forecast.
- **Day cost summary** combines today's actuals (from MQTT) with the remaining
  forecast and is formatted with the € sign; plan output reordered (breakdown
  first, headline summary last). A plain-English `Reason:` explains the current action.

## Follow-up changes (round 6)
- **Clearer mode model.** The single overloaded action label is replaced by four
  user-facing modes — **BUY / SELL / HOLD / SELF-SUPPLY** — plus a separate live
  "Grid now" flow line (import/export/idle) and the actually-applied setpoint, so
  e.g. HOLD no longer looks contradictory when PV covers the load and the grid is
  idle. SELL now correctly covers PV-surplus-while-full (not mislabelled). Each
  decision carries a plain-English `reason` and a machine-readable `reason_code`,
  published to state as `ai_mode` / `ai_reason` / `ai_reason_code`.

## Follow-up changes (round 7) — frontend scaffold
- New self-contained, **read-only** dashboard in `frontend/` (Flask + vanilla JS,
  no CDN). Views: current decision, expandable hour→15-min→reasoning schedule tree
  (color-coded by mode), day cost summary (actuals + forecast), and configuration.
- Decoupled by design: the main service publishes its plan as JSON atomically to
  `AI_PLAN_EXPORT_PATH` (`_publish_plan_json` in `run_ai_optimizer`); the dashboard
  only reads that file + `.env`, never the control path. Per-slot `reason`/`reason_code`
  are now attached to every schedule step to power the drill-down.
- Runs standalone (`python -m frontend`, sidecar-friendly) or via
  `frontend.server.run_in_thread()`. New settings: `AI_PLAN_EXPORT_PATH`,
  `FRONTEND_HOST`, `FRONTEND_PORT`. Adds `flask` to requirements.

## Follow-up changes (round 8) — frontend overview, jump-to-now, editable knobs
- **Overview** band (always visible): metric cards + current decision/reason.
- Schedule **highlights the current hour/slot** (`NOW`) and **auto-scrolls to now**
  on open; current marking computed in `data.py`.
- **Editable config knobs**: click-to-edit with a confirm step; `POST /api/config`
  writes one allow-listed, type-validated setting to `.env` atomically. Propagation
  reuses the existing logic — `retrieve_setting()` re-reads `.env` each call (applies
  next cycle + republishes the `Cerbomoticzgx/config/<KEY>` bus mirror) and
  `ConfigWatcher` reacts to the file change. (`STATE`-on-`0` is treated as unset, so
  `.env` is the correct source of truth for these; control toggles that live in
  `GlobalState`/bus remain a separate future class written via `STATE.set`.)
- Auto-refresh updates the plan only (not the config panel) so it can't interrupt an
  in-progress edit.

## Follow-up changes (round 9) — tomorrow's solar + battery cycle cost
- **Tomorrow's solar now forecast.** `solar_forecasting` requests a 2-day VRM
  window and stores `pv_projected_tomorrow`; `_build_pv_forecast_by_slot`
  distributes today's remaining PV and tomorrow's forecast across each day's
  daylight slots. Previously day-2 PV was assumed 0, so the plan over-bought from
  the grid to fill the battery (and showed a false net cost for tomorrow).
- **Battery cycle cost** (`ESS_BATTERY_CYCLE_COST`, €/kWh discharged, default 0):
  penalises battery throughput in the DP so it won't cycle for arbitrage thinner
  than round-trip losses + wear, eliminating value-neutral churn.

## Follow-up changes (round 10) — dashboard clarity
- Split the overview into **Today net** vs **Horizon net (today+tomorrow)** (the old
  single "Day net" was actually the horizon total — confusing). Topbar relabeled.
- Slot rows now show real **Import/Export kWh and a € net**, consistent with hour
  rows (fixed the "—" placeholders and the units mismatch in the Net column).
- Added a **Solar forecast** card (today remaining + tomorrow); `pv_tomorrow_wh`
  is now included in the published plan JSON.

## Follow-up changes (round 11) — real-time dashboard + UX polish
- **Live MQTT feed** (`frontend/live.py`, `/api/live`): read-only subscription to the
  broker caches SoC, price, grid/PV/battery/load power, setpoint and AI mode/reason;
  UI polls every ~5s and overlays live values on the plan (plan still refreshes 30s).
  A connected/offline dot indicates feed health; falls back to plan values when offline.
- **Now card** now shows the real grid flow (e.g. "importing 14 kW") and a live
  power-flow strip (grid / solar / battery / house), and shows "Charge schedule
  active" instead of "0 W" when BUY is driven by the Victron charge schedule.
- **Overview**: Now card moved beside the Solar card; Solar labels spelled out; added
  live "producing X kW now".
- Day summary actual/forecast split is now two readable lines (was a tiny one-liner).
- "Plan generated" shows 24h local time; live-feed status appended.
- Config edit: Save/Cancel now sit above other content (z-index + description hidden
  while editing) so they're reliably clickable.
- Tabs restyled to read clearly as tabs with an obvious active state.

## Follow-up changes (round 12) — min-SoC single source of truth + history logging
- **Battery min-SoC reserve has one source of truth.** New
  `helpers.current_min_soc_reserve()` resolves the seasonal reserve from `.env`
  (`MIN_SOC_RESERVE_WINTER/SUMMER`) using the single season rule. The optimizer's
  planning floor and the Victron hardware `MinimumSocLimit` both derive from it, so
  they can't diverge. Removed the hardcoded `set_minimum_ess_soc(20)` (now uses the
  reserve), made the write idempotent, and the optimizer re-asserts the floor each
  run. (BMS remains the ultimate low-SoC cutoff, per design — no software safety floor.)
- **Historical data logging (now).** Each optimizer cycle appends an analytics-ready
  NDJSON record (state + decision + live power + running daily actuals) to a per-day
  file under `HISTORY_DIR` (default `data/history/`, gitignored). Best-effort, never
  affects control. Intended to feed a future Claude SDK performance-analysis pass.

## Follow-up changes (round 13) — 0% SoC fix, labels, alignment, analytics
- **Fixed: 0% SoC treated as "unavailable"** — the optimizer skipped at a real 0%
  (`STATE.get` returns 0 for both missing and 0%); now uses battery voltage as the
  presence signal so it keeps running at 0%.
- **Canonical 4-state control action (IDLE / RETAIN / BUY / SELL)** — one label,
  derived from the *commanded setpoint* (not predicted energy flow), used
  identically by the console, web UI, plan JSON and history so they can't disagree.
  `control_action_for()` maps the DP's internal action: BUY = forced grid charge;
  SELL = forced discharge to grid (SoC falls); RETAIN = grid-assist hold that
  imports to cover load; **IDLE** = neutral setpoint (PV surplus, self-supply, or a
  hold where PV covers the load) where Victron decides and the real flow is only
  known retroactively. IDLE flow is shown as **projected** and kept out of the
  committed net (Option A); forced export setpoints are used only for real SELL,
  preserving discharge-spreading.
- **Per-slot settlement** — at each quarter-hour boundary a `kind: "settlement"`
  record is written to the same daily NDJSON pairing the prediction for the slot
  that just closed with the actuals (counter diffs: import/export/€, SoC delta,
  PV), handling the midnight reset and service gaps. `history_report.py` reports
  predicted-vs-actual net-€ MAE. Backbone for forecast-accuracy learning + the
  roadmap timeline view.
- **Optimizer runs clock-aligned** to :00/:15/:30/:45 so each 15-min slot's action is
  applied at its boundary (fixes the 2–3 min apply lag).
- **Export limits raised** to 16 kW (`ESS_MAX_GRID_EXPORT_KW`/`ESS_MAX_DISCHARGE_KW`/
  `ESS_EXPORT_AC_SETPOINT`); hardware/BMS/grid-code remain the real cap.
- **History sampling** now snapshots realised power at cycle start (prior-decision
  outcome) for clean plan-vs-actual.
- **New `scripts/history_report.py`** — rolls the NDJSON logs into realised daily €,
  a house-load fingerprint, and the realised PV shape by hour (stdlib only; `--json`
  for machine consumption). Seeds the future learning of this installation's unique
  PV curve.

## Links
- Jira: CGX-11
- Branch: `CGX-11-ai-ess-optimizer-8967993876044487087`

---

### Suggested smart-commit message
```
CGX-11 Rewrite AI ESS optimizer and add feed-in/next-day logic #comment Fix start-tz crash, hourly slots, terminal value, buy/sell spread, import-time crash, AI-flag truthiness; rewrite DP engine; add 13:05 48h re-plan, PV forecast wiring, and negative-price grid feed-in limit (0W, auto-revert). Docs + tests updated. #time <ACTUAL_MINUTES> #transition In Review
```
Replace `<ACTUAL_MINUTES>` with your tracked time. Run `export DEV=1 && pytest -q`
before committing, per AGENTS.md.
