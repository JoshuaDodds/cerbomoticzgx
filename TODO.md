# TODO / roadmap

- **Weather forecast validation / apply tuning** — Open-Meteo fetch/cache, Weather tab,
  history recording, and the HVAC/PV apply gates are implemented. Production `.env`
  may enable `HVAC_LOAD_APPLY` / `PV_WEATHER_APPLY`, and the current plan confirms
  they change forecasts (`hvac_apply=True`, `pv_apply=True`). Do **not** mark this
  effective yet: the first partial 2026-06-28 sample only has afternoon/evening
  weather rows and the load counterfactual was slightly worse with weather
  (weather MAE ~0.240 kWh vs base ~0.227 kWh on non-trivial load rows). Keep
  collecting data, fit/tune `HVAC_ALPHA_COOL` / `HVAC_ALPHA_HEAT`, and only call the
  apply phase done after multiple full days show lower forecast error.

## AI Advisor
- Phase 2 — approve-to-apply for tunables only. Each setting has hard min/max bounds;
  on approval the system runs a dry-run backtest, shows projected EUR, writes .env
  (hot-reloads, no restart), and auto-reverts if the next day underperforms. Bounded
  numbers can't crash the controller — this is the safe sweet spot.

- Phase 3 — code changes via PR, not hot-patch. Let the model propose a diff + tests;
  the "apply" button opens a PR/branch for human review and your normal pytest gate.
  Keep a human on the actual diff before anything restarts a 16 kW controller.

# Bugs / Testing

## MEDIUM — wrong behaviour / cost leak, not dangerous

### M1 — Victron evcharger amps under-read ~3× (hardware config)
The Victron CT ratio/phase config should be corrected so the meter reads 
true per-phase amps. It's still used by `_charging_now`/gates as a 
"drawing?" threshold, which is fine at any scale, but a correct meter 
is preferable for accuracy. This is a hardware/config task, not a code change.

## LOW

- **L3 — Two `update_charging_amp_totals` implementations** (in
  `event_handler` and `ev_charge_controller`) both set
  `tesla_charging_amps_total`. Same value, but duplicated logic —
  consolidate to one source to avoid future drift.

## Verify operationally (not a bug)

- The Victron CT scaling (M1) is the one to chase on the hardware/config
  side — it affects both the surplus-match accuracy and (previously) the
  redundant-command spend.
- Confirm `retained: true` on the fleet-telemetry dispatcher persists
  across receiver restarts (so the bridge always snapshots current state on
  connect).
