# Pending branch — EV load decomposition, forecast stability, and dashboard insight

Status: local, unmerged changes based on `main` release commit `487ca2c`

This document describes only the work pending after the previously merged v2.1
iteration. The archived documents `PR-Branch-current-iteration.md` and
`v2.1-Improvements-07-15-2026.md` remain records of their original merged changes.

## Purpose

Extended EV charging can be measured as ordinary house consumption and then
learned as a recurring base load. This branch separates measured EV demand from
house demand prospectively, handles legacy history conservatively without
rewriting it, corrects ownership and display of ABB charger-current data, and
adds clearer dashboard planning and forecast-accuracy views. It also fixes two
independent optimizer inputs that caused abrupt morning plan and final-net
forecast changes: stale new-day PV counters and an over-broad battery cost-basis
floor.

## EV and base-load decomposition

- Cycle history keeps raw `load_w` and adds measured `ev_w`, validated
  `base_load_w`, and `load_decomposition_quality`.
- Settlement history diffs the ABB meter's monotonic `Ac/Energy/Forward`
  totalizer into `ev_charge_kwh`. A `base_load_kwh` is recorded only when both
  load and EV deltas are complete and physically plausible.
- The history learner prefers measured base load. For legacy records it retains
  ordinary loads but rejects obviously high, unclassified samples rather than
  estimating an EV component.
- Multiple replans within one daily 15-minute slot are reduced to a daily median,
  followed by a median/MAD high-outlier guard across days.
- No backfill script or history rewrite is part of this branch. New classified
  records replace the legacy learning window naturally.

The detailed data contract and migration behavior are in
[`EV_LOAD_DECOMPOSITION.md`](EV_LOAD_DECOMPOSITION.md).

These classification limits affect only which history samples may teach the
forecast. They do not impose a new charger, inverter, import, or export control
limit. Missing, stale, or physically incoherent meter data is retained with a
quality label but is not converted into an invented zero or estimated EV load.

## ABB charger-current ownership and display

- Corrected the L1 and L2 subscriptions to
  `evcharger/42/Ac/L{1,2}/Current`, matching the already-correct L3 path.
- The ABB dbus driver already applies the Modbus scaling. No `/100` or `/1000`
  conversion is applied in consumers.
- `lib/event_handler.py` is the sole publisher of the shared measured
  `Tesla/vehicle0/charging_amps` topic. A real zero is published instead of being
  mistaken for a missing argument.
- Fleet `ChargeAmps` is retained only as diagnostic state because its change-only
  stream can otherwise leave a stale non-zero display after charging stops.
- The Vehicle tab and legacy consumers keep the per-phase measured convention.
  Only the Power Flow EV card sums the three physical ABB phase currents to match
  the Tesla app/car total-current convention requested for that card. At 100 W or
  less, it displays 0 A rather than retained idle current.

## Dashboard additions

### Backend-offline state

The sticky header now shows an accessible **Server Offline** banner when no
backend response or SSE heartbeat has arrived for 35 seconds. The last good data
remains visible, the footer explicitly says it is showing stale data, and the
warning clears automatically after reconnection. Raw fetch errors are not written
into the status strip.

### Remaining-day strategy

The P/L Summary card includes a short, human-readable description of the plan
from now until midnight: charge target and windows, remaining export peaks, and
the cost/profit strategy.

### Daily final-net forecast evolution

Each optimizer cycle records `plan_today_remaining_net_eur` and
`forecast_day_net_eur` using a profit-positive convention. Tomorrow is excluded
even when its prices are present in the optimizer horizon.

The Trends monthly chart renders genuine intraday forecast snapshots as a
candlestick-style mark:

- wick: lowest to highest final-net forecast seen that day;
- body: first to latest forecast;
- solid round point: final settled actual;
- hollow point: today's actual net so far.

Legacy days without these snapshots remain actual-only dots. No forecast range is
invented or backfilled.

## Daily-net forecast stability and battery planning

