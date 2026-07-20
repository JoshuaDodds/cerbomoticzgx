# Ready for operator testing — Season-independent appliance scheduling and ESS coordination

Branch: `feat-rework-appliance-scheduler`

This is the living PR record for the current branch. The completed Winter Mode
optimizer work remains archived in `docs/finished/`; new appliance-scheduling
decisions, implementation notes, and validation results belong here.

## Purpose

Retain the household-friendly Home Connect behavior while making appliance price
deferral an explicit, tested policy that can be enabled independently of the
restart-isolated Summer or Winter ESS optimizer. The
system should continue forcing the preferred dishwasher program so an accidental
long program is not run, but should defer appliance starts for price optimization
only when the operator has enabled all applicable controls.

Known dishwasher and dryer demand should be represented in either ESS planning mode.
The goal is forecast and battery-charge accuracy: Victron already imposes the
physical grid-import limit, but coincident appliance demand reduces the energy that
can actually enter the house battery during a planned charge interval.

## Activation contract

Three settings have deliberately distinct responsibilities:

- `HOME_CONNECT_APPLIANCE_SCHEDULING` is the master switch for all automated Home
  Connect interventions.
- `WINTER_MODE` selects the restart-isolated Winter ESS optimizer.
- `APPLIANCE_OPTIMIZATION_ENABLED` permits price-based appliance deferral in
  either ESS season when the Home Connect master is enabled.

`APPLIANCE_OPTIMIZATION_ENABLED` defaults to `False`, appears directly below
`WINTER_MODE` in the top **AI ESS Optimizer** dashboard group, and requires a
supervised restart. This keeps appliance behavior coherent for the process lifetime
without coupling it to the selected ESS policy.

The supported matrix is explicit:

| Winter Mode | Appliance optimization | Runtime behavior |
| --- | --- | --- |
| Off | Off | Summer ESS; appliances run normally |
| Off | On | Summer ESS; appliances are price-optimized |
| On | Off | Winter ESS; appliances run normally |
| On | On | Winter ESS; appliances are price-optimized |

Preferred dishwasher-program enforcement remains active in Summer and Winter when
the Home Connect master is enabled. Price deferral is the gated behavior; program
enforcement is not.

## User-intent constraints to preserve

- Always use the configured preferred dishwasher program rather than allowing an
  unexpectedly long program.
- Before the evening boundary, keep any delay short enough that clean dishes or
  clothes remain available when expected.
- Later in the evening, permit a longer overnight delay because the household is
  sleeping, while still completing by the morning deadline.
- A deliberate second manual start is an immediate-run override.
- Do not abort an appliance unless a valid replacement start has first been
  planned and remote restart is available.
- If prices, planning, MQTT delivery, or appliance acknowledgement fail, prefer a
  bounded safe fallback over leaving an appliance stopped indefinitely.

These constraints encode the original reason for the legacy five-hour daytime and
longer evening windows. Odd-looking limits must not be removed solely to maximize
small theoretical savings.

## Baseline investigation

The merged legacy path still receives dishwasher and dryer state via Home Connect,
but its price deferral is calendar-gated rather than controlled by `WINTER_MODE`.
It also chooses from legacy hourly prices independently of the ESS optimizer. As a
result, enabling Winter Mode outside September–February does not enable appliance
deferral, and disabling Winter Mode inside those months does not disable it.

Reliability and planning issues identified for this branch:

- the abort/readiness flow can observe stale `Ready` state and send a replacement
  command before the appliance completes its abort transition;
- replacement commands are not acknowledged, so a successful abort followed by a
  failed start can leave an appliance stopped;
- readiness monitoring has no bounded timeout and can accumulate worker threads;
- dishwasher remote-start readiness is not fully checked before aborting;
- selection ignores negative prices, full-program runtime, contiguous prices, and
  the quarter-hour resolution used by the ESS optimizer;
- dryer quiet-program selection can reread stale program/runtime state;
- scheduled appliance demand is absent from the optimizer load trajectory, which
  can overstate expected battery charging when loads coincide.

## Planned implementation

### Phase 1 — characterization tests

- Capture current manual-start, second-start override, preferred dishwasher
  program, dryer program, daytime window, overnight window, and fallback behavior.
- Add explicit activation-matrix tests for the three feature settings.
- Reproduce the stale-state abort race and missing-acknowledgement behavior.

### Phase 2 — deterministic scheduling policy

- Replace implicit calendar gating with the restart-frozen, season-independent
  appliance activation contract.
- Evaluate valid quarter-hour start candidates using the same price horizon as the
  selected ESS optimizer.
- Score the complete appliance runtime while preserving the daytime convenience
  window and overnight completion deadline.
- Treat negative-price slots as valid and retain an immediate-run fallback when
  savings are immaterial or future data is unavailable.

