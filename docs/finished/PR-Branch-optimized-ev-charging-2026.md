# PR: Optimized EV smart charging

## Branch

`optimized-ev-charging`

## Goal

Add an intuitive charge-by workflow for the Tesla Model S 100D: select a target
SoC and ready-by time, then minimize the incremental whole-home charging cost
while guaranteeing the requested energy whenever physically feasible.

## Non-negotiable operating constraints

- Tesla Fleet API is the vehicle control surface; Fleet Telemetry is required for
  applied smart control so the controller can verify commands without polling or waking
  the vehicle needlessly.
- The existing repeated sub-5 A `set_charging_amps` behavior is intentional: this
  vehicle accepts a request below Tesla's usual 5 A floor when it is sent twice.
- Maxem remains the independent and authoritative 25 A-per-phase overload
  protector. It may reduce actual EV power at any time. The application must not
  fight that throttling or repeatedly raise current in response to lower measured
  ABB power.
- The local ABB EV meter remains authoritative for actual power and energy.
- Smart planning and Fleet API application use separate gates. Both default off.
- With no active job, Summer and Winter optimizer behavior must remain unchanged.
- Manual Tesla/app/dashboard intervention wins; automation must pause instead of
  repeatedly reversing a user action.

## Delivered design

1. `lib/ev_smart_charge.py` owns the validated one-job model, atomic persistence,
   15-minute cost/PV planner, infeasibility handling, conservative latest-safe
   start, daily summaries, command blocks, and shadow plan snapshot. Horizons longer
   than 48 hours protect gentle minimum daily progress, then pull additional forecast
   solar forward only when its export opportunity cost is lower than the future planned
   energy it displaces; deadline feasibility still wins.
2. The ESS broker adds planned EV kWh to forecast load in both optimizer modes,
   publishes EV/non-EV slot decomposition, and records it in cycle/settlement
   history. With `EV_ALLOW_ESS_DISCHARGE=False`, both DPs forbid stationary-pack
   SoC reduction during EV slots.
3. The Vehicle tab accepts a Tesla-supported 50–100% target and offset-aware
   deadline, then shows a plain-language strategy and day rows containing the exact
   windows, daily kWh, SoC progression, cost/price status and source. It has no
   hover-only details or horizontal timeline. Pause/resume/cancel request a replan;
   stale job/plan pairs are never presented as current.
4. The controller reconciles only meaningful block edges. It converts requested
   power to per-phase current, clamps configured/live limits, retains Tesla's
   intentional sub-5 A double-send, and compares against the last desired command
   rather than ABB delivery so it cannot fight Maxem.
5. A deterministic application-owned Tesla schedule protects the latest safe
   start without billable schedule-list reads or touching foreign schedules.
   Charge-limit/schedule commands are deduplicated and budget gated, with bounded
   asleep retry and unsupported-firmware fallback.
6. Manual charge-now remains highest priority. A later external/manual start while
   the car is already plugged is never adjusted or stopped. Fleet Telemetry plug/start
   edges distinguish Tesla's automatic charge-on-plug behavior: outside a selected block
   it is stopped instead of bypassing the active job, while inside a selected block it
   is adopted for planned current control. If a controller-owned charge stops within a
   block, that job/block is durably suppressed (including across restart); a later block
   may resume. Disabling apply produces no stop/current/schedule side effect.
7. An unplugged active job sends one restart-safe, normal-priority Pushover reminder
   near its plug-by/first-block time using pushed state only and no Tesla read/wake.
8. A small virtual `EV_CHARGE_BLOCK_START_PENALTY_EUR` avoids fragmented plans for
   immaterial price differences. Reported electricity cost excludes the penalty.
9. Applied charge-limit, requested-current, and start commands remain pending until pushed
   Fleet state confirms them. Current requests wait the full 60-second acknowledgement window
   and receive exactly one retry; a newer lower request may be Maxem and does not trigger an
   immediate response. Actual ABB power/charge-state confirmation is used for starts, while
   requested-current acknowledgement—not Maxem-throttled delivery—is used for amp commands.
   The less frequent limit/start and owned fallback-schedule operations retain at most three
   attempts; their state cannot delay charge-limit acknowledgement.