Two independent rollover/economic faults explained the large morning forecast
swings seen on 18 July:

- The persisted battery-average cost basis was applied to every future battery
  discharge. A small expensive charge into a nearly empty battery therefore
  blocked a new `€0.13 -> €0.30+` cycle until PV production diluted the average
  basis. The dynamic basis now protects only the SoC already stored when planning
  begins. Energy charged later in the plan can be sold back down to that protected
  opening tranche. `ESS_MIN_SELL_PRICE` remains an absolute operator floor for all
  active battery exports.
- Victron MPPT daily-yield counters retained the previous day's 39.12 kWh until
  after 02:00. That stale value was subtracted from the new day's VRM forecast,
  temporarily forcing remaining PV to zero. Positive pre-sunrise yield is now
  classified as stale and excluded. Successful forecasts are stamped with local
  today/tomorrow dates, and yesterday's dated tomorrow forecast is promoted at
  midnight until the first successful new-day refresh.

The plan JSON and cycle history now expose PV forecast dates/freshness plus the
cost-basis floor and opening protected SoC. These fields make future forecast
movement attributable instead of leaving the chart to show an unexplained jump.
The daily-settlement two-stage planner also carries only the original protected
tranche into its next-day segment, rather than relabelling a newly charged battery
as entirely historical energy.

The historical basis remains deliberately conservative: it may protect the
opening tranche from a below-basis export, but it cannot block a profitable cycle
made from energy charged later in the same plan. The separately configured
`ESS_MIN_SELL_PRICE` remains the unconditional operator policy.

## Data and compatibility notes

- Existing raw history fields and accounting totals remain unchanged; all EV/base
  fields and forecast-provenance fields are additive.
- Existing history is not rewritten. Unclassified legacy samples above 6 kW are
  excluded from load learning, while ordinary legacy samples remain usable.
- State written before forecast dates existed follows the legacy fallback until
  the next successful VRM refresh. Thereafter, explicit local dates control the
  midnight today/tomorrow promotion.
- No new dependency or writable `.env` option is introduced.
- No backfill command is shipped or required.

## Test and review status

- Full repository suite: `325 passed` under `DEV=1`.
- Regression coverage includes meter-topic ownership/scaling, cycle and settlement
  quality rejection, robust load learning, PV midnight rollover, cost-basis
  tranche handling across day segments, dashboard offline recovery, strategy
  summaries, and forecast-candle rendering.
- A replay of the affected price horizon with a `€0.3131/kWh` historical basis
  changed the old result of zero buys/zero sells into an economically valid plan
  targeting 100%: 11 cheap buy slots followed by 12 peak sell slots.
- Replay invariants passed: chronological slots, continuous SoC, configured power
  limits, protected opening SoC, and a single 100% Victron charge window.
- JavaScript syntax checks passed for `app.js` and `charts.js`.
- Server-loss and recovery behavior was exercised with a temporary backend.
- Desktop and mobile layouts were visually checked in Chrome and Firefox.
- Dependency-originated `pkg_resources`, namespace-package, and Python 3.12
  `utcfromtimestamp` warnings are narrowly filtered by module/message in
  `pytest.ini`; unrelated project warnings remain visible.

## Follow-up

The future EV smart-charge job/deadline design remains in `TODO.md`. It is not
implemented by this branch. During an active real-world charge, verify that all
three ABB `Ac/L{1,2,3}/Current` topics are populated; that operational check is
also retained in `TODO.md` and is not a software blocker.

## Deployment verification

1. Stop the existing `main.py` process before restarting it; never run two control
   loops concurrently.
2. After the first successful VRM refresh, confirm the plan JSON exposes dated PV
   forecast fields and `pv_actual_quality`.
3. Confirm a low-price charge followed by a profitable peak can target the
   configured maximum SoC even when the opening battery tranche has a higher
   historical cost basis.
4. Stop the backend briefly and verify the dashboard shows **Server Offline**,
   retains the last good values, and clears the warning after recovery.
