# EV smart-charge runtime reliability

## Scope

This follow-up addresses production evidence collected from the applied EV
smart-charge controller on 2026-07-25. It does not alter the Summer/Winter ESS
policy boundary or Maxem's independent site-overload authority.

## Root causes and corrections

- Replanning a few seconds after each quarter used a ceiling operation and
  discarded the quarter that had just begun. The planner now retains the
  remaining seconds/capacity of the current quarter and the broker commits an
  already-selected active block from the previous atomic plan snapshot.
- A partial final quarter previously appeared as a low-current tail while live
  control needed to request full current. Grid/mixed tails now use the full
  expected rate for a shorter interval; energy, expected SoC and controller stop
  time remain consistent.
- Smart/grid starts now send the configured `EV_CHARGER_MAX_AMPS` ceiling
  (1–25 A/phase). Planning still uses `EV_CHARGER_MAX_KW`,
  `EV_EXPECTED_DELIVERY_KW`, forecast site headroom and measured SoC so Maxem may
  lower real delivery without being modelled as additional site capacity.
- A successful Fleet command response is only delivery acceptance. Current
  requires a newer matching `ChargeCurrentRequest`; start requires a newer
  charging edge or local ABB draw. Missing current/start confirmation receives
  guarded retries during the active block and becomes visibly `at_risk`.
  Confirmed state is reset between distinct blocks so a later block always sends
  and verifies its own initial ceiling.
- Saving or editing an applied GUI job now creates its charge-limit obligation
  directly from the durable job file; it no longer waits for a matching optimizer
  snapshot or a calendar-representable Tesla fallback schedule. Home/plug state
  does not gate this vehicle setting. Commands repeat no faster than the
  60-second acknowledgement window until authoritative state confirms the
  requested target, with no fixed retry ceiling. A known matching
  `ChargeLimitSoc` is already satisfied regardless of when that unchanged state
  was first observed, and Tesla's idempotent `already_set` response confirms the
  same fact. Both terminate the command lifecycle immediately rather than
  causing repeated commands or wakes. The acknowledgement clock starts only
  after the centralized command/wake/settle delivery sequence returns, so wake
  latency cannot consume the window and cause an early paid retry.
- Tesla write operations now use a consistent `EvCharger [Tesla API]` audit
  prefix. Actual sends and accepted/rejected outcomes are visible, vehicle-wake
  and OAuth retries are explicit, and pushed confirmations identify the observed
  limit/current/start value and controller attempt. Retry context is appended
  idempotently, so nested wake-retry paths cannot duplicate the same explanation
  in one audit line.
- Fleet API accounting now reserves $0.25 of Tesla's $10 monthly credit instead
  of a full dollar: the non-critical hard ceiling is $9.75. Daily caps remain a
  separate runaway breaker and use the burst-safe 300 command / 150 data /
  20 wake defaults; audit lines no longer mislabel a daily-cap block as a
  monthly-ceiling block. Identical category/reason blocks are logged once and
  then no more than every 15 minutes; locally blocked attempts remain debug-only
  because no Tesla request was sent.
- Tesla commands no longer jump directly from an asleep-bus rejection to an
  explicit wake. Every stateful command uses one central delivery policy: try
  the command, allow 10 seconds for its implicit wake side effect, retry the
  command once, and spend one explicit wake only after a second asleep-bus
  rejection. After an explicit wake the controller waits at least 10 seconds
  and observes Fleet connectivity for up to Tesla's documented 60-second wake
  window before retrying the original command. Critical stops retain their
  budget bypass, while generic command errors never trigger a wake.
- Documented idempotent Tesla outcomes are normalized centrally:
  `charge_stop/not_charging` and `charge_start/is_charging|requested` satisfy
  the command instead of triggering needless retries.
- While the vehicle is home and plugged in, the EV controller is the exclusive
  charge authority. Charging is allowed only by an active applied smart block,
  protected PV surplus, or the deliberate combination of dashboard Vehicle
  **Start** and Grid assist. Tesla-app, onboard-schedule, cable-insertion and
  vehicle-limit starts cannot bypass those gates; unauthorized charging is
  stopped on the next control tick. External starts inside an authorized window
  are adopted and reconciled, and external stops inside an active block are
  restarted. Only the dashboard Vehicle **Stop** suppresses the current smart
  block, including after process ownership was lost or the service restarted.
- The deterministic Tesla deadline fallback is now persisted as application
  ownership. A fresh charge edge in its exact fallback window can therefore be
  identified and adopted after a service restart. Other starts are still adopted
  only inside an authorized window and stopped everywhere else.