10. Forecast PV is offered to the EV only after a chronological reservation can raise the
    stationary battery to `MINIMUM_ESS_SOC`. Unknown future PV/source periods are explicitly
    `pending`, not falsely labelled as grid. The Vehicle card reports solar, grid, and pending
    totals in plain language.
11. An actionable applied job may advance between scheduled blocks using live exportable PV
    after the same `MINIMUM_ESS_SOC` guard. It reuses the smart command acknowledgement and
    ownership path, preserves the 60-second cloud grace, never adjusts an external session,
    and a manual Stop durably suppresses that opportunistic session.
12. A forecast-solar-only block is capped to currently exportable amps and waits through a
    cloudy/missing-surplus period instead of silently drawing its full request from the grid.
    Mixed/grid blocks retain their planned backup; missed solar is recovered by the next
    measured-SoC replan and the deadline fallback.
13. A manual/grid start cannot inherit the low current left by a previous surplus-PV session.
    It restores the configured full-rate request, bounded by the configured kW and per-phase
    amp limits, then starts once the command is accepted. Pushed `ChargeCurrentRequest`
    must confirm the target within 60 seconds or exactly one retry is allowed. Persistently low
    ABB delivery is reported as Maxem/site limiting and never causes another current command.
14. Tesla's pushed `ChargeCurrentRequestMax` is treated as transient available current rather
    than a durable installation limit. Maxem may legitimately move it while protecting the site;
    those movements no longer shrink/expand the next plan or alter the controller's desired
    request. The configured kW/amp ceilings remain stable while Maxem controls actual delivery.
15. The Power Flow EV card uses the same per-phase ABB-current convention as the Vehicle tab
    and Tesla commands. It no longer presents the sum of all three phase readings.
16. A job becomes terminal when its target SoC is reached or its `ready_by` deadline elapses.
    Cleanup removes the exact current and legacy application-owned Tesla schedule IDs first,
    stops a controller-owned session when necessary, and only then deletes matching local
    job/plan artifacts. A failed Tesla removal retains the terminal marker for bounded retry.
    Cleanup-only reconciliation remains available after apply/telemetry is disabled without
    granting permission to execute an active shadow plan.
17. PV-surplus control stays dormant when pushed state already says the vehicle is away or
    unplugged. A live home-to-away location edge also clears an impossible retained home-cable
    state (plugged, charging and ETA); a later explicit public-charging event can set those
    values normally while away. Installations without Fleet Telemetry retain the legacy,
    rate-limited discovery wake, and authoritative ABB current always retains the safety path.
18. Mobile Power Flow cards reserve enough vertical space for every detailed telemetry row.
    Content-aware row budgets prevent short embedded viewports from crossing card borders, while
    the Battery card has a content-fit maximum height so tall phones do not add a large empty tail.

## Configuration

- `EV_SMART_CHARGE_ENABLED=False`: shadow planning and UI only.
- `EV_SMART_CHARGE_APPLY=False`: keep reviewed plans observational. Set it to `True` to permit
  control; live application also requires `TESLA_TELEMETRY_ENABLED=True`.
- `EV_BATTERY_USABLE_KWH=100`, `EV_CHARGE_EFFICIENCY=0.90`.
- `EV_CHARGER_MAX_KW=16`, `EV_EXPECTED_DELIVERY_KW=14`,
  `EV_CHARGER_MAX_AMPS=24` per phase.
- `EV_DEADLINE_BUFFER_MINUTES=30` and
  `EV_CHARGE_BLOCK_START_PENALTY_EUR=0.02`.
- `EV_ALLOW_ESS_DISCHARGE=False` (recommended).
- Plug-reminder gate/lead and durable job/plan/controller-state paths are documented
  in `.env.example`.

## Adversarial findings fixed

- Replaced the old unconditional 18 A adapter ceiling with an explicit smart-path
  installation ceiling while preserving the exact legacy 18 A default/no-job path.
- Preserved the intentional two-command sub-5 A Tesla workaround and return success
  if either accepted command succeeds.
- Prevented repeated measured-power chasing under Maxem throttling.
- Prevented a manual Tesla-app stop or a zero-power/Maxem event from being restarted
  repeatedly inside the same block.
