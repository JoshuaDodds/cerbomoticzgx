# Weather-forecast integration — shadow-mode status and apply plan

> User story / spec for a coding agent. Scope: improve the optimizer's **load** and
> **PV** forecasts with weather data. **EV is explicitly out of scope** here (it needs
> Tesla Fleet-API control first — see `TODO.md`).

## Current status on this branch

Implemented in shadow mode:

- `lib/weather.py` fetches keyless Open-Meteo forecasts using
  `HOME_ADDRESS_LAT` / `HOME_ADDRESS_LONG` from `.secrets`.
- Forecast snapshots are cached in `data/weather/latest.json`, and compact weather
  summaries are appended to `data/weather/weather.ndjson` for later correlation.
- The dashboard has a desktop Weather tab (`GET /api/weather`) with temperature,
  cloud/irradiance, and shadow impact charts.
- The optimizer logs a weather shadow context. By default the HVAC and GTI weather
  apply gates remain off (`HVAC_LOAD_APPLY=False`, `PV_WEATHER_APPLY=False`), but
  the separate **PV nowcast** layer always reconciles today's near-term slots with
  live PV power and recent settled PV when the raw VRM remaining value has drifted.
- `.env.example`, `frontend/config_schema.py`, and `README.md` document the weather
  knobs.

Still pending:

- Validate 1-2 weeks of weather shadow output against actual load/PV history.
- Fit or tune `HVAC_ALPHA_COOL` / `HVAC_ALPHA_HEAT` from observed degree-day error
  instead of trusting the defaults blindly.
- Enable `HVAC_LOAD_APPLY` and/or `PV_WEATHER_APPLY` only after shadow validation
  shows lower forecast error.

## Why

The VRM-based load forecast extrapolates from trailing history, so it **lags
temperature-driven HVAC swings by 3–5 days**. During the late-June 2026 omega-block
heat wave the load forecast underestimated by ~24 % on the hot days (Jun 22: +24 %,
Jun 25: +24 %, Jun 24: +12 %) while quiet days landed within ±4 %. That clustering on
hot days is the signature of the **four Daikin split heat-pump units** holding the
house at 21–24 °C — a large, *forecastable*, physically-causal load the VRM model
can't see. Temperature is the high-value lever.

Secondary: a cloud/irradiance forecast sharpens **next-day** PV. Same-day PV misses
are handled by the intraday correction plus the newer PV nowcast layer, so weather PV
is lower priority for same-day control than HVAC.

## Data source — Open-Meteo (chosen over OpenWeatherMap)

- **No credentials, no sign-up, no card.** Open-Meteo is free for non-commercial /
  home-automation use; limits (~10k calls/day, 5k/h, 600/min) are far above ours.
  → **The user does NOT need to register or obtain a key.**
- **Richer PV data than OWM:** the solar/forecast API serves **GTI (global tilted
  irradiance for our exact panel tilt/azimuth)**, GHI, DNI, and diffuse — all in W/m² —
  vs OWM's coarse cloud-% / uvi proxies.
- **15-minute resolution for Central Europe** (incl. NL) — a direct match to our 15-min
  slot grid (interpolate only outside CE). Hourly 7–16-day horizon covers the Tibber
  window.
- Inputs required: home **lat/lon** — **already in `.secrets`** as `HOME_ADDRESS_LAT` /
  `HOME_ADDRESS_LONG` — plus panel **tilt** and **azimuth** (new tunables).
- Keep the provider behind a **source-agnostic interface** (`WeatherProvider`) so the
  existing OWM key (still in `.secrets`) or Solcast/KNMI can be added as alternates
  later without touching the optimizer.

Endpoint shape (no key):
`https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..&minutely_15=temperature_2m,global_tilted_irradiance,direct_normal_irradiance,diffuse_radiation,cloud_cover&tilt=..&azimuth=..&forecast_days=2&timezone=Europe/Amsterdam`

## Config

`.secrets`: **nothing new required.** Open-Meteo is keyless, and your home coordinates
are already present as `HOME_ADDRESS_LAT` / `HOME_ADDRESS_LONG`. Leave the existing
`OPENWEATHERMAP` key in place as an optional fallback provider only.

