# Branch changes vs upstream main

Comparison target: `origin/main` at `3f2143d812d3973237c9bd3e251082ccc4ae03db`

Branch described: `v2-roadmap-iteration-2`

Date: 2026-06-30

Scope: current branch working tree, including the untracked files that are intended
to be part of this branch (`lib/config_paths.py`, `lib/weather.py`,
`tests/test_config_paths.py`, and `tests/test_weather.py`).

## Executive summary

This branch turns the AI ESS work into a fuller operator system:

- The optimizer is now **best-daily-settlement first**. It tries to maximize today's
  profit, or reduce today's cost as close to zero as possible, and only accepts a
  worse today for tomorrow when the future win is clearly exceptional relative to
  learned history and forecast risk.
- Forced grid charging is now **path-economic**. The old hard grid-charge price cap
  tunables were removed; the optimizer decides from buy/sell prices, PV/load
  forecasts, battery wear, stored-energy cost basis, and available future spreads.
- The dashboard is now an **operator dashboard**, not just a visibility page:
  Overview entry point, ESS tabs, guarded `.env` editing, replan requests, Import
  Schedule clearing, Trends, Weather, persistent Advisor chat, desktop/mobile
  navigation, and external Battery/Venus views.
- Weather forecasting has a first implementation in **shadow mode** using keyless
  Open-Meteo. It logs/visualizes weather-derived HVAC and PV adjustments without
  changing control unless apply gates are deliberately enabled after validation.
- Same-day PV forecasting now has a **measured nowcast correction**. VRM remains the
  raw source forecast, but the optimizer raises today's near-term PV slots from live
  PV power and the latest settled PV slot when measured production proves VRM's
  remaining value is too low.
- Forecast and history tooling now supports actual-vs-forecast analysis for PV/load,
  projected-today profit, Advisor retrieval, richer charts, runtime control toggles,
  and regression tests.

## Optimizer and control behavior

### Daily settlement policy

`lib/ai_powered_ess.py` now compares:

- a full-horizon plan across all available Tibber prices, and
- a today-first plan that protects the current 24-hour settlement result.

The selected policy is exported in `planning_policy` inside the plan JSON. This
prevents small forecasted gains tomorrow from causing a large known loss today,
while still allowing a future carryover when the price spike or expected upside is
large enough to be worth the forecast risk.

### Economic grid charging

The branch removes obsolete user-facing grid-charge price caps:

- `ESS_MAX_GRID_CHARGE_PRICE`
- `ESS_GRID_CHARGE_CHEAP_PCT`

Grid charging is now selected by modeled path economics. The remaining
`ESS_MAX_GRID_CHARGE_SOC` setting means only this: forced grid charging will not
target above that SoC, but PV surplus is still allowed to charge the battery above
that cap naturally.

### Cost basis, wear, and anti-churn guards

The optimizer now considers:

- battery cycle cost (`ESS_BATTERY_CYCLE_COST`),
- arbitrage margin (`ESS_ARBITRAGE_MARGIN`),
- persisted ESS cost basis (`ESS_COST_BASIS_PATH`),
- sell floor (`ESS_MIN_SELL_PRICE`),
- SELL hysteresis (`ESS_SELL_MIN_DWELL_MIN`, `ESS_SELL_HYSTERESIS_EUR`), and
- seasonal SoC reserve.

This reduces fragile thin-spread cycling and prevents active export below the
effective cost of stored grid energy.

### Grid-charge reporting and Victron slots

`ESS_MODEL_CHARGE_RATE` keeps settlement/reporting aligned with how Victron scheduled
charging behaves: full-power-to-target, then hold. Victron charge slots are still
derived from the optimizer trajectory; this is a reporting/economics correction, not
an unsafe direct control expansion.

## Forecasting and analytics

### Weather shadow mode

New file: `lib/weather.py`.

Weather integration:

- fetches Open-Meteo without an API key,
- uses `HOME_ADDRESS_LAT` / `HOME_ADDRESS_LONG` from `.secrets`,
- caches latest forecast data under `data/weather/latest.json`,
- appends compact summaries to `data/weather/weather.ndjson`,
- computes symmetric heating/cooling degree-day load adjustments, and
- computes GTI-shaped PV shadow forecasts.

Default apply gates remain off:

- `HVAC_LOAD_APPLY=False`
- `PV_WEATHER_APPLY=False`

The intent is to compare weather-adjusted forecasts against actual PV/load for 1-2
weeks before allowing them to affect a 16 kW controller.

