please note, we have finished work on the previous branch and switched to a new one now and will need to keep and maintain a new PR doc as before.  this branch will be to re0implement and investigate the winter month awareness of the app since we have switch to the new AI optimizer for ESS management and are not using the legacy methods anymore.  The legacy system behaved differently during winter months... basically trying to buy only enough energy to remain fully sulf sufficient during peak price points to achieve lowest cost energy usage from the grid during these winter months. The purpose of this was that in this location the PV panels are practically useless during winter and produce very little.  This could be verified from previous years by looking at historical values from the VRM API and then you would understand how vastly different the panels produce in winter months.  Since we cannot make use of buy sell trading opportunies very much at all in the winter on most days we dont try and instead for just making sure we charge enough into the battery to make sure we can run off that energyu during the peak hours until the next low point where we will charge up again to keep the cost of buying from the grid as low as we possibly can.  If we see a huge SPREAD which is obvious win we might charge to 100% on that day to sell some excess later but would always leave enough for the house to run off of until we hit the next dip in pricing.  

You can find some remnants of the functionality in the legacy functions and perhaps on the v1.0 branch in this repo.  The goal here is to re-implemtnt this functionality and if possible, improve on it, and let the new AI optimizer orchestrate this behaviour while gating it behind a toggle in the config / .env that we would rename to "Winter Mode"

Let's formulate a clear plan for this and make sure we segregate any new decisions and behaviour perhaps into a completely separate version of the optimizer so that we can be certain that we would never have a regression in the current summer mode module as we work on and develop the winter mode module.  Separeate files i think would almost ensure this and .env would gate that only one or the other would ever get even imported ensuring desired activation of the right runtime behaviour in a transparent way.  For this, some code duplication is okay with me because i believe the payoff in safety is worth it.  

Let me know your thoughts.



# Ready for review — isolated Winter Mode ESS optimizer

Status: implementation and Phase 4 adversarial validation complete on
`feat-reimplement-winter-mode`; activation remains off by default pending operator
review.

This document is the living PR record for this branch. The completed EV/load and
dashboard iteration remains archived under `docs/finished/`; changes and decisions
for Winter Mode belong here.

## Purpose

Reintroduce winter-aware ESS behavior for the AI optimizer without changing the
current summer optimizer. In Winter Mode, expected PV is usually too small to make
routine charge/export trading the primary strategy. The system should instead buy
the minimum practical energy in low-price windows, use that stored energy to avoid
grid imports through the following expensive period, and arrive at the next low
window with the configured resilience reserve intact.

An exceptional, clearly profitable spread may justify charging to the user's
configured maximum grid-charge SoC (`ESS_MAX_GRID_CHARGE_SOC`) and selling surplus
later, but only after reserving enough forecast energy for the house until the next
low-price replenishment opportunity. Winter Mode must never replace that setting
with a hardcoded 100% target.

## Historical evidence

A read-only query of the configured VRM installation's daily `solar_yield` stats
was performed during planning. Daily production was reconstructed from the VRM
`Pc + Pb + Pg` components. No VRM or local history was modified.

Across available 2023–2026 data:

| Period | Days | Mean PV/day | Median PV/day | Days below 5 kWh | Days below 10 kWh |
|---|---:|---:|---:|---:|---:|
| Deep winter (Nov–Feb) | 419 | 4.3 kWh | 3.1 kWh | 76.1% | 93.1% |
| Shoulder (Mar, Sep, Oct) | 307 | 19.6 kWh | 18.7 kWh | 6.5% | 27.0% |
| Higher-PV months (Apr–Aug) | 569 | 34.3 kWh | 34.8 kWh | 0.2% | 2.8% |

Representative monthly daily averages were 2.2/2.6/2.1 kWh in December
2023/2024/2025 and 2.8/2.6/2.5/2.3 kWh in January 2023/2024/2025/2026.

This validates the operating premise, but also shows that September and October
are not equivalent to December and January. `WINTER_MODE` should therefore be an
explicit operator selection. Historical/forecast PV may inform the plan and a
future recommendation, but it must not silently switch control modes.

