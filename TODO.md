# TODO / roadmap

- **Daikin ONECTA HVAC shadow validation** — Four real Daikin units are now
  collected in one request every 20 minutes, published as retained `hvac/#`
  state, and persist correctly separated daily cooling/heating energy. The
  combined daily figures match the ONECTA app in attended checks, but the
  source remains a delayed, 0.1 kWh-resolution cumulative measure rather than
  instantaneous power. It remains observational: no ONECTA measurement changes
  the load forecast or ESS dispatch yet.

  - Accumulate at least 7 continuous complete cooling days and, separately, 7
    complete heating days. Account explicitly for cloud reporting delay,
    two-hour source buckets, shared-outdoor-unit behaviour and midnight rollover;
    never treat a cumulative delta as instantaneous HVAC power.
  - Compare weather-only versus weather-plus-ONECTA forecasts on a held-out
    period for both daily load and measured 15-minute base-load settlements.
    Require a material improvement before proposing any HVAC apply gate.
  - Keep the independently implemented manual control surface gated by
    `ONECTA_CONTROL_ENABLED=False` until an attended real-unit matrix confirms
    power, mode, setpoint, fan, airflow and Powerful commands converge in the
    cached UI within the API reserve. Streamer remains out of scope until Daikin
    advertises an official readable/settable capability for an installed unit.

- **Daily-net forecast calibration** — The Aug 2–3 low-SoC divergence was not a
  PV/load forecasting miss: the plan improperly proposed expensive
  `BUY`/`PRECHARGE_FOR_PEAK` blocks merely to cross a discretised reserve, while
  the realised Victron action correctly remained `RETAIN` ahead of known cheaper
  prices. The reserve-policy repair is live: it prevents further discharge below
  reserve but allows `RETAIN` only while a strictly cheaper known buy remains.
  Preserve historical misses in Trends; they are useful evidence rather than
  chart artefacts.

  - Validate at least 3 low-SoC mornings: before a genuinely cheap window the
    expected action is `RETAIN`/`RESERVE_POLICY`, not forced `BUY`; an immediate
    `BUY` remains valid only when its known economics justify it.
  - The PV nowcast now uses `pv-nowcast-confidence-v3`: a single daylight low/0 W
    source observation is `live_drop_pending` and preserves the baseline; two
    distinct fresh source updates spanning at least 45 seconds become the bounded
    `live_drop_confirmed` correction. A fresh near-sunset low/0 W reading is the
    immediate `live_drop_sunset` case. Both uplift and drop overlays are limited to
    the current hour: a point live reading must not revise the remaining day’s
    export forecast. Collect 7–14 days with the explicit fair baseline/weather
    branches before drawing conclusions about PV forecast error.
  - **Active-slot daily-net accounting repaired (2026-08-05).** The optimiser
    deliberately keeps the current quarter-hour in its schedule; previously the
    dashboard and history counted that entire slot as future while Tibber's daily
    counters already contained its elapsed portion. Both now book only the
    unelapsed fraction, so during a planned BUY the rising realised import should
    be offset by falling remaining-import cost; during SELL, rising realised export
    should be offset by falling remaining-export reward. Historical snapshots remain
    unchanged. Validate this through BUY, waiting and SELL periods, including
    EV/appliance days. Any remaining movement must be attributable to a real replan
    or new PV/load/EV information, not the passing time inside the active slot.

- **Weather forecast validation / apply tuning** — Both gates stay off:
  `HVAC_LOAD_APPLY=False`, `PV_WEATHER_APPLY=False`. The validation command is
  deliberately read-only and now fails closed if any compacted history cannot
  be enumerated/read:

  `python scripts/validate_forecasts.py --dir data/history`

  Current valid load evidence is **13** complete days / **1,223** unique
  quarter-hour slots: baseline MAE **0.106831** kWh versus weather shadow
  **0.103834** kWh (**2.8%** improvement; 7 improved, 3 tied, 3 worse). Its
  day-block confidence interval still includes harm, so the correct result is
  `KEEP_APPLY_OFF`. The old raw PV comparison is diagnostic only (3.9%
  improvement on 13 days) because it predates matched post-nowcast branches;
  new `final_baseline_pv_forecast_kwh` and
  `final_weather_pv_shadow_kwh` records are required before PV can be judged.

  - Re-run after at least 14 complete local calendar days. A branch only earns
    human review if it has at least 80 distinct quarter-hours/day, at least 80%
    of the expected local-day slots, evidence close to both local-day boundaries,
    improves MAE by at least 5% on a majority of days, does not materially worsen
    bias, has a day-block confidence interval excluding harm, and stays within
    the 0.5 kWh/slot adjustment bound. The evaluator accounts for 92/100-slot
    daylight-saving days and uses unrounded values for every threshold decision.
  - Evaluate HVAC/load and PV independently. Enable at most one gate at a time,
    then collect another multi-day holdout period; nothing changes automatically.
  - Do not tune `HVAC_ALPHA_HEAT` or claim heating validation from summer cooling
    data. Winter needs its own meaningful heating sample.