### PV nowcast and VRM forecast reporting

Files changed:

- `lib/energy_broker.py`
- `frontend/data.py`
- `frontend/static/js/app.js`
- `tests/test_energy_broker.py`
- `tests/test_90_mobile_ux_static.py`

Why this changed:

The raw VRM solar forecast can be correct as a full-day total but wrong as a
remaining-day control input late in the day. On 2026-06-29 the system was still
producing meaningful PV while VRM's `pv_projected_remaining` had collapsed near zero.
That made the optimizer schedule too little PV in the remaining slots, and the
Overview Solar card contradicted the schedule once the optimizer was corrected.

Implementation notes:

- `_latest_settled_pv_slot_kwh(slot_duration_h, now)` reads today's newest complete
  settlement row, rejects stale/incomplete rows, checks slot duration, and normalizes
  actual PV to the optimizer slot size.
- `_pv_nowcast_anchor_kwh(slot_duration_h, now)` blends the latest settled slot with
  live `STATE.get("pv_power")`. If live PV has dropped materially below the recent
  settled slot, it treats that as sunset/tree-line drop-off and lowers confidence
  instead of blindly extrapolating the prior high output.
- `_apply_pv_nowcast(pv_forecast, forecast_slots, weather_context, slot_duration_h,
  now)` raises only the current day's near-term PV forecast, fades the correction over
  the next few hours, weights the fade by Open-Meteo GTI ratios when available, and
  leaves tomorrow unchanged. It annotates `weather_context["summary"]` with
  `pv_nowcast_*` fields for inspection.
- `_publish_plan_json()` now exports both the raw and adjusted values:
  `pv_remaining_raw_wh`, `pv_remaining_raw_source` (`"VRM forecast"`),
  `pv_adjusted_remaining_wh`, `pv_adjusted_remaining_source`, and
  `pv_adjustment_kwh`. The original `pv_remaining_wh` is retained for compatibility.
- `frontend/data.py` passes those fields through `/api/plan`.
- The Overview Solar card now shows the optimizer-adjusted remaining PV as the main
  value and explicitly labels the raw source underneath as `VRM forecast`. This avoids
  hiding provenance while making the card match the schedule the optimizer is actually
  using.

Important design choice:

This is deliberately not behind `PV_WEATHER_APPLY`. Weather PV shaping is a forecast
model that still needs validation. The nowcast correction is based on measured
production from the live system and recent settlement data, so it is a control-time
reconciliation layer rather than a speculative weather-model apply gate.

### Forecast accuracy and monthly net

`frontend/data.py` and `frontend/static/js/charts.js` now support:

- SoC/price horizon chart with a now marker,
- actual-vs-forecast PV/load overlay,
- point tooltips for forecast accuracy data,
- toggleable PV/load legend visibility,
- toggleable SoC/price, weather forecast, and weather impact legend visibility,
- monthly net chart,
- separate in-progress actual and projected full-day points for today, and
- weather charts/tooltips for the desktop Weather tab.

## Dashboard changes

The dashboard has been reorganized around current operator workflows:

- **Overview** is the default entry point.
- **ESS** opens directly to tabs and module content without the old top overview
  cards on desktop.
- Desktop has an ESS Weather tab between Vic Schedule and Advisor.
- Mobile has a compact sticky header, swipeable status chips, and bottom navigation:
  **Menu · Flow · Schedule · Trends · Advisor**.
- Mobile Schedule/Trends/Advisor/Flow hide redundant overview cards and jump to the
  relevant content.
- Battery and Venus external views are scaled/fitted in their panes and remain
  scrollable while scrollbar chrome is hidden.
- Top-left branding returns to the default Overview entry point on desktop and mobile.
- Timeline now includes a moving "Today so far" ledger row immediately above the
  current hour, showing settled cost/profit accumulated from midnight to now.

Guarded operator controls:

- allow-listed `.env` edits through `frontend/config_schema.py`,
- `POST /api/replan`,
- `POST /api/restart` publishing the existing supervised restart MQTT message, and
- `POST /api/control/ai-override`, which idles Victron once and makes the AI ESS
  optimizer stand down until toggled off,
- `POST /api/control/grid-assist`, which reuses the existing `grid_charging_enabled`
  manual retain/grid-assist path, and
- `POST /api/victron/clear-schedule` using the existing Victron helper.

The Advisor remains read-only.

## Advisor changes

`frontend/advisor.py` now behaves like a persistent chat session:

