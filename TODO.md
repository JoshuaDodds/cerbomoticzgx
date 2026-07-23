# TODO / roadmap

- **Daily-net forecast calibration** — The Trends chart now treats intraday values as
  time-ordered forecast revisions rather than independent statistical samples: one latest
  value per 15-minute period, full observed range without outlier labels, complete-day
  coverage checks, and a comparable latest-full-day marker for today. Five complete
  2026-07-18–22 days show that the final forecast is already close (about €0.23 MAE), but
  earlier forecasts are optimistic: approximately €2.40–€2.48 MAE from midnight through
  18:00 with +€1.02 to +€2.48 profit bias. New history rows separately persist remaining
  forecast import cost and export reward so this can be attributed instead of guessed.

  - Collect at least 7 complete days, preferably 14, with
    `forecast_remaining_import_cost_eur` and
    `forecast_remaining_export_reward_eur`.
  - Recalculate error by time-to-settlement and attribute the positive bias to predicted
    import cost, export reward, or both. Check EV/appliance days separately.
  - Tune the underlying forecast only after the component history identifies the source;
    require lower morning/midday MAE without degrading the approximately €0.23 closing MAE.

- **Weather forecast validation / apply tuning** — The first 21-full-day validation
  found the original apply model harmful: load MAE was 0.2163 kWh/slot with weather
  versus 0.1155 without it, and weather improved 0/21 days. Root causes were full-day
  HVAC demand being reallocated into every shrinking remaining-day horizon, absolute
  HVAC demand being added to a trailing baseline which already contained HVAC, a
  compass/Open-Meteo azimuth convention mismatch, preceding-hour GTI being assigned
  to the following hour, and fresh 0 W sunset evidence being discarded. The
  `hvac-pv-validation-tuning` branch repairs these and records explicit baseline,
  shadow, and final forecasts. Summer now evaluates cooling anomalies only; Winter
  evaluates heating anomalies only. Keep `HVAC_LOAD_APPLY=False` and
  `PV_WEATHER_APPLY=False` while collecting repaired shadow rows. Re-enable each gate
  independently only after multiple full holdout days show a material reduction in
  error; summer history tentatively supports `HVAC_ALPHA_COOL=2.0`, while heating
  still requires winter data.

  Immediate validation of the repaired implementation has passed: configuration and
  provider azimuth, 144-hour weather coverage, cooling-mode selection, disabled apply
  gates, bounded slot adjustments, Weather-tab presentation, and the new settlement
  fields all checked out. Deferred operational validation that needs future slots or
  additional seasons remains:

  - Over several replans and through the end of a day, confirm the HVAC adjustment
    remains stable as the horizon shrinks, does not accumulate into late slots, and
    stays below the temporary investigation threshold of 0.5 kWh per 15-minute slot.
  - At sunset on multiple days, confirm a fresh live 0 W PV reading suppresses any
    stale near-term PV forecast. `pv_nowcast_source=live_drop` is expected when a
    correction is needed; no applied correction is correct when the baseline is
    already zero.
  - After at least 7 complete days, preferably 14 with varied temperature and cloud
    cover, compare baseline versus shadow forecasts against settlement measurements.
    Require roughly 5% lower combined MAE, improvement on a majority of complete
    days, no materially worse bias, and no recurring oversized adjustments before
    enabling either apply gate.
  - Evaluate HVAC and PV separately and enable at most one apply gate at a time,
    followed by another multi-day observation period to catch optimizer-plan
    instability or unintended schedule changes.
  - Do not enable or declare the heating model validated from summer cooling data.
    Collect and evaluate at least 7–14 complete winter-mode days with meaningful
    heating demand before selecting `HVAC_ALPHA_HEAT` or enabling winter HVAC apply.

