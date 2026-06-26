# CGX-11: AI ESS optimizer, dashboard, history, and anti-churn controls

## What / Why
This branch replaces the broken AI ESS prototype with a working, safety-gated
optimizer for the 16 kW Victron ESS. The previous engine crashed on live Tibber
data, planned at the wrong slot duration, had no terminal value for stored energy,
and could mislabel PV surplus as active selling. The new implementation plans
across the current Tibber horizon with PV/load forecasts, applies only deliberate
control actions, exports a read-only dashboard view, and records history for
forecast-vs-actual learning.

Later updates address live behavior observed during manual low-SoC testing and a
June 18–20 data/code review of the running system:
- Near-empty batteries no longer churn SELL->HOLD->SELL on tiny price differences,
  and the manual `grid_charging_enabled` override is honored again while the AI
  optimizer is active.
- The PV forecast self-corrects intraday so a better-than-forecast day is no longer
  starved as VRM's fixed daily total is consumed.
- Charging is reported as full-power-to-target so BUY settlement predictions match
  how the Victron actually charges, and several data-logging artifacts were fixed.
- Power limits and a HOME_CONNECT flag bug were corrected against measured reality.

Two consecutive full post-review days were both profitable (about `EUR +14.66` and
`EUR +5.22` realized net), with the anti-churn controls firing as designed.