### Phase 3 — acknowledged device control

- Build the replacement plan before aborting; persist it as optimizer demand only
  after Home Connect acknowledges the delayed start.
- Confirm remote-control readiness and required appliance metadata.
- Observe the appliance leave its old state, complete abort, and return to a newly
  observed `Ready` state with bounded timeouts.
- Require acknowledgement of the delayed start, limit retries, prevent duplicate
  workers, and expose failures without blocking the main event loop.

### Phase 4 — ESS optimizer coordination

- Represent accepted appliance work as bounded load reservations.
- Add reservation demand to the selected optimizer's load trajectory and trigger a
  replan when a reservation is created, changed, completed, cancelled, or fails.
- Coordinate simultaneous dishwasher and dryer reservations so forecast battery
  input reflects their combined load.
- Keep both optimizer implementations isolated while allowing the shared broker to
  overlay acknowledged appliance demand in either mode.

### Phase 5 — adversarial validation

- Exercise restarts, missing tomorrow prices, negative prices, delayed telemetry,
  duplicate events, MQTT failures, unavailable remote start, and both appliances
  starting together.
- Verify the manual override and preferred-program contracts.
- Compare Summer control decisions and schedule structure against the merged
  baseline; additive disabled-feature diagnostics are permitted.
- Run targeted suites followed by the repository-required full test command:
  `export DEV=1; python -m pytest -s -q`.

## Current implementation record

- Added the default-off `APPLIANCE_OPTIMIZATION_ENABLED` setting to `.env.example`
  and the local runtime `.env` without changing existing settings.
- Added the setting directly below Winter Mode in the dashboard's AI ESS group.
- Added supervised-restart handling for edits and for the first addition of the
  key to an older deployed `.env`.
- Documented the three-setting activation contract in the root and frontend
  READMEs.
- Added focused schema, restart-handler, and first-key watcher tests.
- Added a pure quarter-hour flexible-load planner which prices the whole contiguous
  runtime, accepts negative prices, requires a material saving, preserves the
  five-hour daytime comfort bound, and completes overnight work by 05:30. It uses
  elapsed-time arithmetic across DST boundaries and safely chooses immediate work
  when the current price horizon is incomplete.
- Added deterministic hourly-to-quarter expansion for Tibber's intentional hourly
  fallback, so full-runtime scoring remains available in degraded price mode.
- Reworked Home Connect coordination onto one bounded background worker per
  appliance. A valid plan and remote-start capability are required before abort;
  stale `Ready` telemetry is rejected until an abort transition has been observed;
  replacement commands require `DelayedStart`/`Run` acknowledgement; coordinator-
  generated transitions are distinguished from manual starts; and a failed
  replacement receives at most one immediate fallback attempt.
- Retained dishwasher programme `8203` enforcement independently of price
  deferral. Dryer price intervention is disabled unless appliance optimization and
  the Home Connect master are active, and delayed dryer runs completing after
  20:30 use SilentDry programme `32068` without relying on a stale programme reread.
- Added an atomic, one-reservation-per-device store under `HISTORY_DIR`. Only
  acknowledged delayed starts are recorded. Completion, cancellation, early stop,
  and expiry remove or prune reservations and request a non-blocking optimizer
  replan.
- Reservation overlay uses persisted per-quarter load profiles when available and
  elapsed-time epoch arithmetic across DST transitions; average power is only the
  fallback.
- Added accepted dishwasher and dryer demand to either optimizer's per-slot load
  forecast after weather correction. Coincident loads are additive, while a
  disabled feature toggle or disabled Home Connect master returns the original
  forecast unchanged. Diagnostics are exported in the dashboard plan, GlobalState,
  and ESS history.
- Retained the legacy hourly selection helpers only for compatibility; the runtime
  appliance coordinator now uses the native Tibber price horizon shared with ESS
  planning.

## Conservative modelling choices

- Until measured programme profiles are available, the coordinator uses bounded
  average estimates of 1.2 kW for the dishwasher and 0.9 kW for the dryer, with
  safe fallback runtimes of 60 and 150 minutes. Available remaining-runtime
  telemetry replaces the fallback runtime where it is trustworthy.
- The known reservation is added in full to the historical base forecast. This can
  conservatively overstate demand if a matching appliance happened to run in the
  same historical slot, but it avoids overstating battery charge input when known
  appliance work overlaps a grid-charge window.
- MQTT publish helpers are best-effort, so state acknowledgement is authoritative.
  The final immediate fallback is deliberately limited to one attempt and exposed
  via scheduler status rather than retried indefinitely.

## Validation record

- TDD red/green coverage: activation matrix, favorite programme, manual override,
  negative prices, contiguous runtime pricing, comfort deadlines, DST fallback,
  stale telemetry, timeouts, duplicate workers, pre-abort validation, command
  failure, cancellation/completion, atomic persistence, expiry, combined appliance
  load, the independent four-state season/feature matrix, and disabled-feature
  Summer baseline isolation.