- **ESS dispatch-efficiency counterfactuals** — The exported AI plan contains
  an explicit, timestamped physical/economic snapshot for an offline comparison
  of three simplified candidates: market arbitrage, PV-first self-sufficiency,
  and a protected hybrid. Run it explicitly, never from the live service:

  `python scripts/evaluate_ess_strategies.py --plan /dev/shm/cerbo_ai_plan.json --json`

  The evaluator is read-only: it does not import the broker/settings/MQTT/Victron,
  select a winner, persist a background job, or change dispatch. It is a research
  baseline, not a second production optimizer; it intentionally does not model
  every live guardrail/device response. After deployment, issue one normal replan
  before using the command so the exported plan has its explicit assumptions; a
  pre-upgrade plan requires the visibly labelled `--use-research-defaults` mode.

  **Comparison basis corrected (2026-08-06); earlier candidate numbers are void.**
  The report previously totalled each candidate over the *whole* plan horizon —
  roughly 32 hours once tomorrow's Tibber prices publish around 13:00 — while the
  Advisor panel scored it against the dashboard's today-only tile. Tomorrow's
  revenue was therefore read as today's, and the alternatives looked
  overwhelmingly better than the active plan (a representative afternoon showed
  market arbitrage at `+€19.92` against a live `+€6.74`). Every row is now one
  calendar day, midnight to midnight: the already-settled part of today
  (`plan.today_actuals`, an identical constant in every row) plus that policy's
  planned remainder. Candidates still optimize over the full known horizon;
  only the reported window is today. `plan_baseline` re-totals the active plan's
  own per-slot flows through the same function as the candidates, so the rows
  differ only by policy, and it ties to the dashboard Today tile in attended
  checks. `schema_version` is 2.

  On the same afternoon's plan the corrected ranking inverted: live plan `+€7.09`
  (after wear `+€5.90`), market arbitrage `+€5.84` (`+€4.76`), protected hybrid
  `+€0.03` (`−€0.52`), PV-first `−€5.74` (`−€5.78`). `market_arbitrage`'s
  constraint set is in fact already the live engine's own (same reserve floor,
  same grid-charge cap, export permitted), which is consistent with a small
  spread rather than a large one.

  - **Open validation (started 2026-08-06):** watch the Advisor strategy panel
    over several days and confirm the ranking is stable when the battery does
    *not* start the window at 100% SoC, and across BUY/RETAIN/SELL afternoons.
    The single validated sample so far began at 100%.
  - **PV-surplus export corrected (2026-08-07); pre-correction candidate
    numbers are void too.** The evaluator credited PV surplus as exported while
    the battery still had room. On a 07:00 plan at 4% SoC this put the live row
    €1.52 above the Today tile across 16 morning `IDLE` slots, but the defect
    was not confined to the baseline: it inflated every candidate, and for
    `pv_first_self_sufficiency` and `winter_self_sufficiency` — which forbid
    active export — it was 100% of their reported revenue (€4.88 and €4.37 that
    day). The installation never commands an export setpoint for surplus, so
    none of it was real. Both the DP and the settlement step now absorb surplus
    first, bounded by headroom and charge rate; commanded discharges are
    unaffected. `tests/test_101_ess_strategy_cli.py` now asserts the live row
    against `frontend.data.day_summary` directly, which is the only check that
    catches the two surfaces drifting apart.
  - **The Winter-style row is a stand-in, not a Winter Mode preview.** It shares
    only one property with `lib/ai_powered_ess_winter.py`: no routine
    battery-to-grid export. It holds a static `MIN_SOC_RESERVE_WINTER` floor
    rather than one sized from forecast household demand to the next
    replenishment window plus a learned uncertainty margin, it never takes the
    exceptional-spread export the real engine permits, and it has no
    replenishment-window charge scheduling. Do not decide whether to flip
    `WINTER_MODE` from this row. Note it is *not* `pv_first_self_sufficiency`
    either: PV-first forbids grid charging, which is precisely the cheap-window
    replenishment Winter Mode is built around. On a high-PV summer day starting
    near 100% SoC the two converge, because replenishment is never needed —
    expect them to separate in winter.
  - Read `carried_energy_kwh` / `carried_energy_value_eur` alongside every
    whole-day figure. A today-only total credits a policy for selling stored
    energy but never debits the emptier battery it hands to tomorrow, so a
    policy that ends the day flat can outscore one that ends it full purely by
    borrowing from tomorrow. On the sample above the four policies sat within
    about €0.90 of each other once carried value was added back.
  - Do not reintroduce a comparison against `day_summary`: that tile applies
    different per-slot rules (it suppresses IDLE PV-surplus export revenue and
    fraction-weights the active slot). `tests/test_90_mobile_ux_static.py`
    pins this out of the Advisor panel.

  Before proposing a seasonal-policy change,
  collect comparable snapshots over at least 14 complete days and compare net grid result
  (export reward minus import cost),
  import/export, battery DC throughput/full-equivalent cycles, minimum/protected SoC,
  terminal SoC and realised settlement. Do not use future actual PV/load to choose a
  historical "winner", and do not change Summer/Winter behaviour from one scenario.

- **Per-day strategy override (proposed, not implemented)** — Requested: select
  an alternative strategy in the Advisor and have it drive dispatch for the rest
  of the day, reverting to the AI optimizer at midnight. Deliberately deferred
  until the corrected counterfactual has collected enough days to show a
  candidate genuinely and repeatably beating the live plan — the original
  motivation for the feature rested on the void horizon-vs-today comparison
  above. Design notes for when it is revisited: the three candidates are
  *constraint sets*, not planners, so this must be a policy override applied to
  the existing engine at the `optimize_schedule()` choke point, never a second
  optimizer. It needs one new DP constraint (`allow_active_battery_export`,
  defaulting to today's behaviour), read-time expiry from a durable JSON store
  so a missed tick or restart cannot strand it, refusal in Winter Mode
  (separate engine, separate reserve policy), a hard invariant that it can never
  lower the planning floor below `current_min_soc_reserve()` or touch
  `VICTRON_HARDWARE_MIN_SOC`, and `strategy_override` recorded into the plan
  JSON and history so every slot stays attributable.

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

# Bugs / Testing
- None known at this time
