# ESS seasonal economics review — 21 October 2026

## Operator objective

- **Green (Winter Mode):** buy only the energy needed at affordable price dips
  to supply the house until the next dip, while preserving an explicit outage
  reserve. Low PV is expected. Routine battery-to-grid export is not the goal;
  an exceptional sale may be considered only above protected household energy.
- **Trader (Summer Mode):** use surplus PV, negative prices and material
  arbitrage opportunities for net profit. During grey or rainy periods, switch
  temporarily to a Green-style conservation policy that *can grid-charge* for
  future household demand, but does not export the protected energy. Resume
  Trader only when its forecast advantage clears wear, fees and forecast risk.

The mode switch is manual and restart-isolated. The operator confirmed that
Summer ran until the morning of 21 October; the winter sample is not evidence
for a months-long Winter Mode regression.

## Evidence and limits

The canonical `history_store.read_day_strict` view covers **130 days**, from
14 June to 21 October 2026, with 24,969 records: 12,462 cycles and 12,092
settlement rows. The cycles include 9,168 explicitly labelled Summer rows,
eight Winter rows, and 3,286 older rows without a mode label. Four of the
Winter rows occur from 08:27 through 08:45 on 21 October. September daily
NDJSON and monthly Parquet overlap for some days; reading both formats directly
would double-count those records. The canonical reader gives daily NDJSON
precedence and was used for all counts here.

The 21 October switch began at **23.9% SoC** against the configured **40%**
Winter planning reserve. The first Winter decision at 08:27 recorded RETAIN
while the meter still showed battery export; subsequent
08:30–08:45 samples held battery power near zero and imported household load at
about **€0.43–€0.46/kWh**. The plan warned
`coverage_infeasible_safest_feasible_plan` and identified a 12:15 next
replenishment opportunity. The synced history stops at 08:45, so it cannot
show whether that later charge occurred. `VICTRON_HARDWARE_MIN_SOC=0` is
separate from the software reserve; raising the Victron minimum above live SoC
would trigger unscheduled Recharge, so this is not an automatic fix.

Across the 20 full October days, 45 Summer cycle samples imported more than
0.5 kW in a day's upper price quartile while SoC was at least 40% and demand
exceeded PV by more than 0.5 kW. These are review candidates, not 45 proven
mistakes: a later visible price can make holding rational. For example, the
14 October 18:15 decision held at €0.4485/kWh with a later visible value of
€0.478/kWh. The 6 October 18:15 decision held at €0.5061/kWh under sell
hysteresis. The replay must account for the later sale, conversion loss,
capacity and closing SoC before assigning a missed euro value.

October's 1,889 complete metered settlement rows show about **€130.82 net
grid cost** under recorded prices and a **€160.84 simple instantaneous
no-storage comparator**. The apparent €30.02 difference is *not* a validated
battery saving: it excludes incomplete intervals, beginning/ending stored
energy, battery wear, tariff settlement differences and the counterfactual
effect of battery behavior on PV routing. Do not use it to select a policy.

The same limitation applies to every monthly row below. In June through August,
recomputing grid cost with the price attached to each settlement differs from
its recorded `actual_cost - actual_reward` by roughly €12, €28 and €19 even
before restricting to complete load/PV rows. The attached price and realized
cash fields cannot yet be treated as a reconciled invoice ledger. The simple
no-storage column therefore describes an instantaneous physical comparator,
not a verified euro saving or profit caused by this optimizer.

The modeled grid ledger shows the seasonal transition. Positive cost means
net spending; negative cost means modeled export reward exceeded import cost
on the qualifying intervals. June starts on the 14th and October ends on the
21st, so their totals are not directly comparable with complete months. The
simple comparator buys each interval's positive site load minus PV and sells
any surplus at that interval's recorded `price_sell`; it has no battery state.

| Month | Complete metered intervals | PV (kWh) | Site load (kWh) | Modeled net grid cost (€) | Simple no-storage cost (€) |
| --- | ---: | ---: | ---: | ---: | ---: |
| June, partial | 773 | 334.2 | 299.6 | -70.66 | +21.46 |
| July | 2,876 | 1,187.1 | 1,054.8 | -84.74 | +39.18 |
| August | 2,815 | 1,022.7 | 1,005.3 | -50.04 | +77.96 |
| September | 2,738 | 681.2 | 880.7 | -10.41 | +135.91 |
| October, partial | 1,889 | 356.9 | 757.3 | +130.82 | +160.84 |