- Retained `Tesla/vehicle0/plugged_status` now hydrates the controller-facing
  `tesla_is_plugged` state after the tmpfs GlobalState database is recreated.
  Raw Fleet receiver topics may legitimately publish null as signals become
  unavailable; the bridge ignores null and preserves the last valid normalized
  retained state.
- Startup telemetry hydration now checks whether a GlobalState row actually
  exists before reading it. The database client's historical missing-key value
  of integer zero no longer converts unknown home/plug state into a false
  “vehicle away” message before retained MQTT replay completes.
- The Fleet Telemetry subscriber now starts before EV control and publishes its
  startup transport state synchronously. The EV controller itself starts on a
  daemon worker, so Tesla command/wake latency can never delay the Victron MQTT
  client, scheduler, dashboard, pricing or other services. Until a retained
  source lifecycle or live vehicle signal completes hydration, ordinary Tesla
  commands and state inference are deferred in five-second ticks; locally
  measured charging, an explicit Stop and a manual Refresh retain their safety
  paths.
- Timeline quarter details now distinguish home-battery SoC from EV-battery SoC
  and show expected EV charge rate/source. Infeasible plans state the kWh
  shortfall and safety cutoff instead of only displaying a vague status.
- Tesla connectivity mirrors now retain the vehicle's own `CreatedAt` timestamp
  and reject delayed older lifecycle records. An unchanged source event no
  longer rewrites the mirror with a local arrival time. Subscriber transport
  state is separate (`AWAITING_SOURCE`, `SYNCHRONIZED`, `DISCONNECTED`), and
  command acknowledgement fails closed until a reconnected bridge sees a Tesla
  lifecycle event or live signal. Retained lifecycle replay hydrates last-known
  state without changing Tesla's source timestamp; an explicit wake still
  requires a newer post-wake `CONNECTED` event and cannot be confirmed by an old
  retained connection.
- The Tesla bridge and dashboard live feed now use host/process-specific MQTT
  client IDs, and temporary retained-value readers use per-read IDs. A deployed
  service and a development service can therefore overlap without Mosquitto
  evicting one subscriber and losing QoS-0 connectivity changes.
- Eligible applied jobs now expose **Run Now** only when their selected energy
  is one contiguous same-day window. Activation occurs inside the optimizer's
  single-writer lock, shifts the window to the button press, includes the
  remaining partial quarter without a false gap, and republishes Vehicle and
  Timeline costs before reporting success.
- Run Now replaces only application-owned Tesla schedule IDs, enables Grid
  assist, requests/verifies the configured full current and observes charge
  start through the existing guarded controller. At target/window end it stops
  charging, removes the owned schedule, returns the Victron setpoint to 0 W,
  confirms a 5 A Tesla idle request, and only then removes the visible job.
- The Tesla schedule now has the same source of truth as the Vehicle plan. A
  single contiguous same-local-day block is installed at its visible start and
  its end is rounded upward to Tesla's minute precision (for example
  15:15–15:54:32 becomes 15:15–15:55). Sparse/multi-day plans retain a
  conservative continuous deadline fallback, but the Vehicle card labels and
  displays that different window explicitly.
- Application-owned schedule signatures are restored from durable controller
  state before reconciliation. An unchanged schedule therefore survives a
  service restart without another billable upsert. A Run Now schedule which
  began at the next whole minute is frozen for that active window instead of
  sliding its start forward and being rewritten every controller tick.
- Run Now onboard schedules now keep the durable job deadline rather than the
  optimizer's rolling expected-completion block. Replanning may still update
  cost, SoC and Timeline estimates, but it cannot move the installed schedule
  or create another Tesla start edge. This prevents each quarter-hour replan
  from resetting Tesla/EVSE current negotiation to 6 A and forcing Maxem to
  ramp the session again.
- The obsolete application-owned schedule ID is now a one-time migration:
  successful removal (including Tesla reporting it already absent) is persisted
  in controller state, so later jobs and service restarts remove only the current
  owned ID. User-created Tesla schedules remain outside the cleanup allow-list.
- Run Now completion accepts Tesla's fresh `DetailedChargeStateComplete` edge
  when it occurred after the Run Now request and the observed car charge limit
  matches the job target. This handles Tesla displaying 79% at an 80% completed
  limit without weakening ordinary measured-SoC completion; retained/stale
  completion state and completion at a different charge limit are rejected.
- Full-rate current is a bootstrap, not ongoing site regulation. A newer exact
  `ChargeCurrentRequest` still confirms it, but an accepted command followed by
  measured delivery above 5 A is also sufficient proof that charging ramped up.
  The pending command lifecycle is then closed and Maxem remains authoritative.
