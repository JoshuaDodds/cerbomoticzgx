# TODO / roadmap

- **Daikin ONECTA HVAC shadow validation** — Phase 1 now reads all four units in
  one request every 20 minutes, publishes retained `hvac/#` state, and stores
  correctly separated today/yesterday cumulative heating and cooling energy.
  It is observational and off by default; do not use the data to alter the load
  forecast until these future-data checks pass:

  - Compare combined `today_total_kwh` with the ONECTA mobile app several times
    over at least 7 complete cooling days and later 7 complete heating days.
  - Verify midnight rollover moves the prior day's final total into
    `yesterday_total_kwh` without combining the two 12-bucket halves.
  - Confirm OAuth refresh remains unattended, the collector stays below the
    200-call daily limit, and restarts do not duplicate fresh API reads.
  - Correlate cumulative HVAC increments, powered modes, outdoor temperature and
    AC base-load settlements; quantify reporting delay and shared-outdoor-unit
    effects before fitting forecast features.
  - Measure whether ONECTA's 0.1 kWh reporting resolution, two-hour source
    buckets, and cloud delay are reliable enough for intraday correction or
    only for next-day/model calibration. Never reinterpret cumulative energy
    deltas as instantaneous HVAC power.
  - Compare weather-only versus weather-plus-ONECTA holdout error. Apply no HVAC
    forecast correction until multiple complete days show a material improvement
    in both aggregate daily load and 15-minute settlement forecasts.

- **Daily-net forecast calibration** — The Trends chart correctly preserves one latest
  forecast revision per 15-minute period, so it must not hide the Aug 2–3 divergence as a
  chart-only artefact. Investigation found 38 (Aug 2) and 36 (Aug 3)
  `BUY`/`PRECHARGE_FOR_PEAK` cycles whose measured action was `RETAIN`: the optimizer
  hardware's RETAIN was the correct response to an uneconomic plan. When live SoC was slightly
  below the seasonal reserve, the DP prohibited the neutral below-reserve state and so
  forced an immediate BUY merely to cross its discretized reserve boundary—even at
  €0.30–€0.36/kWh while a €0.13/kWh daytime window was known. The reserve now prevents
  further discharge below the floor but permits RETAIN there only while a strictly cheaper
  known buy remains; otherwise it still recovers immediately. This is an optimizer control
  fix, not PV/load tuning. Historical box plots should retain the pre-fix misses as useful
  evidence. The closing forecast was already close in earlier validation (about €0.23 MAE),
  but earlier forecasts require fresh validation after this repair.

  - Observe at least 3 low-SoC mornings after deployment. Before the genuinely cheap
    window, expected control is `RETAIN`/`RESERVE_POLICY` (grid covers house load but no
    forced battery charge); only an economically justified scheduled window may become
    `BUY`.
  - Compare forecast error by time-to-settlement before and after the repair. Treat a
    planned BUY that actually executes as RETAIN as an execution incident only after
    verifying the planned BUY itself is economical; inspect the reserve state and planned
    buy price before changing PV/load assumptions.

  - Collect at least 7 complete days, preferably 14, with
    `forecast_remaining_import_cost_eur` and
    `forecast_remaining_export_reward_eur`.
  - Recalculate error by time-to-settlement and attribute the positive bias to predicted
    import cost, export reward, or both. Check EV/appliance days separately.
  - Investigate the repeatable intraday shape observed by the operator: the projected
    final net starts highly profitable, falls roughly in step with realized grid cost
    during scheduled BUY/charging, then rises again after buying finishes while the
    system waits to SELL. The BUY-period reduction has also been observed to jump back
    up when the optimizer runs, suggesting a sawtooth where live settled cost is
    subtracted from a stale remaining-cost forecast and only reconciled on the next
    optimizer cycle. For every forecast snapshot and intervening live UI update, verify
    whether the displayed value comes from the persisted optimizer projection or is
    recomputed by mixing live settlement with stale forecast components. Verify the
    accounting identity
    `projected final net = settled net so far + remaining export reward -
    remaining import cost` and determine whether realized import/export is replacing
    its corresponding forecast exactly once or being omitted/double-counted.
  - Replay BUY, waiting, and SELL periods with fixed day-ahead prices and record
    settled import cost/reward, remaining import cost/reward, SoC, PV/load revisions,
    and optimizer plan changes separately. Distinguish legitimate forecast changes
    caused by new load/PV/SoC evidence from a ledger/display bug that merely follows
    cumulative spend or reward.
  - Tune the underlying forecast only after the component history identifies the source;
    require lower morning/midday MAE, smoother convergence through BUY/SELL settlement,
    and no degradation of the approximately €0.23 closing MAE.

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

- With an applied job waiting outside a selected block and no usable PV surplus, insert
  the cable once. Tesla may begin its normal immediate charge, but the controller must
  stop it on the next control tick and ABB draw must fall to standby. Repeat with a
  Tesla-app/onboard start while still home and plugged: it must also be stopped because
  external Tesla behavior is observation, not controller authority. Repeat inside a
  selected block and confirm the already-running session is adopted at the planned current.