## AI Advisor
- Phase 2 — approve-to-apply for tunables only. Each setting has hard min/max bounds;
  on approval the system runs a dry-run backtest, shows projected EUR, writes .env
  (hot-reloads, no restart), and auto-reverts if the next day underperforms. Bounded
  numbers can't crash the controller — this is the safe sweet spot.

- Phase 3 — code changes via PR, not hot-patch. Let the model propose a diff + tests;
  the "apply" button opens a PR/branch for human review and your normal pytest gate.
  Keep a human on the actual diff before anything restarts a 16 kW controller.

## EV smart-charge scheduling — operator validation / learning follow-up

Phase 2 implementation is complete on `optimized-ev-charging`: one durable
target-SoC/ready-by job, a pure 15-minute cost/PV planner, feasibility and
latest-safe-start calculation, Summer/Winter ESS load integration, separate
shadow/apply gates, readable day-by-day Vehicle plan, budget-aware Fleet execution, an
application-owned Tesla fallback schedule, and one durable Pushover plug reminder.
The sub-5 A double-send workaround is intentional. Maxem remains authoritative
for 25 A/phase overload protection; the controller never chases lower ABB power.
Jobs beyond 48 hours are automatically paced across local days at each day's cheapest
available time, with cost-effective forecast solar allowed to advance later grid demand;
shorter jobs retain deadline-first global price optimisation. Forecast PV is reserved for
the home battery to `MINIMUM_ESS_SOC`, and the applied job can also use live surplus between
blocks after that threshold. Unknown future energy sources stay visibly pending.

Do not consider production apply validated until these attended/multi-slot checks
have been completed:

- After a surplus-PV session has left the Tesla request at 5 A or below, press Vehicle
  **Start** once. Confirm one full-rate request bounded by the live Tesla ceiling, a fresh pushed
  `ChargeCurrentRequest` acknowledgement within 60 seconds, and normally ABB delivery above 5 A.
  If acknowledgement is absent, confirm exactly one retry and no third command. If Maxem holds
  actual delivery low after the request is confirmed, expect `delivery_limited` status/a warning
  and no repeated current increases.
- During a planned EV/grid block that overlaps an ESS BUY window, compare each quarter-hour's
  actual grid import, PV, ABB EV energy and ESS SoC delta with the dashboard plan. The displayed
  simultaneous ESS rise is valid only from the residual
  `grid + PV - EV - non-EV load`; a Maxem reduction should lower EV progress and be corrected by
  the next measured-SoC replan, never be hidden as full planned EV delivery.
- Run at least one full job in shadow mode (`EV_SMART_CHARGE_ENABLED=True`,
  `EV_SMART_CHARGE_APPLY=False`) and confirm the selected blocks, cost, target,
  tentative-price marking, ESS EV-load overlay, and latest-safe-start are credible.
- On a sunny multi-day shadow plan, confirm a day with cheap forecast surplus may exceed its
  even daily share and later grid days fall by the same kWh. Confirm a high export-value PV
  period is not preferred over genuinely cheaper grid energy, and days beyond the available
  PV forecast say **Source to be chosen**, not **Grid**.
- With an applied job waiting between scheduled blocks, attend one surplus event after the home
  battery reaches `MINIMUM_ESS_SOC`. Confirm charging starts/adjusts to exportable amps, Maxem
  may reduce actual delivery without command chasing, a 60-second cloud dip grace applies, and
  actual SoC progress reduces the next plan. Press Vehicle **Stop** once and confirm the same
  opportunistic session is not restarted. Repeat below `MINIMUM_ESS_SOC` and confirm the EV does
  not take the forecast/live PV reserved for the stationary battery.
- Attend one selected **Solar surplus** block during variable cloud. Confirm its requested amps
  never exceed live surplus, it does not silently become a full-rate grid charge when PV misses
  forecast, and the next replan moves any undelivered energy into later mixed/grid capacity while
  preserving the ready-by target.