A further round of work added a read-only **AI Advisor**, substantially expanded the
dashboard, added a month-to-date profit figure, and corrected the documented control
model:
- An **AI Advisor** tab (Phase 1) gives a manually-triggered, streaming, plain-
  language review of recent performance and answers open questions ("why did we sell
  at 15:00 yesterday?"). It authenticates through a **subscription-login CLI**
  (Claude Code, Gemini, or OpenAI Codex via `ADVISOR_CLI_CMD`) — never a pay-as-you-go
  API key — caps token usage hard, and can pull deeper days from history on demand.
- The dashboard gained a collapsed **previous-day settled-schedule** view, a
  **month-to-date net** chart, and a sticky-header **month-to-date profit** chip
  (the sum of our settled daily totals for the current month).
- The live power-flow diagram was rebuilt HASS/Domoticz-style — no central hub,
  direct source-coloured flows with per-connector watt labels — so grid/battery/
  solar power can be told apart end-to-end.
- Startup was made fully **non-blocking** so a Tibber outage can't delay or crash the
  service.
- **Control model clarified:** this ESS runs *ESS Optimized (without BatteryLife)*,
  driven by commanded setpoints under DVCC (the inverters report "External control").
  There is no BatteryLife scheduling — earlier wording to that effect was corrected.

## How (High Level)
- Rewrote `lib/ai_powered_ess.py` as a SoC-to-SoC dynamic program that supports
  hourly or quarter-hour Tibber prices, PV/load forecasts, charge/discharge
  efficiencies, export pricing, terminal stored-energy value, seasonal min-SoC
  reserve, and configurable optimizer granularity.
- Added live Tibber quarter-hour price fetching with fallback paths, tomorrow PV
  forecasting, per-slot load forecasting, and a 13:05 next-day replan flow.
- Added Victron negative-price feed-in protection via `MaxFeedInPower`, gated by
  config and restored automatically when the condition clears.
- Standardized the user-facing action model to `IDLE`, `RETAIN`, `BUY`, and
  `SELL`, derived from the commanded setpoint. Console output, plan JSON, history,
  and the web UI now use the same labels.
- Added per-slot settlement records to daily NDJSON history, pairing the prior
  prediction with actual import/export, cost/reward, SoC delta, and PV. Added
  `scripts/history_report.py` for daily realized EUR, forecast error, load shape,
  and PV-shape analysis.
- Added a read-only dry-run tool, `scripts/ai_ess_dryrun.py`, for safe optimizer
  tuning without writing to Victron, MQTT, or service state.
- Added a standalone Flask dashboard in `frontend/` that reads the exported plan,
  displays current decision, live MQTT telemetry, power flow, day/horizon net,
  solar forecast, schedule drill-downs, and allow-listed `.env` tuning controls.
- Added persistent ESS cost-basis tracking in `lib/ess_cost_basis.py`. Grid
  charging raises the stored-energy basis, PV charging dilutes it, and the
  optimizer refuses active grid discharge below the effective basis-adjusted sell
  floor.
- Added SELL hysteresis in `lib/energy_broker.py` so the controller may suppress
  re-entering SELL after a recent stop unless the price improves enough. This only
  blocks marginal sells; it never forces or prolongs discharge.
- Restored the manual `grid_charging_enabled` override under AI mode. When set,
  the controller forces RETAIN so grid covers house load and EV charging without
  draining the battery, including in the fast load-change path.
- Added intraday PV self-correction in `lib/energy_broker.py`
  (`_pv_intraday_remaining_kwh`). The per-slot PV forecast is a daily magnitude
  distributed by a learned **daylight-only, trailing-3-day** shape
  (`_pv_shape_by_slot`); since VRM anchors the magnitude to a fixed daily total,
  the remaining estimate is now scaled **up** toward a projection from actual
  production so far (damped, capped, and only after enough daylight has elapsed).
  It only ever raises the forecast and is a no-op on a normal day.
- Modeled charging as full-power-to-target for reporting/settlement
  (`_frontload_charging` in `lib/ai_powered_ess.py`). The system charges at full
  power until the target SoC (commanded via setpoints under DVCC — *ESS Optimized
  without BatteryLife*), but the DP tie-breaks to a gentle trickle on flat-price
  windows, which made BUY settlements read far too low. The reported per-slot
  trajectory is re-timed to match; the Victron charge windows are derived from the
  original trajectory **before** re-timing, so control is unchanged.
- Set the optimizer power limits from observed peaks (last 5 days): grid export
  `13 kW`, battery discharge `15 kW`, export setpoint `-13000 W` (down from 16 kW),
  so planned discharge revenue and SoC trajectories are realistic.
- Added a midnight counter-rollover guard and a `realized_action` history field
  (`lib/energy_broker.py`): pre-dawn PV yield and a not-yet-reset consumption
  counter are zeroed in the first record of the day, and each record now also logs
  what the system was actually doing (from live grid/battery flow) so a decision
  logged mid-transition no longer looks mislabeled.
- Fixed a shared boolean-parsing bug with `is_truthy()`
  (`lib/helpers/__init__.py`): `bool("False")` is truthy, so
  `HOME_CONNECT_APPLIANCE_SCHEDULING=False` (and similar flags) were always treated
  as enabled. `main.py` and `lib/event_handler.py` now parse the value correctly.
- Frontend: the Live power-flow battery node now shows power large with `% SoC`
  beneath, matching the other nodes.

## Dashboard, Advisor & live insights (read-only)
All of the following is strictly read-only — a separate Flask process that reads the
published plan JSON, history NDJSON, `.env`/`.secrets`, and (for the advisor) shells
out to a local CLI. None of it imports the control path or writes Victron/MQTT state.

- **AI Advisor tab** (`frontend/advisor.py`): a manually-triggered analyst. It sends
  the recent performance history + the allow-listed tunables (never secrets) + the
  current plan + the **live power flow** (`live_now` — real-time grid/PV/battery/load,
  ground truth for "right now") to an LLM and streams a short, skimmable markdown
  review back into the tab (SSE, live stage/log/delta events). It also answers
  free-text questions ("why did we sell at 15:00 yesterday?"). The prompt makes the
  model treat `live_now` as truth (and understands daylight/partial-day PV) so it
  describes what the system is actually doing, not just the plan's forecast label.
  - **Subscription-login auth, never a metered API key.** `ADVISOR_CLI_CMD` points
    the advisor at any subscription CLI — Claude Code, Gemini (`gemini -p {prompt}`),
    or OpenAI Codex (`codex exec {prompt}`); usage is drawn from that plan. A Claude
    Code OAuth token path and an `ANTHROPIC_API_KEY` path also exist but are not the
    default. Extended thinking is disabled (`ADVISOR_MAX_THINKING_TOKENS=0`) and the
    prompt is hard-capped (`ADVISOR_MAX_INPUT_CHARS`) so a review costs a few K tokens.
  - **On-demand history retrieval.** The prompt includes a manifest of every day in
    `data/history/`; when a question needs days beyond the inline window the model
    replies with a `NEED_HISTORY: <dates>` directive and the advisor pulls exactly
    those day files (bounded by `ADVISOR_RETRIEVAL_MAX_DAYS/CHARS`) and re-asks —
    streaming only the final answer.
  - **Tuned review prompt:** capped length, a "no changes recommended" escape hatch
    (it won't invent improvements), a self-check that every tunable it suggests
    actually exists and changes the current value, and explicit suppression of
    low/0% SoC "concerns" (intended behaviour). The primer describes the real control
    model (no BatteryLife) so the model stops attributing behaviour to it.
- **Month-to-date profit chip** (sticky header): the sum of our settled daily totals
  for the current calendar month (`Σ export_reward − Σ import_cost`, profit positive),
  including today's running total. Deterministic and always available. (A live Tibber
  GraphQL fetch was prototyped but dropped — its monthly endpoint only exposes
  completed months and didn't reflect mid-month bonuses; a manual
  `scripts/tibber_mtd_probe.py` diagnostic remains if that angle is revisited.) The
  **Today** and **Month** header chips show a signed € value (green `+` / red `−`),
  no "profit"/"cost" word.
- **Previous-day schedule view** (Schedule tab): a collapsed row above today's tree
  that expands into the prior day's *settled* hour→15-min schedule (same colours and
  drill-downs), for a continuous 2–3 day view. Actual consumption for past days is
  derived from the cumulative `load_actual_today_wh` counter already in the cycle
  records.