`.env` + `config_schema.py` (implemented tunables, documented inline + in README):

| Key | Default | Purpose |
|---|---|---|
| `WEATHER_ENABLED` | `True` | master switch for the whole feature |
| `WEATHER_PROVIDER` | `open-meteo` | provider id (source-agnostic) |
| `HOME_ADDRESS_LAT`, `HOME_ADDRESS_LONG` | *(already in `.secrets`)* | home coordinates — reuse the existing keys |
| `PV_PANEL_TILT`, `PV_PANEL_AZIMUTH` | — | for GTI (Open-Meteo azimuth convention) |
| `WEATHER_FETCH_TTL_MIN` | `30` | cache TTL; forecasts only refresh ~hourly |
| `HVAC_LOAD_ENABLED` | `True` | enable the temperature→load adjustment |
| `HVAC_T_COMFORT_LOW` / `HVAC_T_COMFORT_HIGH` | `21` / `24` | comfort band edges (°C) |
| `HVAC_ALPHA_COOL` / `HVAC_ALPHA_HEAT` | fitted | kWh per cooling/heating degree-°C-day |
| `HVAC_LOAD_MAX_DELTA_KWH` | `15` | safety cap on the daily load delta |
| `HVAC_LOAD_APPLY` | `False` | **shadow gate** — compute+log only until validated |
| `PV_WEATHER_ENABLED` | `True` | enable the irradiance→PV adjustment |
| `PV_WEATHER_BLEND` | `0.5` | blend weight: VRM-shape vs GTI-derived PV |
| `PV_WEATHER_APPLY` | `False` | **shadow gate** for the PV adjustment |

## Architecture

- Implemented module `lib/weather.py`: `WeatherProvider` interface + `OpenMeteoProvider`.
  `fetch(lat, lon, tilt, azimuth, horizon) -> list[{ts, temp_c, gti_wm2, ghi_wm2,
  dni_wm2, diffuse_wm2, cloud_pct}]`, cached with `WEATHER_FETCH_TTL_MIN`.
- **Off the critical path** (AGENTS rule): refresh on a background thread / cached read;
  the optimizer only ever reads the last good snapshot. A fetch must never block or
  crash the 15-min planning cycle.
- **Fallback:** on any weather fetch/parse error, return `None`; the optimizer proceeds
  from the VRM/history baseline forecast and any measured nowcast data still available
  locally. Weather can only improve or be skipped; it must never break the cycle.

## HVAC load adjustment (the high-value piece) — symmetric degree-day model

The house is **heated *and* cooled** to a 21–24 °C band, so the model must be symmetric
(cooling-above and heating-below), not cooling-only:

```
CDD = Σ_slots max(0, T_slot − HVAC_T_COMFORT_HIGH)        # cooling degree-°C
HDD = Σ_slots max(0, HVAC_T_COMFORT_LOW − T_slot)         # heating degree-°C
Δload_kWh(day) = HVAC_ALPHA_COOL·CDD + HVAC_ALPHA_HEAT·HDD
```

- Distribute `Δload_kWh` across slots weighted by the day's load shape (or
  proportionally to the VRM slot load), then add to the VRM slot load forecast.
- **Next validation step:** fit `α_cool`, `α_heat` by linear regression over the last *N* completed days of
  `(CDD, HDD, load_actual − load_baseline)`; until enough history exists, use the
  tunable defaults (manual override). Refit on a rolling 14-day window.
- **Cap** `|Δload| ≤ HVAC_LOAD_MAX_DELTA_KWH` per day to prevent runaway adjustments
  while the model warms up.
- Requires daily temperature in the records to build the training set (see Tracking).

## PV forecast adjustment (irradiance-based) — lower priority

- Applied **after** the VRM daily total is distributed by the learned daylight shape
  (`ESS_PV_SHAPE_DAYS`) and **before** intraday correction (`ESS_PV_INTRADAY_CORRECTION`):
  `pv_adj = (1 − B)·pv_vrm + B·pv_gti`, where `pv_gti` comes from GTI × panel kWp ×
  performance ratio (or `GTI_slot / GTI_clearsky_ref · pv_vrm`), `B = PV_WEATHER_BLEND`.