- Desired state now outranks stale command intent. If a current request was
  locally blocked or rejected but a newer ABB observation proves delivery rose
  above 5 A, reconciliation closes without another Tesla command. Likewise, an
  ordinary authorized block already charging during startup does not install a
  redundant future-start fallback. Explicit Run Now still performs its requested
  one-time schedule replacement.
- A successful `charge_stop` no longer manufactures a zero in ABB-owned meter
  state. The controller records command acceptance, allows up to 60 seconds for
  measured current to fall below 1 A, and sends another bounded stop only when
  draw genuinely remains after that grace. This removes the observed duplicate
  stop 31 seconds after Tesla had already reported `Charging=False`.
- The Power Flow **AC Loads** card is now explicitly a non-EV household view.
  It subtracts the separately metered EV watts from the AC-out total and
  distributes that subtraction across L1/L2/L3 using the ABB EV phase-current
  proportions. The raw live load data, optimizer inputs and dedicated EV card
  remain unchanged.

## Durable EV actuals

- Closed Timeline rows are now outcome-only. Their visible action comes from
  `actual_control_action` or the nearest measured cycle `realized_action`;
  `predicted_control_action` remains comparison metadata and can no longer
  colour or label history as though the plan happened.
- The ABB lifetime meter was already diffed into settlement `ev_charge_kwh`;
  the missing presentation/analytics layer now consumes it instead of allowing
  completed EV activity to disappear when the forward plan is regenerated.
- Future cycle rows also retain the ABB daily counter as
  `ev_actual_today_kwh`, protecting the daily total across day rollover and
  incomplete settlement gaps. Future settlement rows add measured average kW
  and Tesla SoC endpoints for supporting context.
- Settlement rows store `ev_grid_import_kwh`, `ev_non_grid_kwh` and
  `ev_grid_cost_eur` with an explicit quality label. Grid import and its measured
  cost are attributed proportionally to the EV's share of simultaneous site
  load; non-grid energy may be direct PV or home-battery output and is never
  called free.
- Old history remains immutable. Readers derive the new cost/rate presentation
  from existing `ev_charge_kwh`, site-load and grid settlement fields, so recent
  completed charges appear immediately without a backfill.
- Advisor daily summaries now expose EV energy, attributed grid energy/cost,
  session count, slot coverage and completeness. The history manifest includes
  the new cycle and settlement fields, preventing the model from incorrectly
  claiming that EV actuals are absent.
- ABB `Ac/Power` events now produce only two additional history records per
  physical charging session: `ev_charge_transition` start and stop boundaries.
  A persisted hysteresis state ignores idle electronics and prevents duplicate
  events. New Timeline details therefore show meter-observed 24-hour start/stop
  times. Existing settlement-only history is truthfully labelled as energy
  measured within a 15-minute interval, without claiming the bucket boundary
  was the exact charging start.

## Validation

- Targeted EV planner/controller/broker/frontend suite passed 314 tests after
  the final duration/current alignment.
- Full repository suite passed 762 tests after the final refinement.
- Python compilation and `git diff --check` pass.
- Chrome desktop and mobile smoke rendering completed; the Timeline EV badge
  renders without overflow, and the new detail fields are covered by static and
  data-contract tests.
- After the durable-history follow-up, the full repository suite passes 768
  tests. Chrome desktop and 390 px mobile checks show completed EV badges
  without horizontal overflow; expanded settled rows show measured energy,
  average rate, source attribution and attributed grid cost.
- Outcome-only Timeline and measured EV-session timing add regression coverage
  for idle noise, duplicate active readings, first observations after state
  loss, exact ABB start/stop pairing, legacy interval labelling and planned
  action isolation.
- Charge-limit lifecycle coverage includes durable GUI jobs before optimizer
  publication, away/unplugged vehicles, target edits during an outstanding
  acknowledgement, deadlines beyond Tesla's one-week schedule representation,
  mismatching telemetry, 60-second retry pacing, matching retained state,
  idempotent `already_set` responses, and command cessation after confirmation.
  Tesla API tests cover accepted, rejected and wake-retried audit records.
- Authority-matrix coverage confirms that neither Grid assist nor Vehicle Start
  alone forces a charge, the pair does, removing Grid assist stops that session,
  external starts outside authorized conditions are stopped, external
  starts/stops inside a smart block are reconciled, and dashboard Stop
  suppression survives lost process ownership and restart.
- Final repository validation after the persistent charge-limit and audit-log
  refinement passes 783 tests; Python compilation and `git diff --check` also
  pass.
- Final authority/wake-escalation refinement passes the complete repository
  suite with 798 tests. Python compilation and `git diff --check` pass.
