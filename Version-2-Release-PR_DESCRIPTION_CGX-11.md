# CGX-11: AI ESS optimizer, dashboard, history, and anti-churn controls

## What / Why
This branch replaces the broken AI ESS prototype with a working, safety-gated
optimizer for the 16 kW Victron ESS. The previous engine crashed on live Tibber
data, planned at the wrong slot duration, had no terminal value for stored energy,
and could mislabel PV surplus as active selling. The new implementation plans
across the current Tibber horizon with PV/load forecasts, applies only deliberate
control actions, exports a read-only dashboard view, and records history for
forecast-vs-actual learning.

The latest updates also address live behavior seen after manual low-SoC testing:
near-empty batteries no longer churn SELL->HOLD->SELL on tiny price differences,
and the manual `grid_charging_enabled` override is honored again while the AI
optimizer is active.

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

## Config / Docs
- Added and documented AI ESS tunables in `.env.example`, `.env`, README, and
  frontend docs, including cycle cost, arbitrage margin, grid-charge ceilings,
  min sell price, expected peak price, terminal value, optimizer step size, cost
  basis path, and SELL hysteresis thresholds.
- Added `AI_PLAN_EXPORT_PATH`, `FRONTEND_HOST`, `FRONTEND_PORT`, and history/cost
  basis paths for sidecar operation and analytics.
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
- The same run flagged a high load forecast (`64.4 kWh` over the reported horizon),
  so forecast quality should be watched rather than tuned around blindly.

## Risk / Rollback
- Risk is high because this controls a live 16 kW 3-phase ESS. The optimizer now
  writes charge schedules, export/retain setpoints, min-SoC reserve, and optional
  feed-in limits.
- New protective logic is conservative: cost-basis failures are best-effort and
  do not block control, the cost-basis floor only prevents active discharge,
  hysteresis only suppresses marginal SELL entry, and history writes are
  best-effort.
- Rollback is straightforward: set `AI_POWERED_ESS_ALGORITHM=False` to return to
  legacy control. Negative-price feed-in limiting is separately gated by
  `NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED`.
- Deleting `data/ess_cost_basis.json` resets the cost basis. `/dev/shm` hysteresis
  state is temporary and resets on reboot.
- Confirm the Venus OS `Settings/CGwacs/MaxFeedInPower` path on hardware before
  relying on negative-price limiting.

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
   summaries render correctly.
9. After at least one slot boundary, run `python scripts/history_report.py` and
   confirm settlement records include predicted-vs-actual values.

## Links
- Jira: CGX-11
- Branch: `CGX-11-ai-ess-optimizer-8967993876044487087`