- **Monthly net chart** (Trends tab): per-day net €/profit for the current month with
  hover tooltips, from a new `/api/history/month` endpoint.
- **Live power-flow rebuild** (`frontend/static/js/powerflow.js`): removed the central
  hub and now draws **direct source→sink flows** from a flow decomposition (PV→house/
  battery/grid, battery→house, grid→house/battery); every flow dot keeps its source
  colour end-to-end, with thicker ribbon connectors and a watt label per active
  connector. The build-once / mutate-in-place model is preserved, so dots never freeze.
- **Power-flow v2 — VRM-style cards in the Victron physical topology**
  (`frontend/static/js/powerflow.js`, `frontend/live.py`): nodes became **rounded-
  rectangle info cards** (à la Victron GUI-v2) wired in the **real topology** — Grid —
  **Inverter/Charger** — AC Loads (AC bus), Inverter/Charger — Battery (DC link),
  **Solar — Battery** (PV is DC-coupled), **EV — AC Loads**, and **Gas — AC Loads**
  (EV + Gas as two compact cards beneath AC Loads). Smooth HASS-style
  connectors stay **faintly visible** (the topology always reads) with source-coloured
  dots travelling in the direction of real power; the Grid headline shows **► import /
  ◄ export**. Cards carry **per-phase L1/L2/L3**, **battery temp·V·A·SoC·time-to-go**,
  **EV session**, and a central **Inverter/Charger SystemState** node. The SVG is
  **responsive** — it measures its container and re-lays everything to fill width
  **and** height via a `ResizeObserver`, for embedding on any screen (the old
  `max-width:720px` cap and the "Live power flow" heading were removed). Box sizes are
  **deliberately non-uniform** — a wider central Inverter/Charger + Battery column and
  small EV/Gas cards, echoing the Victron GUI-v2 proportions (per-box font scaling). `live.py`
  gained read-only subscriptions for all of the above (topics mirror `lib/constants.py`,
  incl. battery temp on the LFP/BMS service 512; absent topics degrade to hidden). Gas
  is shown as a small card beneath AC Loads (from the plan's `gas_m³`). **Requires a
  dashboard restart** to register the new subscriptions. Tests:
  `tests/test_93_live_snapshot.py`.
- **Per-slot predicted-vs-actual storage** (`lib/energy_broker.py`): settlement
  records now also carry `predicted_pv_kwh`, `predicted_load_kwh`, and
  `actual_load_kwh`, enabling forecast-accuracy analysis and future overlays. The
  advisor's history payload exposes PV/load forecast-vs-actual (normalising the
  mislabeled `pv_forecast_today_kwh`, which is actually Wh).