- Source-ordered connectivity, unique MQTT identities and single-window Run Now
  add regression coverage for delayed/duplicate lifecycle events, subscriber
  transport loss, retained replay, optimizer-lock overlap, mid-quarter
  activation, schedule replacement, full-current reconciliation and terminal
  Victron/Tesla cleanup.
- Final repository validation for this round passes 820 tests. Python
  compilation and `git diff --check` pass. Chrome checks at 1440 px desktop and
  390 px mobile confirm the Run Now action is visibly primary, uses 24-hour
  times and does not overflow or overlap its card.
- Startup isolation and idempotent charge-limit reconciliation add regression
  coverage for pre-hydration command suppression, non-blocking EV/telemetry
  workers, retained-replay settling, already-matching retained limits, Tesla `already_set` responses,
  retained connectivity hydration and rejection of stale pre-wake connection
  events. The complete repository suite now passes 828 tests; Python compilation
  and `git diff --check` pass.
- Run Now schedule reconciliation now persists its first representable minute,
  restores that ownership across restart and never advances the start on each
  controller tick. The planner, Vehicle UI and Tesla command use one schedule
  description: a single block is mirrored exactly (end rounded upward to one
  minute), while a multi-block deadline fallback is labelled separately.
- Accepted full-rate bootstrap commands stop retrying as soon as measured
  delivery rises above 5 A, handing current regulation back to Maxem. Accepted
  stops retain a 60-second idempotency/ABB-observation record so stale Tesla
  state cannot trigger a duplicate stop; a real return of local current breaks
  that guard immediately.
- Final repository validation for the schedule/current/stop reconciliation pass
  succeeds with 838 tests. The focused EV/Tesla/UI set succeeds with 329 tests;
  Python compilation and `git diff --check` pass. A Chrome desktop smoke check
  confirms the Vehicle controls and smart-charge card render without overflow.
- Fresh ABB power is now authoritative for physical EV draw: an idle sample
  prevents a stale Tesla `Charging` flag from reopening accepted `not_charging`
  stop commands, while stale/unavailable ABB data retains the conservative
  Tesla fallback. Run Now persists Grid-assist ownership, releases a
  module-owned toggle immediately on cancellation and preserves a toggle that
  was already enabled by the user. The final full suite passes 844 tests; the
  broader EV/controller/frontend set passes 463 tests.
- The terminal cleanup refinements add regressions for fresh-versus-stale Tesla
  completion, matching charge-limit authority, durable legacy schedule
  migration and non-duplicated wake-retry audit text. The complete repository
  suite passes 851 tests; the focused Tesla API, telemetry bridge and EV
  controller suite passes 293 tests.
- The budget refinement validates the exact $9.75 boundary, critical-command
  bypass and accurate daily-versus-monthly guard reporting. The complete
  repository suite passes 852 tests; the Tesla API/budget suite passes 96 tests.
- Desired-state reconciliation adds regressions for pre-existing versus fresh
  ABB delivery, startup adoption without a redundant schedule, preserved Run Now
  replacement and 15-minute budget-log suppression. The complete repository
  suite passes 854 tests; the focused EV/Tesla suite passes 318 tests.

## Attended production checks

- Create an applied near-term job while home and plugged. At the selected start,
  confirm a single configured-ceiling request and charge start, followed within
  60 seconds by a newer requested-current/charging signal or ABB draw.
- If the first command is intentionally made unobservable, confirm guarded
  retries remain inside the active block and the Vehicle state becomes
  `at_risk`. Restore telemetry and confirm retries stop immediately after fresh
  acknowledgement.
- Keep a job active across a quarter-hour optimizer cycle and confirm its
  current block does not move forward. A later distinct block must issue a new
  initial current command.
- Use a job with a partial final energy requirement. Confirm the Timeline shows
  a full expected rate ending part-way through the quarter and charging stops at
  target/end rather than running the full quarter.
- Restart the service while the application-owned Tesla deadline fallback is
  installed. If that fallback starts in its exact window, confirm the controller
  adopts it; a Tesla-app/onboard start outside an authorized block must be
  stopped.
- Put the vehicle into deep sleep, request a non-destructive setting change and
  inspect the Tesla audit lines. Confirm two command attempts are separated by
  roughly 10 seconds before one explicit wake appears. Confirm the post-wake
  command is not sent until the vehicle has had at least 10 seconds to settle
  and Fleet connectivity reports it available (or the bounded 60-second wake
  confirmation fails visibly).
- Exercise the full authority matrix while home and plugged. Grid assist by
  itself and Vehicle Start by itself must not force a grid charge; enabling both
  must do so. Turn Grid assist off and confirm the EV stops. During a smart
  block, stop once from the Tesla app and confirm reconciliation, then use the
  dashboard Stop and confirm the current block remains suppressed.
