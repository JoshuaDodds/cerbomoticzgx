# Peak-price ESS dispatch review — 31 October 2026

## Recorded behavior

The synced `data/history/ess-2026-10-31.ndjson` contains 79 Winter decision
cycles through 19:00 and eight Summer cycles from 19:04 through 19:45. The
Winter controller chose `RETAIN` in 67 of its 79 cycles. At 17:15, the tariff
was €0.4949/kWh, SoC was 98.7%, the protected SoC reported by the plan was
96.64%, and the meter imported 2,658 W. At 18:00, €0.4961/kWh and 98.5% SoC,
it retained again and imported 1,986 W. The next selected replenishment slot
was 23:45. This is contrary to the intended use of stored energy at peak
prices. The recorded import cost at 19:30 was €24.56; the synced file does not
yet establish the final daily cost. Household demand and any EV load also
affect that total, so the entire bill cannot be assigned to the optimizer.

The Summer selector chose `pv_first_self_sufficiency` after the 19:04 switch.
At 19:30 its plan said `RETAIN` for a forecast 0.3573 kWh household load,
although the physical meter reported battery discharge and only 67 W grid
import at that instant. Planned action and realized power can differ while a
setpoint settles; both must be checked during validation.

## Causes and correction

- Winter's forecast coverage calculation set mandatory SoC checkpoints at
  selected low-price windows. A short 23:45 window could require nearly full
  SoC after it, which compelled the planner to preserve energy and import
  during the preceding peak. The coverage estimate remains visible as
  `coverage_target_soc_percent`, but the actual `protected_soc_percent` now
  reports only the configured outage reserve. The optimizer minimizes known
  import cost above that reserve and gives a bounded value to a few hours of
  energy beyond the known price horizon. Forecast coverage remains a useful
  target, not a mandatory floor. Exceptional battery export still requires its
  separate near-zero-PV household envelope and economic hurdle.
- Summer PV-first forbids active battery export. With a 42 kWh battery, one
  1% SoC step releases about 0.386 kWh AC at 92% discharge efficiency, larger
  than a typical 0.35 kWh quarter-hour load. The planner therefore rejected
  useful discharge as export. PV-first and live Green now include exact household self-supply transitions
  in addition to a finer SoC lattice. The search retains bounded representatives
  per SoC bucket and charge-stage state, so fractional physical energy is kept
  without an unbounded subset search. Other Summer candidates retain their
  configured step. No new operator
  setting was added.

## Validation and remaining limits

Regression tests cover a peak-price Winter horizon with a short future trough
and the Summer small-load case, including reserve and no-export assertions.
The change has not been applied to the running controller by editing this
repository. After deployment, compare planned and realized quarter-hour
import, charge slots, SoC, and cost on days with both modes. The Summer
selector retains its benefit hurdle and now compares Trading, PV-first and
`green_conservation`. Green uses the higher of the seasonal and Winter reserves,
can buy grid energy when useful, and forbids routine battery export. On a
forecast net energy deficit, Green is the conservative baseline; Trading can
still win by clearing its economic hurdle. An old PV-first dwell period cannot
block that transition. The former
protected-hybrid policy remains available to explicit offline callers.

## Follow-up corrections after review

Winter now replays its published integer-target windows before comparing normal
and exceptional candidates. The replay charges at the feasible full rate, holds
inside an active window, carries the resulting SoC into later slots, and
recomputes cost, wear and terminal value. Targets round upward only within the
integer user ceiling; the reported trajectory includes any resulting shortfall.
This makes the existing window grouping explicit in the economic comparison;
it does not claim a globally optimal search over every possible window grouping.

The learned, bounded uncertainty allowance now expands the economically valued
continuation buffer in normal Winter planning. It does not become a mandatory
household floor. Continuation price is capped by the horizon mean as well as
its lower quartile, so a horizon dominated by peak prices does not value unknown
future demand as another certain peak.

EV phase-current events record individual receive timestamps. Only fresh phase
current can establish local charging; stale retained current no longer keeps an
expired manual Start alive. Fresh EVCS power and fresh vehicle charging evidence
remain available.

Validation includes small loads down to 4 W, cheap-to-peak Green charging, integer
charge-target replay, learned-risk procurement, and stale-current Start expiry.
The full suite passes locally; actual hardware timing and monetary savings must
be checked after deployment.

## Follow-up: Winter fractional household demand

Winter now includes exact feasible household self-supply and PV-only charging
transitions, carries fractional SoC forward, and can hold that exact state.
Partial lattice self-supply is rejected because a neutral ESS setpoint actually
serves the full feasible household load. Two representatives per SoC bucket
bound the search without rounding stored energy into existence. This removes
the systematic extra discharge introduced by execution replay on no-charge
trajectories.

Using the 20:15 published forecast (92.0% opening SoC), an offline replay with
the same tariffs/load/PV supplies the 21:15 small load, supplies tomorrow's
21:00 peak, and ends tomorrow near 43.2%. It still schedules no grid refill:
this is a forecast decision with existing stored energy, not evidence that
charging has been disabled. Existing cheap-to-peak procurement regressions
continue to check that charging occurs when useful. These figures are modeled,
not measured savings.

`next_replenishment_time` and `active_charge_windows` now describe actual
published charging instructions. `next_eligible_charge_time` and
`eligible_charge_windows` separately describe opportunities the optimizer
considered. A null next replenishment time means no charge is scheduled.

## 2 November: avoid premature charging within a grouped window

The 23:08 plan grouped a tiny 23:45 purchase with later cheap purchases under
one final SoC target. Hardware therefore front-loaded the charge at about
40 cents instead of waiting for the 01:45–02:00 prices near 38.2 cents.

Winter now evaluates later starts within each selected charge window against
the executable trajectory. A later start is accepted only if it lowers the
full modeled objective and preserves at least the original battery energy at
the window's end. Charging power, import limits, target ceilings and the five
window limit still apply. Recovery of an opening outage-reserve deficit is
not delayed. This is a bounded timing improvement over the selected windows,
not a global search over every possible schedule.

The operator confirmed that the current 0% reserve is intentional; this change
does not alter it. A regression checks that two cheaper slots are chosen when
one slot cannot provide the required energy.