- Verify the manual authority matrix explicitly: Grid assist alone never starts the EV;
  Vehicle **Start** alone cannot force charging outside a smart/PV window; Vehicle
  **Start** plus Grid assist starts and maintains a full-rate grid-backed charge; disabling
  Grid assist then stops that manual session. Vehicle **Stop** must stop immediately and
  suppress the current smart block even after a service restart or lost process ownership.
  A Tesla-app/onboard stop inside an active smart block must instead be reconciled.
- After a surplus-PV session has left the Tesla request at 5 A or below, press Vehicle
  **Start** once. Confirm one full-rate request at the configured 1–25 A/phase installation
  ceiling, a fresh pushed `ChargeCurrentRequest` acknowledgement within 60 seconds, and normally
  ABB delivery above 5 A. If acknowledgement is absent, confirm one guarded retry per
  acknowledgement interval while the request remains active and an `at_risk` state after the
  initial attempts. Once a newer pushed request confirms the ceiling, Maxem may hold or later
  reduce actual delivery without causing command chasing.
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
  commands occur after fresh acknowledgement, a distinct later block sends its own initial
  ceiling command, sub-5 A solar requests are sent twice, no duplicate Tesla schedule is
  created, and an ABB/Maxem throttle after confirmation does not cause repeated current
  increases. If a current or start command is accepted but not observed, confirm guarded
  60-second retries continue only through that active block.
- After restarting onto the Fleet Auth fix, confirm the expired access token refreshes
  without an `auth.tesla.com` 401. Change the job target/current during an attended slot:
  the UI/controller state should move from pending to confirmed from pushed telemetry
  within 60 seconds. A missing or contradictory current/start acknowledgement becomes
  visibly `at_risk` after the initial attempts and continues guarded retries while the
  selected block remains active; it must reset at the block boundary rather than leaking
  acknowledgement state into the next block. Maxem-reduced ABB delivery after a fresh
  requested-current acknowledgement is not a failed command.
- During the applied charge, confirm all ABB `Ac/L{1,2,3}/Current` values are
  populated and already in amperes, and compare planned kWh/SoC with ABB settled
  `ev_charge_kwh` and actual SoC increase.
- After a completed charge crosses at least two quarter-hour boundaries, confirm
  its settled Timeline rows continue to show actual EV kWh/average kW after the
  forward plan is regenerated. After midnight, verify the Advisor's completed-day
  `ev_charge_kwh` matches the ABB/Domoticz daily total and that attributed grid
  cost is marked partial whenever a service gap left settlement coverage incomplete.
- Turn off/interrupt the service before the latest-safe start and confirm the
  onboard Tesla safety schedule uses local vehicle time and a continuous remaining-
  energy window ending at the deadline (it intentionally does not mirror the daily
  low-cost blocks, which use live control). Confirm it completes the job only inside
  that allowed fallback window. For a deadline over seven days away, confirm no Tesla
  fallback is installed yet; once the exact start is within seven days, confirm it appears
  on the intended local date with a non-zero interval. Then resume and confirm both known
  application-owned IDs are reconciled without touching user schedules. Repeat once with
  the car deeply asleep: the first asleep-bus rejection must wait 10 seconds and retry the
  command without an explicit wake. Only a second asleep-bus rejection may send one wake;
  the post-wake command must wait until at least the 10-second settle period and a connected
  vehicle signal (bounded by Tesla's 60-second wake window). Generic schedule errors must
  not cause wakes.
- While charge intent is already off, begin a charge outside a selected smart block using the
  temporary branch-created Tesla fallback, then press Vehicle **Stop** once. Confirm a stop
  command is attempted on the next controller tick (normally within 30 seconds), ABB current
  falls to zero, the Vehicle card changes to **Idle** at EV-meter standby draw (normally only a
  few watts), its ETA disappears, and the stale fallback no longer appears in the Tesla app.
  Repeat with stale home/plug telemetry if that condition can be reproduced safely.
- Stop one optimizer-started block in the Tesla app and confirm the controller reconciles
  it while the block remains active. Then stop it with the dashboard Vehicle **Stop** and
  confirm it remains suppressed for the rest of that block; a later distinct block may resume.
- Leave the car unplugged through the reminder lead time and confirm exactly one
  normal-priority Pushover notification survives a service restart without spam.
- Collect several completed sessions before tuning `EV_BATTERY_USABLE_KWH`,
  `EV_CHARGE_EFFICIENCY`, `EV_EXPECTED_DELIVERY_KW`, startup delay, or high-SoC
  taper. Do not auto-learn/apply these from one session.

## Onecta module for data and control of Daikin Airco units
- Phase 0 discovery and Phase 1 monitoring are implemented. The four real units,
  cumulative cooling/heating energy and retained `hvac/#` state are available.
- A top-level HVAC page and separately gated, capability-driven manual controls
  are implemented. Keep `ONECTA_CONTROL_ENABLED=False` until the disabled/read-only
  presentation and each desired real-unit command have been reviewed manually.
- Complete one real-unit command matrix for power, mode, setpoint, fan,
  horizontal/vertical airflow, and Powerful; verify accepted commands converge
  in the cached UI without manual refresh and remain within the daily API reserve.
- Streamer is not advertised by any installed unit's official API capabilities;
  revisit only if Daikin adds a readable/settable purification characteristic.
- Continue multi-day shadow validation before allowing ONECTA data to alter load
  forecasts. Scheduling and comfort-aware optimizer orchestration remain future work.

# Bugs / Testing
- None known at this time