- Kept shadow/apply toggles independent, with apply-off having zero smart side effects.
- Added an explicit regression proving apply-off preview mode still runs the established
  excess-PV Tesla path and issues its Fleet amp/start commands after `MINIMUM_ESS_SOC`.
- Replaced global front-loading for holiday-length jobs with feasibility-protected daily
  pacing, while preserving global cheapest-slot selection for urgent/short jobs.
- Added conservative sustained-delivery feasibility instead of claiming continuous
  16 kW availability through ordinary throttle/taper periods.
- Capped every EV slot to forecast site-import headroom after base load and PV;
  variable-capacity feasibility and latest-safe-start calculations no longer make
  the ESS DP infeasible by stacking full EV power on top of household demand.
- Added block-fragmentation economics, stale/future plan rejection, bounded retries,
  exact owned-schedule deletion, persistent notification dedupe, and target validation
  matching Tesla's 50% lower charge-limit bound.
- Isolated the one-shot vehicle-refresh regressions from live `.env` and durable-job
  state. A separate applied-job case proves that normal no-wake polling may continue
  without repeating the manual forced wake.
- Made GitHub Actions use the checked-in `.env.example` with no secrets file, and
  isolated the fresh-process optimizer-selector test from real MQTT/Tibber services.
- Migrated every Tesla OAuth exchange to the required Fleet Auth host. The runtime now
  reuses an unexpired saved JWT, refreshes within one minute of expiry, retries one
  unexpected Fleet 401 with the rotated token, atomically persists both new tokens,
  preserves the in-process settings cache, backs off authentication failures, and logs
  only sanitized OAuth error codes. The partner-domain helper uses the same current host.
- Kept sleeping-vehicle handling bounded: only an explicit unavailable/asleep command
  response can trigger one wake and one command retry; generic Fleet errors never wake the
  car. Replaced the decorative, float-rounded owned-schedule ID with an exact stable Unix-
  seconds ID matching Tesla's schedule representation, and translated the controller's
  weekday bitmap into Tesla's comma-separated weekday-name wire format.
- Corrected the onboard deadline fallback to convert every timestamp to vehicle-local
  Europe/Amsterdam wall time and to use one continuous remaining-energy window instead
  of treating the sparse multi-day daily plan as one Tesla interval. The Vehicle card
  now explains that optimized daily blocks run live while Tesla displays this fallback.
  Because Tesla one-time schedules carry only weekday/time, deadlines over seven days
  away do not install a fallback until the exact occurrence is within Tesla's representable
  week; a farther date is never approximated onto the wrong week. Cleanup removes both the
  current owned ID and the exact legacy ID briefly emitted by this branch, without listing or
  touching user schedules. Tesla's `schedule_not_found` response is preserved so only deletion
  of an ID which actually existed supplies migration ownership evidence for an automatic stop.
- Made dashboard Stop an imperative latched request in addition to clearing persistent charge
  intent. It now reaches the bounded, locally verified stop path even when intent was already
  off or pushed home/plug state is stale, retries an unconfirmed stop after 60 seconds, and
  clears only on confirmation or bounded escalation. An invalid app-owned fallback is removed
  before an associated outside-block charge can be mistaken for a manual session. A genuine
  later external start while already plugged remains protected from automatic stop/current
  changes; the immediate charge-on-plug edge is handled separately.
- Fixed stale Vehicle status after a confirmed stop in Fleet Telemetry mode. Confirmed stops now
  refresh the retained charging/ETA topics instead of waiting indefinitely for another
  `DetailedChargeState` edge. The live dashboard also reconciles a stale `Charging` value to
  `Idle` only when the dedicated ABB/Victron EV meter supplies clear standby evidence (at most
  100 W); missing meter data and meaningful draw continue to preserve Tesla's state.
- Restored the dashboard-wide 24-hour convention in the EV deadline editor, readable plan
  windows and urgent-plan departure summary. A separate native date field plus 15-minute
  24-hour selector avoids browser-locale AM/PM; desktop and mobile show `14:00–14:45`.