## Legacy behavior found

The v1 history contains several winter-specific iterations, notably commits
`012e346`, `26a4c89`, `ec75a44`, `f19831e`, `d7bd5a1`, `f80bb06`, `a16f338`,
`c0bfd1a`, and `baa75b9`.

The final legacy mechanism was heuristic rather than an optimizer policy:

- winter months increased the number of cheapest 24/48-hour charge slots;
- one hour was assumed to add 25 percentage points of SoC;
- the synthetic winter target was repeatedly tuned (75%, 90%, 125%, then 150%)
  to expose enough candidate slots;
- PV surplus was subtracted from the requested charge amount;
- stored-energy selling was handled separately using the day's highest N prices
  above a minimum SoC.

The intent was sound, but the fixed charge-rate assumption and split buy/sell
decisions did not prove that the house had enough energy to reach the next trough.
Winter Mode should preserve the intent using the new per-slot load, PV, price,
efficiency, power-limit, and SoC trajectory model instead of porting those constants.

## Isolation and runtime selection

The recommended boundary is a separate planning engine, not a second copy of the
hardware-control loop:

```text
energy_broker.py (one shared, tested application/control path)
        |
        +-- ess_optimizer_selector.py
                |
                +-- WINTER_MODE=False -> ai_powered_ess.py
                |
                +-- WINTER_MODE=True  -> ai_powered_ess_winter.py
```

- `lib/ai_powered_ess.py` remains the existing summer optimizer and should not be
  edited while Winter Mode is developed, except for a separately reviewed defect
  that also exists on the completed baseline.
- `lib/ai_powered_ess_winter.py` is a self-contained planner with its own state
  lattice, candidates, protection envelope, and formatter. Some duplication is
  intentional so winter policy development cannot change summer decisions through
  shared internal logic.
- `lib/ess_mode.py` freezes `WINTER_MODE` once at process startup. The optimizer
  selector and logical planning reserve consume that same frozen decision. Victron's
  autonomous hardware minimum is deliberately configured independently.
- `lib/ess_optimizer_selector.py` imports exactly one optimizer implementation and
  exposes the common broker/dry-run API. The winter module does not import the
  summer module, or vice versa.
- `lib/energy_broker.py` remains the sole code path that applies AC setpoints,
  Victron charge slots, negative-price feed-in protection, overrides, history,
  and plan publication. Duplicating this safety-critical application layer would
  create more risk than it removes.
- Both optimizers must return the same plan contract. Additive diagnostics may
  include `optimizer_mode` and `winter_policy`, but existing consumers must not
  need separate summer/winter handling.

Changing `WINTER_MODE` requires the existing MQTT-supervised process restart. The
configuration watcher must request that restart so a running process never swaps
engines or imports both implementations dynamically.

## Proposed configuration

- `AI_POWERED_ESS_ALGORITHM=True` remains the optimizer master switch.
- Add `WINTER_MODE=False` to `.env` and `.env.example`, exposed in the dashboard as
  **Winter Mode**.
- `False` selects the unchanged current optimizer; `True` selects only the winter
  optimizer after restart.
- ESS reserve selection should follow the explicit mode:
  `MIN_SOC_RESERVE_SUMMER` when off and `MIN_SOC_RESERVE_WINTER` when on. The
  existing calendar helper used by appliance scheduling is a separate concern and
  should not silently change the ESS algorithm.
- `VICTRON_HARDWARE_MIN_SOC` independently controls Victron's
  `MinimumSocLimit`. Its default is 0 because a value above current SoC causes
  Victron to enter Recharge immediately, outside the optimizer's price schedule.
  A nonzero value remains available as an intentionally selected last-resort
  hardware floor; it must never be inferred from the seasonal planning reserve.
- The plan JSON, history, logs, and dashboard should state the active mode so the
  operator can verify it without inferring behavior from a schedule.

