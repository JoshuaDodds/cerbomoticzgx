# PR: Adaptive ESS strategy and outage resilience

## Purpose

Make ESS dispatch resilient to cloudy, thin-spread and incomplete-horizon days
without regressing the existing Summer Trading path or the restart-isolated
Winter engine.

This iteration also repairs the always-on optimizer foundation identified by
the Claude Opus re-review of `639fa6f`. The adaptive layer no longer sits above
a calendar-split candidate selector with different economics.

## Behaviour

- With `ESS_ADAPTIVE_POLICY_ENABLED=False` (the default), Summer runs one
  continuous Trading solve over every known price slot. The former today-first
  split was removed: it charged the first-day sacrifice twice, compared raw
  cash instead of the DP objective, assigned no continuation value to same-day
  horizons, and allocated Victron windows independently.
- When enabled, the live Summer DP evaluates Trading, PV-first and protected
  hybrid constraint sets. Trading must clear a configured benefit plus a
  configured fraction of capped learned forecast risk; common historical error
  is not charged in full after per-candidate lifecycle/arbitrage risk. A durable
  dwell prevents quarter-hour policy flapping. The selected policy is solved on
  every normal cycle; all three are compared hourly or immediately after a
  material price, load/PV, SoC, EV-block or horizon change. This avoids up to
  nine solves without keeping a stale strategy or changing the 1% execution
  lattice.
- Protected hybrid reserves forecast household energy until the next cheap
  replenishment opportunity and a bounded continuation after the last known
  price slot. Neighbouring cheap slots form one procurement valley; the required
  energy must be reached by its final cheap slot, while grid charging remains
  restricted to genuinely cheap slots. A low live SoC can no longer remain in
  RETAIN indefinitely below the candidate's own household requirement.
- Winter Mode protects its 40% emergency backup floor while grid-connected,
  self-supplies expensive household demand above it, and applies the same
  unknown-horizon allowance at every final horizon boundary.
- Both optimizers now model unavoidable forecast PV spill explicitly. Export is
  clamped to the physical site limit, `pv_curtailed_kwh` is published, and the
  battery can never discharge merely to create more spill.
- The five-window Victron limit is an optimization constraint. Post-processing
  asserts the invariant instead of silently dropping a charge window assumed by
  the SoC/economic trajectory. Falling tariffs use distinct intermediate target
  stages so cheaper energy cannot be pulled into an earlier dear slot; saturated
  adjacent stages may safely share a target when no earlier charge headroom
  exists. Full-power reporting is confined to those exact stages, so an adjacent
  PV-only rise cannot become an uncovered BUY. Candidate economics are re-scored
  from that final executable trajectory.
- Historical stored-energy cost is treated as sunk when it cannot be recovered:
  the optimizer waits for the best visible sale, while the explicit
  `ESS_MIN_SELL_PRICE` remains absolute.
- An explicit Victron grid-offline signal suspends all optimizer control writes.
  Forecasting, planning, history and settlement continue, and the emergency
  reserve is available to the house. Missing startup state is unknown, not
  offline.
- Manual Override now suppresses writes only; the dashboard and historical
  evidence continue updating.

## Auditability and safety

The selected strategy, candidate scores, decision hurdle, controller authority
and suppression reason are included in plan JSON and cycle history. Each live
result also reports `optimizer_runtime_ms`; a cycle over 30 seconds emits a
warning while the previously applied control remains in force until planning
finishes. The feature does not modify `VICTRON_HARDWARE_MIN_SOC`. The separate
Advisor/CLI strategy evaluator remains observational and cannot select live
control.

The 40% winter reserve is deliberately unchanged. On the configured 42 kWh
bank it protects 16.8 kWh and leaves 25.2 kWh tradeable, versus 33.6 kWh at a
20% reserve. That 25% reduction in tradeable capacity is the accepted cost of
unexpected-outage heating resilience. Only an explicit grid-offline signal
releases it for emergency household use.

## Claude Opus re-review disposition

