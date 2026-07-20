# Weather validation and forecast tuning

Branch: `hvac-pv-validation-tuning`

## Purpose

Validate the Open-Meteo HVAC/PV forecast overlays against accumulated settlement
history, correct any unsafe or biased behavior, and leave both apply gates in shadow
mode until repaired forecasts demonstrate lower holdout error.

## Baseline result

The initial model was evaluated over 21 complete days (2026-06-29 through
2026-07-19), covering 1,962 complete measured load slots. Weather-applied load MAE
was 0.2163 kWh/slot versus 0.1155 for the counterfactual baseline, with positive
bias of 0.1504 versus 0.0117 kWh/slot. Weather improved 0 of 21 days.

Confirmed causes:

- the full daily HVAC delta was redistributed over every shrinking remaining-day
  horizon, producing up to 4.638 kWh of weather load in one 15-minute slot;
- absolute heating and cooling demand was added to a trailing-three-day baseline
  that already contained recent HVAC consumption;
- both heating and cooling degrees were counted even though the split units operate
  cooling-only in Summer Mode and heating-only in Winter Mode;
- a conventional compass bearing of 170 degrees was passed to Open-Meteo as 170
  degrees, although the provider expects -10 degrees for that orientation;
- backward-averaged hourly GTI was assigned to the following hour;
- fresh 0 W PV was treated as missing, preserving stale production at sunset; and
- settlement history did not retain the raw PV baseline needed for counterfactual
  validation.

## Implementation

- `PV_PANEL_AZIMUTH` remains a conventional compass bearing (`0=N`, `90=E`,
  `180=S`, `270=W`) and is converted at the Open-Meteo boundary. Provider request
  geometry is fingerprinted so an incompatible cache is refreshed immediately.
- Forecast snapshots request three past days, matching the load forecast's trailing
  history.
- HVAC uses restart-frozen Summer/Winter selection. Summer computes cooling-degree
  anomalies; Winter computes heating-degree anomalies. Each slot is compared with
  the same hour over the prior three days, so a shrinking horizon cannot concentrate
  the adjustment and cooler/warmer conditions can reduce as well as increase load.
- Negative corrections are bounded at zero resulting load and total remaining-day
  corrections retain the configured safety cap.
- GTI is shifted onto the preceding interval represented by Open-Meteo's hourly
  backward average.
- Fresh PV telemetry is timestamped; a fresh 0 W value is accepted as strong drop
  evidence and can collapse a stale sunset forecast.
- Settlements now preserve baseline, weather-shadow, and final load/PV forecasts plus
  both apply flags, enabling valid future counterfactual scoring.
- Live configuration is shadow-only: `HVAC_LOAD_ENABLED=True`,
  `HVAC_LOAD_APPLY=False`, `PV_WEATHER_APPLY=False`, compass azimuth `170`, and a
  conservative history-supported cooling candidate `HVAC_ALPHA_COOL=2.0`.

## Validation status

Historical replay of the repaired cooling-anomaly structure reduced MAE from
0.11399 to 0.11197 kWh/slot at alpha 2.0. On an untouched last-seven-day holdout it
reduced MAE from 0.12802 to 0.12310. This is promising but not sufficient to enable
control: fresh rows with the corrected provider geometry and explicit baseline
fields are still required. No heating coefficient is validated because the
available period is Summer Mode.

Live provider validation confirms compass 170 is sent as Open-Meteo -10, the cache
contains 144 hourly rows (three past plus three forecast days), and today's repaired
cooling anomaly is bounded near zero rather than adding several kWh of heating.

TDD coverage includes provider conversion, three-past-day retention, stale-cache
fail-open behavior, rolling-horizon invariance, Summer cooling/Winter heating
selection, bounded negative adjustments, radiation interval alignment, fresh-zero
sunset handling, and explicit settlement counterfactual fields. The repository-required
suite passes `452 passed in 16.86s` with
`export DEV=1; python -m pytest -s -q`; Python compilation and `git diff --check`
are clean.

The adversarial pass additionally corrected provider-cache invalidation after geometry
changes, retained the optimizer's restart-frozen seasonal selection, made a zero HVAC
safety cap actually disable correction, and caught an internal multi-day summary value
being removed before subsequent forecast days consumed it. A real forced provider
refresh was used after that test/fix loop.

## Apply gate

Do not enable either apply gate on this branch. After multiple complete repaired
days, compare baseline versus shadow MAE and bias separately for load, next-day PV,
and final same-day PV. Enable HVAC and PV independently only if holdout improvement
is material and no late-day concentration or sunset artifact reappears.