- Use **GTI/irradiance, not cloud-%** (cloud-% → PV is crude and nonlinear).
- Clamp to a sane `[0, max]`; log the per-slot adjustment ratio.
- Note: intraday self-correction plus the live/settled PV nowcast now handles most
  *same-day* PV misses. The real weather-PV win is **next-day** planning, hence lower
  priority than HVAC.

## Same-day PV nowcast (implemented outside the weather apply gate)

The current branch also adds a non-tunable near-term PV nowcast in
`lib/energy_broker.py`:

- `_latest_settled_pv_slot_kwh()` reads the newest complete settlement row for today,
  rejects stale/incomplete slots, and normalizes it to the optimizer slot duration.
- `_pv_nowcast_anchor_kwh()` blends that settled slot with live `pv_power`; if live PV
  has dropped sharply below the settled slot it treats that as sunset/tree-line
  drop-off instead of extrapolating the earlier high output.
- `_apply_pv_nowcast()` raises only today's near-term PV slots, weighting the live
  anchor by Open-Meteo GTI ratios and fading over the next few hours. Tomorrow's
  forecast is left unchanged.

Why this is separate from `PV_WEATHER_APPLY`: this is not trusting a weather model to
reshape the day; it is correcting the active plan with measured production from the
system itself. The dashboard Solar card therefore shows the adjusted remaining PV as
the main value and the original `VRM forecast` value as source-labelled subtext.

## Forecast-quality tracking (prove it before trusting it)

- Current branch tracking: `data/weather/weather.ndjson` records a compact summary of
  the forecast and shadow adjustment per run, while the existing settlement history
  records predicted-vs-actual PV/load for dashboard and Advisor analysis.
- Future richer tracking: add per-slot weather fields to settlement rows
  (`temp_forecast_c`, `gti_forecast_wm2`, `weather_pv_adj_kwh`,
  `weather_load_adj_kwh`, and, if a temp sensor is available, `temp_actual_c`) so
  forecast improvement can be measured at slot granularity.
- Surface `temp_forecast_c` and the day's irradiance summary in the daily summary so the
  **AI Advisor** can reason about weather (it already flagged the hot-day pattern).

## Shadow-mode validation (it's a 16 kW controller)

1. **Phase 1 — shadow.** `HVAC_LOAD_APPLY=False`, `PV_WEATHER_APPLY=False`: compute and
   **log** the weather adjustments only. The optimizer still starts from the
   VRM/history forecast, then applies the measured PV nowcast for today's near-term
   slots. Run 1-2 weeks and compare forecast-vs-actual error *with vs without* the
   weather adjustment.
2. **Phase 2 — apply.** Once the adjustment demonstrably reduces the error, flip the
   APPLY gates to `True`. Keep the caps + fallback in place.

## Priority / phasing

1. ✅ Provider + fetch + cache + config + summary logging (foundation; low risk).
2. ✅ HVAC symmetric degree-day adjustment in shadow mode (the high-value item).
3. ✅ PV irradiance adjustment in shadow mode.
4. Fit/tune α values and validate forecast error over 1-2 weeks.
5. Enable `HVAC_LOAD_APPLY` and/or `PV_WEATHER_APPLY` only after validation.

## Timing / ROI note

Under net-metering ("saldering", buy = sell until **Jan 2027**) the € value of forecast
accuracy is muted — surplus exports at the price you'd have bought. The value ramps
**sharply** after Jan 2027 (self-consumption, peak-shaving). So build + calibrate **now**:
the α model needs a few weeks of `(degree-day, load)` pairs, so starting early means it's
tuned and trusted by the time it actually pays.

## Out of scope (separate iterations)

- **EV** load/optimization and **Tesla Fleet-API** control of the 2018 Model S 100D
  (OAuth app + partner token + signed vehicle commands) — see `TODO.md`.
- Humidity/wind load terms, a full irradiance→panel physics model, seasonal re-training.