Avoid adding many policy knobs initially. Candidate values such as an exceptional
spread threshold or load uncertainty buffer should start as named code constants,
be justified by backtests, and become writable settings only if operational tuning
is genuinely needed.

## Winter planning policy

### 1. Normal self-sufficiency candidate

- Disable active battery-to-grid export as a routine objective. PV surplus may
  still export normally when it cannot be stored.
- Optimize grid purchases and household self-supply over the known Tibber horizon.
  The per-slot load/PV forecasts and real battery efficiencies determine how much
  to charge; there is no fixed target such as 75% or a fixed 25%-per-hour rule.
- Prefer low-price replenishment windows and carry enough usable DC energy through
  the following higher-price household-demand interval.
- Include a conservative forecast-error allowance derived from historical
  predicted-vs-actual load where sufficient data exists, with a bounded fallback
  when it does not.
- When tomorrow's prices are not yet available, apply a conservative end-of-known-
  horizon household-energy requirement rather than assuming an unknown cheap slot
  appears at midnight.

### 2. Exceptional arbitrage candidate

- Build this as a separate candidate, never by weakening the normal plan in place.
- Permit active export only when the net spread clears charge/discharge losses,
  battery cycle cost, configured arbitrage margin, export fees, and an additional
  exceptional-opportunity hurdle.
- Before accepting it, stress-replay the trajectory with conservative household
  load and PV assumptions. At every point before the next low-price window, the
  battery must retain the house-energy requirement plus the configured minimum SoC.
- The candidate may charge up to `ESS_MAX_GRID_CHARGE_SOC`, but only energy above
  that protected envelope is available for sale. It must never assume 100% when the
  user configured a lower ceiling.
- Select it only when its expected improvement over the normal winter candidate is
  material and all physical/control invariants remain satisfied.

### 3. Explainability

Return an explicit policy block containing at least:

- `mode`: `winter`;
- selected candidate: `self_sufficiency` or `exceptional_arbitrage`;
- next low-price/replenishment window;
- forecast house energy required until that window;
- protected SoC/energy envelope and uncertainty allowance;
- exceptional spread, expected incremental benefit, and rejection/acceptance
  reason.

The Overview strategy sentence should describe winter intent in plain language,
for example: “Charge to 62% in the 02:00 trough to cover the morning/evening house
load; no battery export planned.”

## TDD and validation plan

### Phase 0 — freeze the summer baseline

1. Record golden summer outputs for representative current scenarios before
   introducing the selector.
2. Add a contract test for required result/schedule fields.
3. Add isolated-process tests proving `WINTER_MODE=False` imports no winter engine
   and `WINTER_MODE=True` imports no summer engine.
4. Add **Winter Mode** to the top AI ESS configuration section as the second option.
5. Prove selector integration leaves all summer golden plans byte-equivalent or
   numerically equivalent where timestamps are normalized.

### Phase 1 — winter self-sufficiency engine

Start with failing tests for:

- negligible PV, cheap overnight trough, morning/evening peak, then another trough;
- charging only enough to cover forecast household load plus reserve;
- no routine active export despite an ordinary profitable spread;
- load/PV forecast errors and the bounded uncertainty allowance;
- missing tomorrow prices and a conservative terminal requirement;
- negative prices, flat prices, and multiple equal-price troughs;
- configured charge/import/discharge limits and 15-minute/hourly resolution;
- infeasible coverage, which must degrade to the safest feasible plan and surface a
  warning rather than inventing energy.

### Phase 2 — exceptional spread candidate

Start with failing tests for:

- ordinary spread rejected;
- obvious spread accepted after all losses, fees, wear, and margin;
- charging up to `ESS_MAX_GRID_CHARGE_SOC` allowed when justified, with lower user
  ceilings enforced exactly;
- sale limited to energy above the household protection envelope;
- a later house-load spike or zero-PV stress case rejecting an otherwise profitable
  sale;
- cost-basis protection and `ESS_MIN_SELL_PRICE` remaining intact;
- no sell/retain oscillation around the exceptional threshold.

