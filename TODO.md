# TODO / roadmap

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

## AI Advisor
- Phase 2 — approve-to-apply for tunables only. Each setting has hard min/max bounds;
  on approval the system runs a dry-run backtest, shows projected EUR, writes .env
  (hot-reloads, no restart), and auto-reverts if the next day underperforms. Bounded
  numbers can't crash the controller — this is the safe sweet spot.

- Phase 3 — code changes via PR, not hot-patch. Let the model propose a diff + tests;
  the "apply" button opens a PR/branch for human review and your normal pytest gate.
  Keep a human on the actual diff before anything restarts a 16 kW controller.

## EV smart-charge scheduling (phase 2 of the EV/base-load decomposition)

The EV/base-load split (see `docs/EV_LOAD_DECOMPOSITION.md`) is the enabling
groundwork. Now that base load and EV charge energy are recorded separately, EV
charging can be modelled as a **schedulable flexible load** the optimizer places
into the cheapest / greenest slots — the "set and forget" goal:

> "I need to leave by 10am tomorrow. Charge the car most efficiently and as
> cheaply as you can but have it at 80% by 10am."

### Job interface
Define an EV charge job the DP can consume:

```
EvChargeJob = {
    current_soc:  float,   # % now (from telemetry battery_soc)
    target_soc:   float,   # % required by the deadline (e.g. 80)
    deadline:     datetime,# be at target_soc by this time
    max_kw:       float,   # charger ceiling (16 kW for this installation)
    min_kw:       float,   # optional floor if the car won't modulate low
    energy_kwh:   float,   # derived: (target-current)/100 * battery_capacity_kwh / charge_eff
}
```

### DP integration
- Add EV energy as an additional, deferrable demand over the horizon: the
  optimizer already models per-slot grid cost and PV surplus; extend it to place
  `energy_kwh` of EV load across the slots between now and `deadline` that
  minimise cost (prefer PV surplus, then cheapest grid), subject to `max_kw`.
- Respect existing safety: never let EV scheduling override ESS control, the
  Tesla budget guard, or the home-geofence gate. EV draw is charged via the
  dedicated `ev_charge_requested` intent flag, not `grid_charging_enabled`.
- Feasibility check: if `energy_kwh` can't fit before the deadline at `max_kw`,
  charge flat-out and surface a "won't reach target by deadline" warning.

### Data / inputs already available
- `current_soc` / `target_soc`: telemetry `battery_soc` / `battery_soc_setpoint`.
- `ev_charge_kwh` per slot (settlement) + `ev_w` per cycle: measured EV behaviour
  to validate the model and learn real charge curves (taper near the SoC limit).
- Charger ceiling and phases: installation ceiling (16 kW), telemetry
  `ChargerPhases`, and evcharger `Ac/Power`.

### Suggested build order
1. `EvChargeJob` dataclass + a pure planner (`plan_ev_charge(job, price_slots,
   pv_forecast)`) returning per-slot EV kW — unit-tested in isolation.
2. UI to set target SoC + deadline (Vehicle tab), writing the job to STATE.
3. Wire the planner's per-slot EV kW into the optimizer's demand, gated behind a
   `EV_SMART_CHARGE_ENABLED` flag (off by default).
4. Close the loop: compare planned vs measured `ev_charge_kwh`, tune the charge
   curve / efficiency.

# Bugs / Testing

## MEDIUM — wrong behaviour / cost leak, not dangerous

### M1 — evcharger per-phase amps: verify on the bus
The `evcharger/42` device is a real **ABB B23/B24** 3-phase meter (confirmed on
the bus: genuine per-phase `L{1,2,3}/Current` + total `Ac/Power` /
`Ac/Energy/Forward`). The shared current metric remains per-phase for control and
the Vehicle tab. Only the powerflow EV card shows the requested sum across active
phases. Remaining task: sanity-check during an active charge that all three
`L{n}/Current` registers are populated (not just L1) so the per-phase control
value is right. Not a code change. See `docs/EV_LOAD_DECOMPOSITION.md`.

## LOW

- **L3 — Two `update_charging_amp_totals` implementations** (in
  `event_handler` and `ev_charge_controller`) both set
  `tesla_charging_amps_total`. Same value, but duplicated logic —
  consolidate to one source to avoid future drift.

## Verify operationally (not a bug)

- During an active charge, confirm all three corrected ABB topics
  (`Ac/L{1,2,3}/Current`) track the physical phases. Values are already amperes;
  the ABB dbus driver applies the Modbus register scale and no additional `/100`
  or `/1000` conversion belongs in consumers.
- Confirm `retained: true` on the fleet-telemetry dispatcher persists
  across receiver restarts (so the bridge always snapshots current state on
  connect).
