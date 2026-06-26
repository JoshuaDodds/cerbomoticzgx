# Weather-forecast integration — implementation plan

> User story / spec for a coding agent. Scope: improve the optimizer's **load** and
> **PV** forecasts with weather data. **EV is explicitly out of scope** here (it needs
> Tesla Fleet-API control first — see `TODO.md`).

## Why

The VRM-based load forecast extrapolates from trailing history, so it **lags
temperature-driven HVAC swings by 3–5 days**. During the late-June 2026 omega-block
heat wave the load forecast underestimated by ~24 % on the hot days (Jun 22: +24 %,
Jun 25: +24 %, Jun 24: +12 %) while quiet days landed within ±4 %. That clustering on
hot days is the signature of the **four Daikin split heat-pump units** holding the
house at 21–24 °C — a large, *forecastable*, physically-causal load the VRM model
can't see. Temperature is the high-value lever.

Secondary: a cloud/irradiance forecast sharpens **next-day** PV. (Same-day PV misses
are already absorbed by the existing intraday self-correction, so this is lower
priority — see "Priority" below.)

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

`.env` + `config_schema.py` (new tunables, all documented inline + in README):

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

- New module `lib/weather.py`: `WeatherProvider` interface + `OpenMeteoProvider`.
  `fetch(lat, lon, tilt, azimuth, horizon) -> list[{ts, temp_c, gti_wm2, ghi_wm2,
  dni_wm2, diffuse_wm2, cloud_pct}]`, cached with `WEATHER_FETCH_TTL_MIN`.
- **Off the critical path** (AGENTS rule): refresh on a background thread / cached read;
  the optimizer only ever reads the last good snapshot. A fetch must never block or
  crash the 15-min planning cycle.
- **Fallback:** on any fetch/parse error, return `None`; the optimizer proceeds with the
  **unmodified VRM forecast** and logs a warning. The feature can only *improve* the
  forecast, never break the cycle.

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
- **Fit** `α_cool`, `α_heat` by linear regression over the last *N* completed days of
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
- Note: the intraday self-correction already handles most *same-day* PV misses; the real
  win here is **next-day** planning, hence lower priority than HVAC.

## Forecast-quality tracking (prove it before trusting it)

- Add to settlement rows (`lib/energy_broker.py`): `temp_forecast_c`, `gti_forecast_wm2`,
  `weather_pv_adj_kwh`, `weather_load_adj_kwh`, and (if a temp sensor is available)
  `temp_actual_c` — so the existing predicted-vs-actual tracking can measure improvement.
- Surface `temp_forecast_c` and the day's irradiance summary in the daily summary so the
  **AI Advisor** can reason about weather (it already flagged the hot-day pattern).

## Shadow-mode validation (it's a 16 kW controller)

1. **Phase 1 — shadow.** `HVAC_LOAD_APPLY=False`, `PV_WEATHER_APPLY=False`: compute and
   **log** the adjustments only; the optimizer still uses the raw VRM forecast. Run 1–2
   weeks and compare forecast-vs-actual error *with vs without* the adjustment.
2. **Phase 2 — apply.** Once the adjustment demonstrably reduces the error, flip the
   APPLY gates to `True`. Keep the caps + fallback in place.

## Priority / phasing

1. Provider + fetch + cache + config + **daily-temperature logging** (foundation; low risk).
2. **HVAC symmetric degree-day adjustment in shadow mode** + α fitting (the high-value item).
3. Validate, then enable `HVAC_LOAD_APPLY`.
4. PV irradiance adjustment (shadow → apply) — lower priority.

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