### Phase 3 — integration and UI

- Add the dashboard/config toggle and restart-required behavior.
- Publish active mode and winter diagnostics in STATE, history, plan JSON, console,
  and the P/L strategy summary.
- Keep all current override, grid-assist, negative-price, schedule-slot, and
  independent hardware minimum-SoC behavior on the shared application path.
- Add a dry-run/backtest tool that never writes MQTT or Victron settings.

### Phase 4 — adversarial review and activation gate

- Replay historical low-PV days with historical load/PV and representative price
  shapes; compare grid cost, peak imports, final SoC, cycles, and unserved protection
  envelope against the summer optimizer and legacy intent.
- Verify physical invariants: SoC continuity, min/max SoC, import/export and battery
  power limits, no overlapping/overflowing Victron slots, and no active export below
  policy floors.
- Run the full repository suite and static JavaScript checks.
- Review the complete diff specifically for accidental edits to
  `lib/ai_powered_ess.py`.
- Keep `WINTER_MODE=False` by default until dry-run evidence and an operator-reviewed
  activation plan are complete.

## Non-goals for this branch

- Automatically switching modes from the month, weather, or PV forecast.
- Replacing the shared energy-broker control/application path.
- Re-enabling the fixed-slot legacy scheduler while the AI optimizer is healthy.
- Treating routine export profit as the primary winter objective.
- Changing appliance winter scheduling.
- Modifying or backfilling historical data.

## Implemented behavior

- The dashboard toggle is the second setting in **AI ESS Optimizer**. A change
  requests the existing MQTT-supervised restart, including when an older `.env`
  receives `WINTER_MODE` for the first time.
- Mode and its logical planning reserve are frozen together for the process lifetime. Fresh-process
  integration tests prove the complete EnergyBroker path imports only the selected
  optimizer, not merely that the selector behaves in isolation.
- The normal candidate charges in selected low-price replenishment windows only,
  protects forecast household demand plus a bounded uncertainty allowance, uses
  stored energy for house load, and prohibits active battery export.
- Forecast uncertainty learns only from measured, EV-excluded `base_load_kwh`
  settlements. Raw or unclassified site-load records are rejected. Diagnostics
  publish the source, sample count, quantile, percentage cap, and kWh bounds; sparse
  history uses the bounded fallback.
- The exceptional candidate uses effective export economics, round-trip losses,
  cycle cost, configured margin, sell floor, persisted cost basis, a separate
  €0.20/kWh exceptional hurdle, and a minimum €1 expected improvement. Its
  time-varying export envelope stress-replays household load with near-zero PV.
- Grid charging never targets above `ESS_MAX_GRID_CHARGE_SOC`. Decimal ceilings are
  floored for Victron's integer target, and a cap below the winter reserve is
  reported rather than silently raised.
- Charge eligibility is constrained to at most five trough windows before
  optimization. Every published BUY is covered by an executable Victron window;
  no sixth idealized window is discarded after the SoC trajectory has relied on it.
- Exact opening SoC is retained. The winter lattice adapts down to a bounded 0.25%
  step when a configured 1% step would be too coarse to serve a normal 15-minute
  household load without accidental export.
- Plan JSON, STATE, history, logs, the Overview strategy sentence, dry-run output,
  and the read-only comparison tool expose the active mode and winter diagnostics.
- If Winter Mode cannot produce a feasible plan, the shared broker path applies a
  safe RETAIN result, clears stale charge windows, neutralizes a prior export
  command, reconciles the independent Victron hardware floor, and publishes an
  explicit degraded warning instead of leaving summer control active.

### Victron hard-minimum correction

Testing on the real system exposed an unsafe coupling in the initial implementation:
selecting a 40% winter planning reserve also wrote 40% to Victron
`MinimumSocLimit`. When actual SoC was below 40%, Victron correctly entered its own
Recharge state and imported at full power immediately, bypassing the optimizer's
chosen low-price window. Returning to Summer Mode could then leave the retained 40%
setting behind because the recreated in-memory state represented a missing value as
numeric zero and the old idempotence check mistook that for an already-applied 0%.