- **Schedule-vs-actual consistency:** when SELL hysteresis or the manual override
  changes the committed action, `schedule[0]` is synced to it (action, reason, mode,
  held SoC), so the Schedule tab no longer shows a phantom SELL the controller didn't
  take.
- **Non-blocking startup** (`main.py`, `lib/tibber_api.py`): the Tibber account is
  initialised on a background daemon thread with capped backoff, and the frontend +
  pricing/forecast warm-up are deferred off the critical path, so a Tibber 504/outage
  can neither block the web server nor crash service start.
- **Container/CI:** the Dockerfile installs Node 20 + the Claude Code CLI for the
  advisor; `anthropic` is added to `requirements.txt` for the optional API path.

## Notable Fixes
- Handles Tibber ISO timestamp strings without crashing.
- Detects native price slot duration instead of assuming 15 minutes.
- Avoids end-of-horizon battery dumping with terminal value.
- Handles empty schedules and missing settings safely.
- Uses separate buy/sell pricing via export factor and fee.
- Treats `0%` SoC as a valid battery state instead of missing data.
- Prevents legacy control paths from clobbering AI export/charge setpoints.
- Uses one min-SoC reserve source of truth for optimizer planning and Victron
  hardware floor.
- Avoids classifying PV-surplus export as active battery SELL.
- Stops the PV forecast collapsing to ~0 on a better-than-forecast day.
- Makes BUY settlement predictions match real full-power charging (were 2–20x low).
- Removes the midnight rollover artifact (first record carried yesterday's totals).
- Fixes `HOME_CONNECT_APPLIANCE_SCHEDULING=False` being ignored (`bool("False")`).
- Live power-flow no longer loses source colour at a central hub — grid/battery dots
  no longer read as "solar"-yellow into the house.
- Schedule tab no longer shows a SELL the controller suppressed via hysteresis.
- Month-to-date figure: robust per-day totals (max of each cumulative counter, so a
  partial/garbled final record can't drop a whole day), summed for the current month.
- Frontend template/asset edits now show without a hard refresh
  (`TEMPLATES_AUTO_RELOAD`, `SEND_FILE_MAX_AGE_DEFAULT=0`).
- Silenced repeated "Set AC Power Set Point to 0.0 W" retain-mode log spam (deadband
  + silent no-op when the setpoint is unchanged).
- A Tibber API outage no longer blocks the web server or crashes startup.

## Config / Docs
- Added and documented AI ESS tunables in `.env.example`, `.env`, README, and
  frontend docs, including cycle cost, arbitrage margin, grid-charge ceilings,
  min sell price, expected peak price, terminal value, optimizer step size, cost
  basis path, and SELL hysteresis thresholds.
- New tunables: `ESS_PV_SHAPE_DAYS`, `ESS_PV_INTRADAY_CORRECTION`,
  `ESS_PV_INTRADAY_MAX_RATIO`, `ESS_PV_INTRADAY_MIN_ELAPSED` (intraday PV
  correction); `ESS_MODEL_CHARGE_RATE` (full-power-to-target reporting).
- Tuning changes: `ESS_MAX_GRID_EXPORT_KW=13`, `ESS_MAX_DISCHARGE_KW=15`,
  `ESS_EXPORT_AC_SETPOINT=-13000` (from measured peaks); `ESS_ARBITRAGE_MARGIN`
  raised `0.00 -> 0.03` as a forecast-error cushion (the large midday->evening
  spreads still clear it).
- Added `AI_PLAN_EXPORT_PATH`, `FRONTEND_HOST`, `FRONTEND_PORT`, and history/cost
  basis paths for sidecar operation and analytics.
- **AI Advisor config** (`.env` / `.env.example`, secrets in `.secrets` /
  `.secrets-example`): `ADVISOR_AUTH` (`auto|cli|api`), `ADVISOR_CLI_CMD` (point at a
  subscription CLI — `gemini -p {prompt}`, `codex exec {prompt}`, …), `ADVISOR_MODEL`,
  `ADVISOR_HISTORY_DAYS`, `ADVISOR_MAX_THINKING_TOKENS` (0 = off), `ADVISOR_MAX_INPUT_CHARS`,
  `ADVISOR_RETRIEVAL_MAX_DAYS` / `ADVISOR_RETRIEVAL_MAX_CHARS`, `CLAUDE_CLI_PATH`,
  `CLAUDE_CONFIG_DIR`, and the secrets `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`.
- The month-to-date chip needs no new config — it sums the existing daily history.
- New read-only endpoints: `GET /api/advisor` + `GET /api/advisor/stream` (SSE),
  `GET /api/history/month`, `GET /api/history/day`.
- New tests: `tests/test_advisor_retrieval.py` (NEED_HISTORY parsing + PV/load unit
  normalisation) and `tests/test_ess_pv_headroom.py` (grid-vs-PV charge trade-off).
  `scripts/tibber_mtd_probe.py` remains as a manual Tibber diagnostic (unused by the
  app since the MTD chip sources from our own logs).
- Updated test and validation notes for the dry-run workflow, dashboard, history
  logging, and live operational checks.

## Tuning / Validation Notes
- The dry-run workflow is intentionally read-only and should be used for tuning
  before changing live values.
- A tuning pass found the baseline snapshot at about `EUR 2.45` forecast profit
  and `1.405` estimated cycles. `ESS_MAX_GRID_CHARGE_PRICE=0.23` improved that
  snapshot to about `EUR 2.56` while reducing estimated cycles to `0.780`.
- Lowering `ESS_BATTERY_CYCLE_COST` to zero produced higher paper profit but more
  cycling; that is not recommended because it is fragile to forecast error and
  underprices battery wear.
- June 18–20 live review: two full post-fix days were profitable (`EUR +14.66`,
  `EUR +5.22`); cost-basis, hysteresis, the sell floor, and the PV self-correction
  all behaved as designed (e.g. a single `SELL_DAMPED_HYSTERESIS` instead of churn).
- Observed peak grid export was ~`12.2 kW` and battery discharge ~`14.7 kW`, higher
  than an earlier ~10 kW estimate; ceilings were set from these.
- "SoC not refreshed before each plan" was investigated and is **not** a bug:
  `STATE.get('batt_soc')` reads SQLite fresh each cycle and the Victron keepalive
  (30 s) keeps live SoC publishing.
- Known remaining gap: the load forecast does not see ad-hoc EV fast-charging
  (17–19 kW midday spikes), so per-slot net predictions diverge during those
  sessions. Watch forecast quality rather than tuning around it blindly.
- A close review of a heavy midday grid-precharge day confirmed the optimizer already
  weighs grid-charge cost against the PV-export opportunity cost (the DP nets PV vs
  load and prices export revenue). Front-loading grid when midday grid is *cheaper*
  than the afternoon export price is therefore correct, not blind. On a day where the
  afternoon export price drops below the grid trough it leaves room for PV instead.
  `tests/test_ess_pv_headroom.py` pins this adaptive behaviour as a regression guard.

## Risk / Rollback
- Risk is high because this controls a live 16 kW 3-phase ESS. The optimizer now
  writes charge schedules, export/retain setpoints, min-SoC reserve, and optional
  feed-in limits.
- New protective/analytic logic is conservative and safe: cost-basis failures are
  best-effort and never block control; the cost-basis floor only prevents active
  discharge; hysteresis only suppresses marginal SELL entry; the PV correction only
  ever raises the forecast and falls back to the VRM value on error; the charge-rate
  re-time is reporting-only (Victron windows + the BUY setpoint of 0 unchanged) and
  wrapped in try/except; the midnight guard and `realized_action` are logging-only;
  and history writes are best-effort.
- Rollback is straightforward: set `AI_POWERED_ESS_ALGORITHM=False` for legacy
  control. Feature kill switches: `ESS_PV_INTRADAY_CORRECTION=0`,
  `ESS_MODEL_CHARGE_RATE=0`, `ESS_ARBITRAGE_MARGIN=0.00`, and restoring the prior
  16 kW power ceilings. Negative-price feed-in limiting is separately gated by
  `NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED`.
- Deleting `data/ess_cost_basis.json` resets the cost basis. `/dev/shm` hysteresis
  and sell-state files are temporary and reset on reboot.
- Confirm the Venus OS `Settings/CGwacs/MaxFeedInPower` path on hardware before
  relying on negative-price limiting.
- The dashboard and AI Advisor are **read-only and out-of-process**: they read the
  plan/history/`.env`/`.secrets` and (for the advisor) shell out to a local CLI, but
  never import the control path or write Victron / MQTT / config. The advisor surfaces
  any error in its tab. To disable, simply don't run the frontend (or leave
  `ADVISOR_CLI_CMD`/tokens unset so the Advisor stays idle).

## Human Testing Criteria
1. Run `export DEV=1 && pytest -q` with pyenv Python 3.11.
2. Run `python scripts/ai_ess_dryrun.py --json` and confirm no writes occur, the
   `ENGINE TUNABLES` block reflects `.env`, and the schedule uses the expected
   `IDLE` / `RETAIN` / `BUY` / `SELL` labels.
3. With `AI_POWERED_ESS_ALGORITHM=True`, confirm `run_ai_optimizer` logs a plan,
   exports plan JSON atomically, and programs charge slots with target SoC.
4. Inject or wait for a negative price and confirm `MaxFeedInPower` is set to `0`,
   then returns to `-1` when the price is non-negative.
5. Restart the service and confirm `data/ess_cost_basis.json` appears, rises after
   grid charging, and dilutes after PV charging.
6. On a flat high-price morning or replayed low-SoC scenario, confirm SELL churn is
   suppressed with `SELL_DAMPED_HYSTERESIS` / `SELL suppressed by hysteresis`.
7. Toggle `grid_charging_enabled` on and confirm RETAIN holds the battery while
   grid covers house load and EV charging; toggle off and confirm AI resumes.
8. Start the frontend with `python -m frontend` and confirm current decision, live
   feed status, schedule drill-downs, config display/editing, and day/horizon
   summaries render correctly. The Live battery node shows power large, `% SoC`
   beneath.
9. After at least one slot boundary, run `python scripts/history_report.py` and
   confirm settlement records include predicted-vs-actual values, a `realized_action`
   field, and that the 00:00 record no longer carries a full day of PV/load.
10. On a sunny day that beats the VRM forecast, confirm the rest-of-day PV forecast
    no longer collapses to ~0. On the next BUY window, confirm settlement
    `predicted_grid_kwh` tracks `actual_import_kwh` (no 2–20x gap). Confirm the
    startup log does not claim HomeConnect is enabled while it is set `False`.
11. **Advisor:** with `ADVISOR_CLI_CMD` set to a working subscription CLI (or
    `ADVISOR_AUTH=cli` and a logged-in `claude`), open the Advisor tab, run a review,
    and confirm it streams a short report. Ask a deep question (e.g. "how did we do
    the week of June 1–7?") and confirm a "Pulled N day(s)…" stage before the answer.
    Confirm the run cost is a few K tokens (thinking disabled).
12. **Month-to-date chip:** confirm the header shows a signed **Month** € value
    (green for profit, red for loss; no "profit"/"cost" word) equal to this month's
    `Σ export_reward − Σ import_cost` from the daily history (tooltip shows the
    breakdown). The **Today** chip is likewise a signed €.
13. **Schedule history + power-flow:** expand "Previous day" in the Schedule tab and
    confirm yesterday's settled slots render with real consumption. On the Live tab,
    confirm grid/battery/solar flows keep their own colour into the house (no central
    hub) with per-connector watt labels.

## Links
- Jira: CGX-11