- streams model output to the browser,
- renders common Markdown/table output,
- saves the current session to `data/advisor_latest.json`,
- restores the session after refresh,
- timestamps prompts and responses,
- shows newest exchanges first,
- includes compact conversation context in follow-up prompts,
- can retrieve extra history days on demand,
- supports copy buttons, exchange deletion, and clear chat.

Advisor auth remains via subscription-login CLI by default through `ADVISOR_CLI_CMD`.

## Config and deployment changes

New config path helpers:

- `lib/config_paths.py`
- `tests/test_config_paths.py`

These centralize where `.env` / `.secrets` are read and written. For Kubernetes,
`APP_ENV_PATH` should point to the writable env file when `.env` is mounted outside
the working directory. The writable `.env` should be mounted through a read/write
directory, not as a single immutable file, because dashboard config writes use
write-then-rename semantics.

`.env.example`, `frontend/config_schema.py`, `README.md`, and `frontend/README.md`
were updated so the exposed knobs match the current optimizer.

## Tests added or expanded

The branch expands regression coverage for:

- daily-settlement policy selection,
- economic grid-charge behavior,
- grid-charge SoC cap behavior,
- removed price-cap tunables staying out of user-facing config,
- weather fetch/cache/shadow summaries,
- config path resolution,
- frontend data summaries and charts,
- mobile UX static behavior,
- frontend server routes, and
- energy-broker history/forecast helpers.

New untracked test files intended for this branch:

- `tests/test_config_paths.py`
- `tests/test_weather.py`

## Removed or deprecated behavior

- `ESS_MAX_GRID_CHARGE_PRICE` and `ESS_GRID_CHARGE_CHEAP_PCT` are no longer active
  user knobs and should be removed from live `.env` files.
- The dashboard should no longer be described as visibility-only. It has guarded
  config/replan/restart/schedule-clear controls.
- The optimizer should no longer be described as monthly-settlement-first. It is now
  daily-settlement-first with learned exceptional-future exceptions.
- The raw solar forecast in UI text should be called `VRM forecast`; the adjusted
  schedule/card value should be called optimizer-adjusted or optimizer nowcast, not
  "raw forecast".
- User-facing action labels are `IDLE`, `RETAIN`, `BUY`, and `SELL`.

## Operational notes before merge

- Commit the currently untracked weather/config-path files if they are intended to
  ship with this branch.
- Remove obsolete grid-charge price-cap keys from production `.env`; leaving them in
  place should not affect control, but it will confuse operators.
- Keep `HVAC_LOAD_APPLY=False` and `PV_WEATHER_APPLY=False` until enough weather
  history proves the adjustment reduces forecast error.
- In Kubernetes, set `APP_ENV_PATH` to the mounted writable `.env` path if it is not
  `/app/.env`.
- Restart the dashboard/main service after deployment so new MQTT subscriptions,
  routes, and static assets are loaded.
- When reviewing sunny-day behavior, compare `pv_remaining_raw_wh` with
  `pv_adjusted_remaining_wh` in `/api/plan` or `/dev/shm/cerbo_ai_plan.json` before
  assuming the top Solar card is showing VRM directly.

## Suggested validation

Run:

```bash
export DEV=1
python -m pytest -s -q
python scripts/ai_ess_dryrun.py --json
```

Then inspect:

```bash
jq '.planning_policy, .optimizer_guardrails, [.schedule[] | select(.control_action=="BUY" and .grid_energy > 0)]' /dev/shm/cerbo_ai_plan.json
```

In the browser, verify:

- Overview is the default entry point.
- ESS tabs, Trends, Weather, Advisor, Victron Schedule, Battery, and Venus open.
- The Configuration tab no longer exposes removed grid-charge price-cap knobs.
- Trends shows actual and projected today points plus forecast-accuracy tooltips.
- Weather charts render with readable tooltips.
- SoC/price, Weather forecast, Forecast impact, and Forecast accuracy legends toggle
  series visibility consistently.
- Solar card shows adjusted remaining PV as the main number and `VRM forecast` as
  source-labelled subtext. In JSON, confirm `pv_remaining_raw_source` is
  `"VRM forecast"` and `pv_adjusted_remaining_source` is present when the optimizer
  has a schedule.
- Override and Grid assist buttons reflect live retained MQTT state on desktop and
  mobile, and the mobile copies live in the hamburger menu.
- Advisor chat persists across refresh and can clear/delete exchanges.