For a *price-aligned physical proxy*, reprice the measured grid imports and
exports at the same per-row buy/sell prices used by the no-storage comparator.
This removes the recorded-cash versus attached-price mismatch, but is still
not a bill comparison. Discharge throughput below is the sum of positive SoC
drops times the configured 42 kWh capacity; the last two columns subtract a
wear assumption from the simple comparator minus repriced grid cost.

| Month | Repriced grid cost (€) | Discharge (DC kWh) | Proxy after €0.01/kWh wear (€) | Proxy after €0.03/kWh wear (€) |
| --- | ---: | ---: | ---: | ---: |
| June, partial | -77.60 | 331.9 | +95.74 | +89.11 |
| July | -111.99 | 1,268.5 | +138.49 | +113.12 |
| August | -69.07 | 1,145.0 | +135.57 | +112.67 |
| September | -2.14 | 1,214.5 | +125.91 | +101.62 |
| October, partial | +130.79 | 754.8 | +22.50 | +7.41 |

These positive proxies indicate that storage *may* have helped in the Summer
sample. They cannot establish the actual optimizer benefit: the comparator
assumes the same site load and PV routing without a battery, missing intervals
are omitted, starting/closing SoC is not valued, throughput is based on BMS
SoC deltas rather than calibrated capacity, and annual netting/provider fees
are unresolved. October's small proxy after a plausible higher wear allowance
especially warrants invoice reconciliation before claiming a net saving.

### Summer behavior across the full history

The early history has 3,286 cycles without an optimizer mode label. Adaptive
candidate scores start appearing on 19 August; only that later span can test
the Summer selector itself. From then through 21 October there are **5,251**
cycles with an adaptive selected strategy: **5,182 Trading**, **27 PV-first**
and **42 protected-hybrid**. These are decision samples, not independent
15-minute metered outcomes. Historical records also contain four isolated
pre-21 October rows labelled Winter; the operator confirms that the system
was normally running Summer until the manual switch on 21 October.

For a reproducible low-PV cut, require at least 88 fully metered settlement
intervals in a day and daily PV below half of measured site consumption. There
are **16 such days with adaptive selection** from August to October. Trading
was selected in **1,414 of 1,480** recorded adaptive cycles on those days
(95.5%). In October alone, all eight qualifying low-PV days spent most cycles
in Trading; 9 October and 18 October are the only two with some protected-
hybrid cycles. This confirms that today's adaptive choice rarely becomes a
conservation policy in grey weather. It does not establish that Trading lost
money: its published candidate score often exceeds the existing alternatives,
and neither alternative implements the requested Green behavior.

The recorded buy price never went below zero in this data set (monthly minima
range from about €0.105 to €0.133/kWh). Negative-price capture cannot be
validated from these months; it needs scenario replay or future negative-price
days. The Summer planning reserve and Victron hard minimum in the local config
are both 0%. SoC was at or below 5% in **3,983 of 12,462** historical cycle
samples (32%). This is consistent with the configuration but leaves little
stored outage energy during those samples. A year-round outage reserve would
need an explicit operator choice and an equal-reserve policy comparison.

The low/high price behavior also needs a decision-time replay. October has
234 BUY samples in its daily lower price quartile and 317 SELL samples in its
upper quartile, but 109 lower-quartile samples had more than 0.5 kW of net
household demand and grid import without a BUY, while 82 upper-quartile
samples imported over 0.5 kW with at least 40% SoC. These use all 21 October
calendar days including the partial switch day, so they differ from the
20-full-day, 45-sample high-price screen above. They are diagnostic counts,
not cumulative energy or proven missed euros; load, PV, later prices and
control authority need to be paired with the next settlement.

Meter completeness is a material replay constraint. A typical full October
day has 96 settlement rows but only 92–93 with all load, PV, import and export
fields populated. Midnight load counters often reset; other gaps include
unavailable night PV measurements. Unknown values must remain unknown unless
a physical and timestamped source justifies reconstruction. The existing
weather validation report also recommends keeping both apply gates off:
the HVAC load shadow improves MAE by only 0.7% against a 5% gate, while the
matched PV evidence spans two nowcast pipeline versions.