The correction separates the two concepts:

- `MIN_SOC_RESERVE_WINTER` and `MIN_SOC_RESERVE_SUMMER` are optimizer-only planning
  floors selected by the restart-frozen mode;
- `VICTRON_HARDWARE_MIN_SOC` is the sole source for Victron `MinimumSocLimit` and
  defaults to 0;
- the unconditional startup path force-publishes the configured hard minimum
  before its normal delay and module selection, so a stale retained 40% is cleared
  even before prices or a valid plan are available;
- normal reconciliation compares the observed Victron read topic, with an explicit
  presence check, rather than trusting a last-command shadow or treating missing as
  zero;
- dashboard edits apply the hard minimum immediately, including its first addition
  to an older `.env`, while MQTT failures are logged and retried by startup or the
  next optimizer cycle;
- an absent setting defaults to 0 for upgrade reconciliation, while explicit
  malformed, non-finite, or out-of-range values are rejected without changing the
  current Victron limit.

## Phase 4 results

The adversarial passes found and fixed five integration/physical defects before
review: reserve-mode drift before restart, first-time toggle writes not restarting,
post-optimization truncation beyond five Victron windows, the 1%-lattice
partial-self-supply failure, and seasonal planning reserves being coupled to
Victron's autonomous Recharge threshold. They also tightened classified-history
input, zero-PV exceptional stress, non-finite input handling, dimensional export
economics, exact user caps, failed-activation behavior, startup reconciliation,
and first-write config handling.

Representative 48-hour, 15-minute, approximately 3 kWh/day winter-PV replay:

| Scenario/engine | Net cost | No-ESS cost | Improvement | Active export slots | Candidate |
|---|---:|---:|---:|---:|---|
| Ordinary / summer | -€15.246 | €6.102 | €21.348 | 32 | summer trading |
| Ordinary / winter | €2.617 | €6.102 | €3.485 | 0 | self-sufficiency |
| Obvious spread / winter | -€33.939 | €9.714 | €43.653 | 23 | exceptional arbitrage |

All replay invariants passed: SoC continuity and energy balance, charge/discharge
and grid power limits, reserve/cap bounds, non-overlapping positive Victron slots,
BUY-to-slot coverage, and no routine winter export. The exceptional 48-hour replay
completed in 2.50 seconds with 37,384 KiB peak RSS on the development machine.
Target-Pi timing remains an operator activation check rather than a reason to alter
the default-off gate.

Validation completed:

- `export DEV=1; python -m pytest -s -q` — **380 passed**;
- `node --check frontend/static/js/app.js` — passed;
- `git diff --check` — passed;
- `lib/ai_powered_ess.py` is byte-identical to `HEAD`.

Read-only operator review commands:

```bash
python scripts/backtest_ess.py --mode compare
python scripts/backtest_ess.py --mode winter --exceptional
python scripts/ai_ess_dryrun.py --soc 50 --json
```

The live dry-run selects the mode from `.env` exactly as the service does and does
not publish MQTT/Victron writes. Leave `WINTER_MODE=False` until the winter dry-run
and target-device runtime are reviewed.

## Current progress

- [x] Confirmed new branch and clean starting tree.
- [x] Traced legacy seasonal behavior and its tuning history.
- [x] Quantified multi-year VRM winter-versus-summer production.
- [x] Defined proposed isolation boundary and runtime selector.
- [x] Froze summer golden outputs and result contract.
- [x] Implemented selector/configuration with full-startup import-exclusivity tests.
- [x] Implemented winter self-sufficiency candidate.
- [x] Implemented exceptional arbitrage candidate.
- [x] Added integration diagnostics, UI toggle/strategy, and dry-run tooling.
- [x] Decoupled optimizer reserves from Victron `MinimumSocLimit` and added
      startup/read-topic reconciliation for stale seasonal settings.
- [x] Completed replay, full-suite, and adversarial review.