- Reworked the monthly Trends visual after production data showed that conventional
  statistical outliers were the late, accurate forecast revisions. It now uses one latest
  forecast per 15-minute period, a Q1–Q3 box and median, the full observed forecast range,
  and no false outlier classification. Settled historical spreads require 75% day coverage
  with near-midnight boundaries; partial days remain actual-only. The solid marker is the
  settled result, while today's hollow marker is the comparable latest full-day projection
  and settlement-so-far remains tooltip context. The broker now records remaining forecast
  import cost and export reward separately for subsequent bias attribution.
- Restored the dishwasher preferred-program invariant independently of appliance
  price optimization. A repeated user start now overrides only the wait time and
  still replaces Eco with programme 8203; intervention counts expire after 15
  minutes, scheduler-generated starts are not counted, and only fresh
  `ActiveProgram` telemetry can prove the preferred programme is already running.
- Made Tesla charging state authoritative for ETA lifecycle. Every pushed stopped,
  complete, disconnected, or no-power transition now stores and retains
  `Tesla/vehicle0/time_until_full=N/A`; telemetry cache, REST fallback, and frontend
  reconciliation also suppress any stale ETA whenever the car is explicitly idle.
- Reworked long-horizon solar handling after adversarial review. Equal pacing is now a protected
  minimum rather than a hard daily ceiling: known solar can replace the most expensive energy in
  the remaining gently paced plan, but cannot pull grid energy forward or consume PV whose export
  value is higher. Future periods without a PV forecast no longer claim a grid source.
- Kept stationary storage ahead of the car in both forecast and execution. Forecast surplus first
  fills the gap to `MINIMUM_ESS_SOC`; live between-block surplus uses the existing sun/ESS/current
  guard and normal bounded Fleet acknowledgement. External charging and manual stops remain
  authoritative.
- Prevented forecast error from turning a solar-labelled quarter into surprise grid demand. Pure
  solar slots follow live surplus with the same cloud grace; mixed or explicitly grid-backed
  slots remain deadline-capable, and a later replan accounts for any missed solar from actual SoC.
- Prevented manual/grid starts from silently remaining at a prior 5 A surplus request. A fresh,
  timestamped pushed requested-current acknowledgement is required—the adapter's in-process
  command shadow cannot falsely acknowledge itself—and one bounded retry covers a missed command.
  Low ABB delivery after acknowledgement is visible but cannot make the controller fight Maxem.
- Removed the remaining Maxem feedback loop: transient changes in Tesla's
  `ChargeCurrentRequestMax` no longer alter planning or command targets. A 16 kW configured
  ceiling is a power limit (about 23 A/phase at 3 x 230 V), not a hidden 16 A limit;
  `EV_CHARGER_MAX_AMPS` remains the explicit per-phase ceiling.
- Reverted the Power Flow EV current card from a three-phase sum to the project's canonical
  per-phase ABB average, with an average-of-available-phases startup fallback.
- Completed and expired EV jobs no longer persist as stale cards or files. Deadline expiry is
  now an explicit terminal state, live SoC can complete a still-planned snapshot immediately,
  orphaned exported-plan data is hidden once its durable job is gone, and deterministic Tesla
  schedule removal must succeed (or confirm not-found/unsupported) before local deletion.
- Fixed an attended production regression where inserting the cable caused Tesla's normal
  23 A charge-on-plug behavior while an applied smart job was waiting for a later block. The
  controller had classified every process-unowned charge as a manual override and therefore
  leaked full-price grid energy until the dashboard Stop was pressed. Non-retained Fleet
  plug/start edges now identify that narrow automatic case without stealing a later manual
  Tesla-app session.
- Made Vehicle telemetry health follow Tesla's change-driven model. Configured intervals are
  minimum send spacing, not heartbeats, so an unchanged connected stream remains healthy
  regardless of the last field timestamp. An explicit connectivity event determines whether
  the car is online; a recent field supplies only a short startup race fallback. The dashboard
  warns on disconnect only while Tesla state or the local ABB meter indicates apparent active
  charging, when live confirmation matters operationally. Normal parked/asleep silence no
  longer labels last-known values stale or triggers an unnecessary REST fallback.