| Finding | Resolution |
| --- | --- |
| F1/F2/N2 | Removed the double-counting/raw-cash calendar selector; every candidate now enters selection directly from one continuous objective-aware solve. |
| F3/F4/F5/N4 | One shared bounded continuation model applies to every horizon and to adaptive scoring. It credits only trailing time-of-day household need above the candidate's protected floor, using the visible p75 avoided-import value; it cannot reward hoarding an entire battery. A factor of zero is a true master disable. |
| F6 | Charge-target count and stage saturation are carried in the DP state; exact Pareto pruning removes only dominated paths. Falling tariffs split targets unless a saturated stage can merge without shifting energy. Every final planned grid BUY is covered by one of at most five published windows after full-power reporting. |
| F7 | Current 2026 Tibber sale fee is configured as €0.0248/kWh. Annual saldering position and the post-2026 tariff remain explicitly open; settled Tibber reward is authoritative. |
| F8 | The asserted 85–88% site round-trip value was not evidenced. Code/config fallbacks are now consistently 0.96 per leg; measured-cycle calibration remains open before changing production. |
| F9 | Existing bounded historical risk remains partial. Exposure-specific load-under/PV-over adverse scenarios require shadow validation and were not guessed into live dispatch. |
| F10 | Added explicit PV curtailment slack and diagnostics to Summer and Winter. |
| F11 | Dynamic cost basis waits for the best forward opportunity but cannot strand sunk-cost energy; the static user floor is unchanged. |
| N3 | Kept 40% by explicit operator safety requirement; quantified its capacity cost above. |
| N5 | Removed the up-to-nine-solve calendar layer, bounded physically reachable SoC transitions, cached one config snapshot, added exact window-state Pareto pruning, and normally schedules full three-policy comparison hourly. Material input changes invalidate that cache immediately. No coarse SoC lattice is used. Production-host timing remains an attended gate. |

## Validation

Targeted tests cover policy constraints, material-benefit selection, horizon
continuity, winter self-supply, explicit outage pass-through, manual-override
observability, and absence of Victron writes while control is suppressed. See
the top-level TODO for the attended multi-day production checklist before
enabling adaptive Summer dispatch permanently.

Automated regressions additionally cover strict unified-horizon selection,
same-day household carry, explicit zero adaptive hurdles and terminal disable,
protected-floor/trailing-load continuation, executable BUY/window coverage after
charge-rate re-time, falling-price target staging and safe saturated-stage
merging, excess-PV feasibility/no discharge-into-spill,
unrecoverable cost-basis behavior, scheduled/material full comparisons, and
selected-policy infeasibility fallback.

### Attended QA checklist

1. Restart the service and request a replan. Confirm plan JSON contains
   `planning_policy.reason_code=UNIFIED_HORIZON_OBJECTIVE`, no more than five
   `victron_slots`, and every future `BUY` falls inside one of those slots.
2. Before next-day prices publish, confirm the final visible slot retains only
   the bounded household continuation—not an empty battery and not an arbitrary
   full battery. Inspect `continuation_value` in the plan.
3. On a sunny/full-battery case near the export cap, confirm planning remains
   available and `pv_curtailed_kwh` is non-negative; grid export must never
   exceed `ESS_MAX_GRID_EXPORT_KW`.
4. With Adaptive Summer enabled, confirm `adaptive_policy.full_evaluation=True`
   hourly, after the next-day horizon arrives, and after a material forecast/SoC
   change; intervening replans should say
   `SCHEDULED_POLICY_REEVALUATION_PENDING` and optimize only the selected policy.
5. Confirm forecast export reward is €0.0248/kWh below the corresponding buy
   price while within the 2026 saldering model, and compare settled reward to
   Tibber rather than assuming annual allowance eligibility.
6. In Winter Mode, confirm the grid-connected trajectory never crosses 40% for
   trading. Then simulate only through the existing tested offline-state harness
   (not by interrupting live mains): explicit offline must suppress all writes
   and release the logical reserve toward zero.
7. Observe optimizer duration on the production host for a full comparison and
   an intervening selected-policy replan using `optimizer_runtime_ms`. Record any
   full comparison approaching the scheduler budget before considering the
   adaptive gate unattended.