## Confirmed implementation findings

1. Winter chooses up to five charge troughs using the lower 30% of known buy
   prices, then enforces household-coverage checkpoints at those troughs. If
   any checkpoint is physically unreachable, it retries the whole plan with
   *all* checkpoints removed. When SoC starts below reserve, the cost objective
   can then choose no charge because energy bought merely to reach 40% cannot
   be spent in normal grid-connected operation. This is the reserve-recovery
   defect addressed in this change.
2. A read-only historical-input probe for 19 October, beginning at 23.1% SoC,
   reproduced `coverage_infeasible_safest_feasible_plan` and zero charge
   windows before the fix. After the fix, it scheduled one 13:15 charge window
   and ended at 40%. The probe used measured future prices/load/PV for 94
   usable intervals and conservatively treated missing night PV as zero. It
   establishes behavior under that input, not live forecast accuracy or a
   monetary benefit. Physical energy-balance, power-limit, SoC-continuity and
   five-window invariants passed on the 6, 14, 19 and 20 October probes.
3. The Summer selector's `pv_first_self_sufficiency` candidate forbids grid
   charging. Its `protected_hybrid` candidate can buy in cheap windows but
   still permits active battery export. Neither exactly represents the
   operator's grey-day Green policy. The Summer adaptive selector is enabled
   in the local `.env`, but does not select the separate Winter engine.
   A `green_conservation` variant is now executable through the Summer engine
   for offline replay: it uses the protected-hybrid replenishment and
   household-coverage constraints, protects at least the existing Winter
   outage reserve, and prohibits active battery export.
   The live selector does not consider it until historical replay and shadow
   validation establish an economic and physical benefit.
   In a 48-slot, 08:00–20:00 diagnostic with actual 19 October inputs, the
   Trading plan contained seven active battery-export slots, protected hybrid
   three, and Green zero; each used at most five charge windows. This uses
   future measured inputs and is a constraint check, not an estimated saving.
4. The local `.env` has 143 active settings, including 42 ESS/optimizer/season
   settings. The 42 kWh capacity and 40% Winter reserve bind 16.8 kWh as
   outage energy. That reserve is an intentional safety choice; economic
   comparisons must keep the same reserve between Green candidates.
5. The configured charge and discharge efficiencies are each 0.92, or about
   **84.6% round trip**. This is an assumption, not a measured clean-cycle
   calibration. The current `ESS_BATTERY_CYCLE_COST=0.01` and
   `ESS_ARBITRAGE_MARGIN=0` give thin Summer trades little additional cushion.
   Test these sensitivities before altering production values.

The first configuration simplification groups the existing keys by operator
decision, without deleting or changing their values:

| Operator-facing decision | Existing keys to consolidate or explain |
| --- | --- |
| Seasonal goal and automatic grey-day conservation | `WINTER_MODE`, `ESS_ADAPTIVE_POLICY_ENABLED` |
| Outage energy and physical battery limits | `MIN_SOC_RESERVE_WINTER`, `MIN_SOC_RESERVE_SUMMER`, `VICTRON_HARDWARE_MIN_SOC`, `BATTERY_CAPACITY_KWH`, `ESS_MAX_*` |
| Import/export tariff and permissions | Tibber price feed, `ESS_EXPORT_PRICE_FACTOR`, `ESS_EXPORT_FEE`, `ESS_MIN_SELL_PRICE` |
| Advanced measured assumptions | `AC_DC_*_EFFICIENCY`, `ESS_BATTERY_CYCLE_COST` |
| Internal controls to derive or hide after sensitivity tests | `ESS_ADAPTIVE_*`, `ESS_TERMINAL_VALUE_FACTOR`, `ESS_EXPECTED_PEAK_PRICE`, `OPTIMIZER_SOC_STEP_PCT`, `ESS_SELL_*`, `ESS_PV_*` |

The dashboard now presents seasonal mode, reserves, power limits, tariffs and
the Summer comparison switch directly. Optimizer internals in the first three
ESS-related groups sit under an accessible **Advanced settings** disclosure;
the API, editable allow-list, advisor context and `.env` values are unchanged.
The Summer comparison switch is labelled honestly: its current candidates do
not yet deliver the requested Green-style grey-day fallback. `WINTER_MODE`
still requires a supervised restart.