- Raw Fleet values can legitimately become `null` as charge-only signals become unavailable
  during sleep. The normalization bridge now ignores null values for every configured field
  and ignores unknown detailed-charge enums, preserving the last valid retained
  `Tesla/vehicle0/*` state rather than inventing Unplugged/Idle/False/N/A values.
- Fixed the leave-home stale-plug failure without restoring or widening the legacy polling loop.
  Upstream `main` also engaged on PV surplus without first checking home/plug state, so reverting
  wholesale would retain the defect. With telemetry enabled, known away/unplugged state now makes
  a no-intent surplus tick fully dormant: it performs no Tesla status read, wake, start, stop or
  current command and emits only the normal one-time dormant transition log. A real ABB draw,
  manual intent/Stop, and active smart-job reconciliation remain actionable. The Vehicle-tab
  Refresh control is now visually presented as the same blue primary action as its peers.
- Corrected the mobile Battery card after the additional BMS rows outgrew its original fixed
  proportions. The phone layout now has a readable height floor and per-card row budgets; the
  Battery card remains top-anchored and capped at its content-fit height on taller/narrower
  screens. Grid, Loads, EV, Solar, and Battery final rows were measured inside their borders at
  390x844, and the excessive Battery tail was checked separately at 588x1280.
- Replaced the Advisor's unsafe prefix truncation after the expanded configuration schema made
  its tunable descriptions exceed the complete prompt budget. The v2 payload always retains
  valid JSON, live state, a compressed action-block plan, daily summaries, and every
  allow-listed setting value. Compact decision transitions and largest settlement errors are
  added by priority; recent conversation is tail-bounded, long history manifests use contiguous
  date ranges, and an impossible minimum payload fails before invoking the model. Descriptions
  for unfamiliar safe settings are available through bounded `NEED_CONFIG` retrieval rather
  than being repeated on every call.

## Fleet Telemetry certificate incident

The 2026-07-24 Fleet outage was caused by a renewable server leaf being included in
the vehicle's CA configuration. The receiver renewed from an `R13` leaf chain to
`YR2`, after which the vehicle rejected TLS with `bad certificate`. It was repaired
with a CA-only `YR2 -> Root YR -> ISRG Root X1` bundle and verified by a fresh
`CONNECTED` MQTT lifecycle event plus all 15 fields. The network investigation also
found and corrected an independent `192.168.1.0/24` hairpin-SNAT omission.

The complete evidence, recovery procedure, network distinction, and fast diagnostic
path are archived in
`docs/finished/TESLA_FLEET_TELEMETRY_CERTIFICATE_INCIDENT_2026-07-24.md`.

## Validation completed

- Pure planner, broker integration, Summer/Winter optimizer, Tesla adapter,
  controller, frontend/API, static/mobile, and legacy regression tests were added.
- Normal 12-hour planner benchmark is about 17 ms; an adversarial seven-day
  variable-headroom horizon remains Pi-suitable at about 0.95 s in development.
- Desktop and mobile Vehicle layouts were inspected in Chrome; the daily plan exposes
  all useful timing, price, energy and SoC details without delayed browser tooltips.
- The solar-first revision was re-rendered in headless Firefox at desktop and mobile widths;
  solar/grid/pending summaries and day-source labels remain readable with no horizontal overflow.
- `git diff --check` and Python compilation are clean.
- Final GitHub Actions-equivalent run on Python 3.11 after the attended
  charge-on-plug, Fleet Telemetry incident, freshness-warning, null-state retention,
  home-to-away disconnect, no-intent surplus dormancy, Power Flow current,
  Maxem-feedback, terminal job lifecycle fixes, and Advisor payload hardening:
  `694 passed` with no warnings.

## Operator validation before apply

The attended and multi-slot production checks are tracked at the top of `TODO.md`.
Start with planning on and apply off. Do not enable apply unattended until the shadow
plan, Maxem interaction, owned Tesla schedule, manual-stop behavior and reminders
have been observed successfully.

## Status

Implementation complete; operator validation pending. Keep
`EV_SMART_CHARGE_APPLY=False` until the attended checks above pass.
When disabling apply after an applied job, pause or cancel the job first so the
controller can remove its owned fallback; apply-off itself intentionally sends no
Fleet command.
