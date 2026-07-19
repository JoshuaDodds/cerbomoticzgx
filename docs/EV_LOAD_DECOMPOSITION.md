# EV / base-load decomposition

## Why this exists

The EV charger is inside the Victron AC-out/consumption measurements. A long EV
charge can therefore look like a recurring house load to the per-slot learner,
causing the optimizer to plan for a phantom load on following days.

This implementation does **not** rewrite or estimate historical records. It
handles existing history conservatively at read time and begins collecting
measured, quality-labelled data for future forecasts.

## Meter inputs

| Bus topic (`N/<id>/evcharger/42/...`) | Meaning | Stored as |
|---|---|---|
| `Ac/Power` | total EV real power, W | cycle `ev_w` |
| `Ac/Energy/Forward` | lifetime EV energy, kWh | settlement `ev_charge_kwh` after a validated diff |
| phase current topics | measured phase current, A | shared current/control metric |

The charger ceiling used only for history plausibility checks is 16 kW. It does
not constrain charging or any live control path.

## New records

Cycle records remain additive: raw `load_w` is preserved, `ev_w` records the
meter reading, and `base_load_w = max(0, load_w - ev_w)` is written only when the
two readings are coherent. `load_decomposition_quality` is one of:

- `measured`
- `ev_meter_missing`
- `ev_meter_stale`
- `ev_power_incoherent`
- `load_missing`

Settlement records diff the EV meter totalizer only when the endpoint existed at
both boundaries, the interval is complete, the counter stayed monotonic, and the
delta is physically plausible for a 16 kW charger. Unknown data stays `None`
rather than being converted to zero. `ev_meter_quality` and
`load_meter_quality` explain rejected values. Raw `actual_load_kwh` remains
unchanged for accounting compatibility; invalid data is not promoted into
`base_load_kwh`.

## Learning from history

`_historical_load_by_slot`:

1. Uses measured `base_load_w` when present.
2. Retains ordinary legacy `load_w`, but skips unclassified legacy readings over
   6 kW instead of guessing how much was EV load.
3. Excludes samples taken during heavy battery activity, as before.
4. Reduces multiple optimizer/replan samples in the same day and 15-minute slot
   to one daily median so replans cannot overweight a day.
5. Applies the median/MAD high-outlier guard across daily slot values.

The PV learner retains its original plain-mean behavior; load-specific anomaly
handling is not applied to PV.

## Current display convention

The shared MQTT current and Vehicle tab remain the ABB meter's per-phase average.
Only the EV card in `powerflow.js` displays the requested total-current convention:
it sums the ABB meter's three physical `Ac/L{1,2,3}/Current` readings. At the
meter's normal idle draw (100 W or less), the card displays 0 A so retained phase
notifications cannot look like active charging.

## Migration behavior

No backfill command exists and no NDJSON/Parquet files are modified. The learner
uses the conservative legacy rules immediately. Over the following three days,
quality-labelled measured base-load records naturally replace the legacy window.

Measured base load and EV energy are the inputs for the future smart-charge work
described in `TODO.md`.