- Run an attended applied job with Fleet Telemetry fresh. Confirm only block-edge
  commands occur, sub-5 A requests are sent twice, no duplicate Tesla schedule is
  created, and an ABB/Maxem throttle does not cause repeated current increases.
- After restarting onto the Fleet Auth fix, confirm the expired access token refreshes
  without an `auth.tesla.com` 401. Change the job target/current during an attended slot:
  the UI/controller state should move from pending to confirmed from pushed telemetry
  within 60 seconds. A missing or contradictory acknowledgement may cause at most three
  logical attempts; it must not recur every 15 minutes. Maxem-reduced ABB delivery is not
  a failed requested-current acknowledgement.
- During the applied charge, confirm all ABB `Ac/L{1,2,3}/Current` values are
  populated and already in amperes, and compare planned kWh/SoC with ABB settled
  `ev_charge_kwh` and actual SoC increase.
- Turn off/interrupt the service before the latest-safe start and confirm the
  onboard Tesla safety schedule uses local vehicle time and a continuous remaining-
  energy window ending at the deadline (it intentionally does not mirror the daily
  low-cost blocks, which use live control). Confirm it completes the job only inside
  that allowed fallback window. For a deadline over seven days away, confirm no Tesla
  fallback is installed yet; once the exact start is within seven days, confirm it appears
  on the intended local date with a non-zero interval. Then resume and confirm both known
  application-owned IDs are reconciled without touching user schedules. Repeat once with
  the car deeply asleep: one explicit unavailable response
  may cause one wake and one retry, while a generic schedule error must not cause wakes.
- While charge intent is already off, begin a charge outside a selected smart block using the
  temporary branch-created Tesla fallback, then press Vehicle **Stop** once. Confirm a stop
  command is attempted on the next controller tick (normally within 30 seconds), ABB current
  falls to zero, the Vehicle card changes to **Idle** at EV-meter standby draw (normally only a
  few watts), its ETA disappears, and the stale fallback no longer appears in the Tesla app.
  Repeat with stale home/plug telemetry if that condition can be reproduced safely.
- Stop one optimizer-started block manually in the Tesla app and confirm it is not
  restarted within that block; a later distinct block may resume.
- Leave the car unplugged through the reminder lead time and confirm exactly one
  normal-priority Pushover notification survives a service restart without spam.
- Collect several completed sessions before tuning `EV_BATTERY_USABLE_KWH`,
  `EV_CHARGE_EFFICIENCY`, `EV_EXPECTED_DELIVERY_KW`, startup delay, or high-SoC
  taper. Do not auto-learn/apply these from one session.

# Bugs / Testing

## MEDIUM — wrong behaviour / cost leak, not dangerous

### M1 — evcharger per-phase amps: verify on the bus
The `evcharger/42` device is a real **ABB B23/B24** 3-phase meter (confirmed on
the bus: genuine per-phase `L{1,2,3}/Current` + total `Ac/Power` /
`Ac/Energy/Forward`). The shared current metric remains per-phase for control and
the Vehicle tab. Only the powerflow EV card shows the requested sum across active
phases. Remaining task: sanity-check during an active charge that all three
`L{n}/Current` registers are populated (not just L1) so the per-phase control
value is right. Not a code change. See `docs/EV_LOAD_DECOMPOSITION.md`.

## LOW

- **L3 — Two `update_charging_amp_totals` implementations** (in
  `event_handler` and `ev_charge_controller`) both set
  `tesla_charging_amps_total`. Same value, but duplicated logic —
  consolidate to one source to avoid future drift.

## Verify operationally (not a bug)

- During an active charge, confirm all three corrected ABB topics
  (`Ac/L{1,2,3}/Current`) track the physical phases. Values are already amperes;
  the ABB dbus driver applies the Modbus register scale and no additional `/100`
  or `/1000` conversion belongs in consumers.
- Confirm `retained: true` on the fleet-telemetry dispatcher persists
  across receiver restarts (so the bridge always snapshots current state on
  connect).