- Focused scheduler, reservation, Summer baseline, Winter optimizer, broker, and
  selector regression: `155 passed`.
- Repository-required full suite: `445 passed in 16.28s` using
  `export DEV=1; python -m pytest -s -q` in the project Python 3.11 environment.
- Read-only live Summer dry run: 192 quarter-hour prices, coherent retain/sell
  trajectory from the live 96.3% SoC, and no appliance overlay with the default-off
  feature.
- Python compilation and `git diff --check`: clean.

## Independent adversarial review and refinements

The independent pass found issues not covered by the first green suite. Each was
converted into a regression test before its fix:

- Immediate fallback now requires bounded `Run` acknowledgement; a missing
  acknowledgement is reported as `FallbackFailed` with a critical log, never as
  success.
- The coordinator confirms the appliance was running before abort and carries that
  fact into the Ready waiter, closing the fast `Run -> Aborting -> Ready` race while
  still rejecting an unrelated stale Ready snapshot.
- Incoming Home Connect metadata is committed before a worker can read programme,
  runtime, door, or remote-control state.
- A `DelayedStart -> Run` materially before the coordinator-owned timestamp is a
  manual override: it is honored, the future reservation is removed, and the
  intervention counter is reset. Normal scheduled starts remain untouched.
- SilentDry is selected atomically in the dryer `activeProgram` command, followed
  by operation-state acknowledgement, and price-planned again with a conservative
  150-minute runtime before any abort when the original runtime could understate
  quiet-mode demand.
- A dishwasher already running programme `8203` is not interrupted when the
  economic decision is immediate.
- Appliance telemetry continues to reconcile completion/cancellation reservations
  when the Home Connect automation master is off; the master gates intervention,
  not state hygiene.
- The reservation overlay now consumes shaped quarter-hour energy and uses elapsed
  time across DST folds. Tibber hourly fallback data is expanded explicitly rather
  than silently disabling future-cost comparisons.

The review found no defect in toggle placement/default, restart isolation,
normal-time kW-to-kWh units, coincident-load summation, duplicate-worker guarding,
negative-price selection, or full-runtime scoring. A follow-up decoupling pass made
the appliance policy independently available to the Summer control path as well.

The decoupling pass received its own adversarial test/fix loop:

- Appliance activation is frozen in `lib.appliance_mode` without importing or
  consulting `lib.ess_mode`; changing either setting cannot silently change the
  other policy inside a running process.
- Both Summer and Winter broker paths apply the same acknowledged reservation
  overlay, while feature-off runs with no accepted work retain the original load
  forecast unchanged.
- Disabling appliance optimization or the Home Connect master prevents new
  interventions but does not erase delayed work already accepted by the appliance;
  that physical demand remains forecast until completion, cancellation, or expiry.
- The pre-release Winter-specific setting and reservation source names were removed
  rather than retained as ambiguous aliases.

### Live dishwasher acknowledgement refinement

Operator testing exposed a Home Connect behavior that the mocked acknowledgement
tests did not reproduce: after requesting preferred dishwasher programme `8203`,
the appliance reached `Run` while `SelectedProgram` continued to report the
programme (`8196`) originally selected in the appliance UI. The strict combined
operation/programme check therefore timed out and issued an unsafe duplicate start.
The generated `Ready -> Run` event was also incorrectly counted as a second manual
intervention.

The coordinator now marks command ownership before publishing, records the first
matching event transition as its acknowledgement, and preserves that acknowledgement
even if the user quickly cancels before the worker polls again. A later manual
`Ready -> Run` remains the deliberate second-start override. `OperationState` is
the authoritative delivery acknowledgement; `SelectedProgram` remains useful
diagnostic telemetry but cannot trigger a retry of an appliance already observed
running. Normal completion resets the intervention counter, and command markers are
removed on success, failure, and fallback completion.

## Operator test matrix

Use the dashboard toggles and allow each save to complete its supervised restart:

1. Winter off, appliance optimization off: confirm normal Summer ESS behavior and
   no price deferral.
2. Winter off, appliance optimization on: start an appliance and confirm Summer ESS
   remains selected while an acknowledged delayed start appears in
   `/dev/shm/cerbo_ai_plan.json` under `appliance_reservations`.
3. Winter on, appliance optimization off: confirm Winter ESS behavior with no new
   appliance price deferral.
4. Winter on, appliance optimization on: confirm the same delayed-start behavior
   and reservation overlay under Winter ESS.

In either enabled case, manually start a delayed appliance early and confirm it runs
immediately and its reservation disappears. Dishwasher programme `8203` enforcement
remains active in all four states while the Home Connect master is enabled.