## Change in this iteration

When the full Winter coverage plan is infeasible, the fallback now first tries
to restore an opening reserve deficit by the **earliest selected trough with
enough conservative grid-charge headroom**. If the exact physical optimization
finds that checkpoint unreachable, it falls back to terminal recovery. The
terminal recovery requires the configured reserve by the end of the known
horizon when possible; otherwise it restores as much as feasible. Physical
grid/battery limits, the user's grid-charge SoC cap, and the five Victron
charge-window limit still apply. The existing warning remains visible.

This is a reserve recovery correction, not a claim that the current Winter
planner fully meets the Green objective. In particular, a price trough may
still be chosen by a percentile rather than by whole-horizon economic value.

## Implementation plan and gates

1. **Establish the bill and physical ledger.** Pair each cycle with its next
   settlement; preserve the price known at decision time, tariff component,
   meter quality, Victron scheduled window, battery SoC and control authority.
   Build cash, wear and carried-energy accounts. Compare against the supplier
   invoice/API before calling modeled reward "settled profit". Archive compact
   planning inputs or a reproducible plan reference with bounded daily storage
   so future decisions can be replayed without guessing the forecast.
2. **Calibrate the physical model.** Estimate usable capacity, charge and
   discharge efficiency from clean, metered cycles; exclude EV and missing
   counters. Validate Victron's full-power-to-target behavior, schedule timing
   and inverter limits. Keep estimates in an audited advanced configuration.
3. **Define Green as an executable policy in both seasons.** The Green solve
   should pick replenishment windows and the minimum required target through
   the next feasible dip, protect the outage reserve, permit grid buying, and
   reject routine active export. A summer grey-day fallback must use the same
   economic rule while preserving the restart boundary between seasonal
   engines. Make price-window eligibility aware of forecast load and charge
   headroom, not only a percentile threshold.
4. **Require a material Trader advantage.** Evaluate Trader and Green on the
   same horizon, starting SoC, export tariff, wear, uncertainty scenarios and
   closing-energy value. Trade only when the incremental expected benefit
   clears a visible euro hurdle and remains positive under plausible PV/load
   misses. Explain each switch with a plain-language reason and expected
   benefit. The current `pv_first` and `protected_hybrid` rows cannot be used
   as the final Green comparator without changing their constraints.
5. **Backtest without hindsight claims.** First use actual load/PV and prices
   as a physical upper-bound diagnostic. Then use only forecasts and price
   horizons that were known at each historical decision; withhold or mark days
   where those snapshots are absent. Compare against no storage, observed
   control, and a simple off-peak-to-peak baseline on equal reserve and
   terminal-energy terms. Report daily and monthly euro deltas, forecast
   calibration, reserve breaches and sensitivity intervals. Do not promote a
   candidate based on the read-only approximate Winter dashboard row.
6. **Simplify configuration after sensitivity testing.** Expose the seasonal
   objective, outage reserve, battery/inverter limits, tariff and export
   permission. Derive or hide tunables whose effect is negligible or safely
   learnable. Separate settings for physical safety, tariff facts and policy
   preferences; add an operator view showing the *effective* values used by
   each plan.
7. **Shadow and staged rollout.** Run candidate plans without Victron writes
   through low-PV and mixed-weather days. Require no silent reserve failure,
   no uncovered BUY, at most five executable windows, stable runtime on the
   target host, and a positive bill outcome after wear and carried energy.
   Activate one guarded change at a time with a rollback copy of the prior
   configuration and explicit stop criteria. The Dutch saldering change on
   **1 January 2027** requires a provider-aware tariff model before assuming
   Summer export continues to offset imports at the same value.

## External references

- [Victron ESS scheduled charging and self-consumption limits](https://www.victronenergy.com/media/pg/Energy_Storage_System/en/configuration.html)
- [NREL: forecast uncertainty in residential battery control](https://docs.nlr.gov/docs/fy21osti/76240.pdf)
- [NREL: degradation-aware dispatch](https://docs.nlr.gov/docs/fy22osti/82454.pdf)
- [Dutch government: saldering ends 1 January 2027](https://www.rijksoverheid.nl/themas/klimaat-milieu-en-natuur/energie-thuis/salderingsregeling)
