CerbomoticzGx
========================
## Introduction 
Do you have one or more of the following devices in your home?

- [x] Solar panels (the more the better)
- [x] A Tesla vehicle (optional)
- [x] A Tibber energy contract with a Pulse device (optional)
- [x] Victron Energy Equipment (Cerbo GX compatible Inverters/Chargers, MPPT charge controllers, etc.)
- [x] Home Energy Storage System with canbus or serial control and working well with your Victron system
- [x] an ABB B21/23/24 Kilowatt meter (optional)
- [x] a Domoticz based Home Automation system (optional)
- [x] HomeConnect enabled smart appliances (optional and currently requires you to run [https://github.com/hcpy2-0/hcpy](https://github.com/hcpy2-0/hcpy) as an additional service.) 

If so, this project might be something you will find interesting. Have a look at what this project offers by 
reading more below.  Also, many of the features in this project are visualized and controllable
through the built-in Flask dashboard in `frontend/`.

## Features
This project is a series of modules which aim to integrate, automate and control the following systems and components.

- Victron Energy Equipment (Cerbo GX controlled Inverters, Solar MPPT charge controllers, etc.)
- Victron compatible LFP based Energy Storage Systems
- Tesla Electric Vehicles
- Tibber Smart Energy Supplier (hourly spot rate electricity supplier) API integration
- ABB B21/23/24 kWh meters
- Domoticz Home Automation System 


Current Features include:
- monitors a number of metrics from a Victron Energy CerboGX controlled system and reports these metrics back to
a Domoticz server via its REST API for monitoring and historic tracking
- Modular - Individual modules can be enabled or disabled in the ```.env``` file    
- Included a custom module which can be installed on a cerbo gx to read out ABB B2x kWh meters
- EV Charge Controller - Tesla vehicle charging at lowest rates or using only excess solar energy
- Grid Assisted vehicle charging mode for when you need to just charge at full rate regardless of cost
- Energy Broker module which attempts to buy energy at the lowest possible rate in a 48 hour period and store this in your home battery
- Tibber graphing module to generate visuals of the upcoming electricity prices (Thanks to [Tibberios](https://github.com/Lef-F/tibberios))
- Tibber API integration to constantly monitor current energy rates, daily consumption and production, forecasted pricing, etc (Thanks to [Tibber.py](https://github.com/BeatsuDev/tibber.py))
- deep integration with Victron system for monitoring and control via the cerbo Gx MQTT broker
- Creates, exports, and updates a number of custom metrics to the victron MQTT broker for consumption by the [venus-nextgen Energy Dashboard](https://github.com/JoshuaDodds/venus-nextgen)
- dynamic ESS algorithms for automated buy and sell of energy
- solar forecasting data specific to your installation using ML models and AI for quite accurate current day production forecasts (courtesy of new VRM API features developed by Victron Energy). Note: A Victron VRM portal account is needed for this feature.
- **AI Powered ESS Optimization**: A feature-flagged module that optimizes battery charging and discharging schedules using a dynamic-programming search over battery state-of-charge. It plans across the full available Tibber price horizon (today, and tomorrow once day-ahead prices publish around 13:00), accounts for charge/discharge efficiency, PV and load forecasts, seasonal SoC reserves, battery wear, export economics, and stored-energy cost basis. Its primary policy is **best daily settlement**: make today's result as profitable as possible, or reduce unavoidable cost as close to zero as possible, while allowing tomorrow-first carryover only when the future upside is clearly exceptional relative to learned history and forecast risk. Grid charging is chosen by path economics instead of a hard price-cap knob. When the current price is negative it also limits Victron grid feed-in to 0W (auto-reverting when prices recover).
- HomeConnect supported appliance control. Schedules appliances to run at cheapest time of day without user intervention
- **Web dashboard** (`frontend/`): a self-contained operator dashboard (Flask). It shows the Overview entry point, ESS tabs, current decision, expandable hour->15-min->reasoning schedule tree (with a collapsed **previous-day settled** view and a moving today-so-far ledger row), a **live power-flow** diagram, **Trends** (toggleable SoC/price chart, actual-vs-forecast PV/load overlay, and monthly net chart with projected-today marker), a desktop **Weather** tab with toggleable chart series, sticky header status chips, guarded Victron Schedule clearing, Replan/Restart/Override/Grid assist operator buttons, and allow-listed `.env` config editing. Runs as its own process/sidecar (`python -m frontend`) or an optional in-process thread. Set `APP_ENV_PATH` when the writable `.env` lives outside the app working directory. See `frontend/README.md`.
- **Weather shadow mode** (`lib/weather.py` + dashboard Weather tab): fetches keyless Open-Meteo forecasts using `HOME_ADDRESS_LAT` / `HOME_ADDRESS_LONG`, caches them in `data/weather/`, computes symmetric heating/cooling degree-day load adjustments and GTI-shaped PV shadow forecasts, and records the summary for analysis. `HVAC_LOAD_APPLY` and `PV_WEATHER_APPLY` default to `False`; validate before enabling.
- **AI Advisor** (dashboard "Advisor" tab): a manually-triggered, **read-only** AI review of recent optimizer behaviour. Run the default review or ask an open question ("Why did we sell at 15:00 yesterday?"); it streams a short, plain-language report and can pull deeper days from history on demand. It analyses recent history + the allow-listed tunables (never secrets) and recommends improvements (existing-tunable / new-tunable / code change), and it cannot change anything. Auth uses a **subscription-login CLI** — set `ADVISOR_CLI_CMD` to your provider's CLI (`gemini -p {prompt}`, `codex exec {prompt}`, or Claude Code) so usage draws from your existing plan; a `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` path also exists. (Phase 1 of a planned advise→approve→apply workflow.)

Configuration for your CerboGX IP Address, VRM instance ID, and Domoticz IP/Port are configured in 
the ```.env``` configuration file. 

Note: The name of this project is a nod to both Victron Energy & the Domoticz project.


### Installation
```pip install -r requirements.txt```

### Configuration / Setup
- Read the ```.env.example``` file carefully and adjust as needed. Rename to ```.env```
- Do the same for ```.secrets-example``` and rename to ```.secrets```
- If you rely on Tibber live measurements, set `HOME_ID` in `.secrets` to the home that has real-time data enabled.
- If Tibber live measurements report that real-time consumption is disabled even though the developer portal works,
  set `TIBBER_LIVE_MEASUREMENTS_FORCE=1` in `.env` to attempt a direct websocket subscription.
- Carefully read through lib/contstants.py and adjust to fit your situation. Most logic is event driven and events topics that drive logic are
  defined here in this file
- Homeconnect support is defined in constants as well but requires an external service to publish state. (see hcpy project mentioned in the introduction of this doc)
- Configure the nightly charging skip guardrails if desired: `NIGHT_CHARGE_SKIP_ENABLED` toggles the behaviour and `NIGHT_CHARGE_SKIP_MIN_SOC` / `NIGHT_CHARGE_SKIP_MAX_SOC` bound the state-of-charge window that will skip the 21:30 schedule run.
- **AI Optimization Configuration**:
  - `AI_POWERED_ESS_ALGORITHM=True`: Enable the new AI optimizer.
  - `BATTERY_CAPACITY_KWH`: Your battery capacity in kWh (default 42.0).
  - `AC_DC_CHARGE_EFFICIENCY`: Efficiency of charging (e.g. 0.90).
  - `AC_DC_DISCHARGE_EFFICIENCY`: Efficiency of discharging (e.g. 0.90).
  - `MIN_SOC_RESERVE_WINTER` / `MIN_SOC_RESERVE_SUMMER`: Minimum SoC reserve (%) the optimizer always keeps (defaults 20 / 5).
  - `OPTIMIZER_SOC_STEP_PCT`: DP SoC discretization step in percentage points (default 1.0; smaller = finer control, more compute).
  - `ESS_MAX_GRID_IMPORT_KW` / `ESS_MAX_GRID_EXPORT_KW`: Grid power limits (kW) for the optimizer's feasibility checks.
  - `ESS_MAX_CHARGE_KW` / `ESS_MAX_DISCHARGE_KW`: Optional battery power caps (default to the grid limits).
  - `ESS_MAX_GRID_CHARGE_SOC`: Maximum SoC the optimizer may target with forced grid charging; PV surplus can still charge above it. There is intentionally no user-facing grid-charge price cap: the optimizer evaluates the full path economics instead.
  - `ESS_MODEL_CHARGE_RATE`: Report charging as full-power-to-target (matching the Victron scheduled-charge behaviour) so BUY settlement predictions/economics match reality. Reporting only — control is unchanged. 1 = on (default), 0 = report the raw DP trajectory.
  - `ESS_EXPORT_PRICE_FACTOR` / `ESS_EXPORT_FEE`: Export price model — `sell = buy * factor - fee` (defaults 1.0 / 0.0).
  - `ESS_TERMINAL_VALUE_FACTOR`: Value of end-of-horizon stored energy on multi-day horizons as a multiple of the horizon mean buy price (default 1.0; 0.0 disables; same-day-only horizons ignore it so late Tibber next-day prices do not cause evening over-retain).
  - `ESS_EXPECTED_PEAK_PRICE`: Expected peak buy price (currency/kWh). When set, end-of-horizon stored energy is valued at the higher of the horizon mean and this peak, so charge is held for the typical morning/evening peaks (0 disables).
  - `ESS_MIN_SELL_PRICE`: Hard floor below which the battery is never actively discharged to the grid (PV-surplus feed-in still allowed; 0 disables).
  - `ESS_BATTERY_CYCLE_COST`: Wear cost per kWh discharged; discourages cycling the battery for marginal arbitrage (~0.03–0.06 typical; 0 disables).
  - `ESS_ARBITRAGE_MARGIN`: Additional per-kWh profit cushion on top of battery wear cost; prunes thin-spread cycles that are fragile to forecast error.
  - `ESS_COST_BASIS_PATH`: Path for persisted stored-energy cost basis. Grid charging raises the basis, PV charging dilutes it, and the optimizer will not actively sell stored energy below its effective cost.
  - `ESS_SELL_MIN_DWELL_MIN` / `ESS_SELL_HYSTERESIS_EUR`: SELL anti-flap guardrails that only suppress marginal re-entry after a recent SELL stop.
  - Solar is forecast for **both today and tomorrow** (VRM `solar_yield_forecast`), so day-2 charging plans around expected solar instead of assuming zero.
  - **Intraday PV self-correction**: the per-slot PV forecast is a daily magnitude distributed by a learned **daylight-only** shape (`ESS_PV_SHAPE_DAYS`, default 3). Because VRM anchors the magnitude to a fixed daily total, a better-than-forecast day's "remaining" would otherwise collapse toward zero; `ESS_PV_INTRADAY_CORRECTION` (0-1 damping, default 0.6; 0 disables) scales today's remaining **up** toward a projection from actual production so far, capped by `ESS_PV_INTRADAY_MAX_RATIO` (default 1.6) and only engaged once `ESS_PV_INTRADAY_MIN_ELAPSED` (default 0.10) of the day's solar has elapsed.
  - **PV nowcast correction**: after the baseline VRM/history/weather forecast is built, the optimizer anchors the next few current-day slots to live PV power and the latest settled PV slot, then fades that correction by Open-Meteo GTI/sunset shape. This fixes late-day cases where VRM's remaining forecast has already collapsed to zero even though the array is still producing. The dashboard solar card shows the adjusted remaining PV as the main value and keeps the original `VRM forecast` value as source-labelled subtext.
  - **Weather shadow mode**: `WEATHER_ENABLED=True` fetches Open-Meteo forecasts without an API key. `HVAC_*` settings compute heating/cooling degree-day load adjustments and `PV_WEATHER_*` settings compute GTI-shaped PV shadow forecasts. Keep `HVAC_LOAD_APPLY=False` and `PV_WEATHER_APPLY=False` until enough recorded history proves the adjustments reduce forecast error.
  - `OPTIMIZER_SLOT_MINUTES`: Planning resolution (default 15). Sub-divides hourly Tibber prices and auto-uses finer native data when available.
  - `TIBBER_PRICE_RESOLUTION`: `QUARTER_HOURLY` (default) pulls true 15-minute prices via a direct Tibber GraphQL query (how Tibber bills as of Oct 2025); `HOURLY` requests hourly. Transient failures are retried, then the last cached quarter-hour horizon is used before degrading to hourly.
  - `TIBBER_PRICE_CACHE_PATH`: Optional base path for cached price horizons (default `/dev/shm/cerbo_tibber_price_cache.json`; resolution suffix is appended). Keeps the optimizer on last-good quarter-hour prices through short Tibber/API outages.
  - `LOAD_PROFILE_HOURLY`: Optional 24-value house-load shape for self-consumption forecasting. The daily total comes from the VRM consumption forecast (or measured-so-far, or `DAILY_HOME_ENERGY_CONSUMPTION`) and is distributed across slots so SoC predictions account for self-usage (notably the evening peak).
  - `NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED=True`: Limit Victron system feed-in to 0W while the current price is negative, auto-reverting to unlimited afterward.
  - The optimizer runs every 15 minutes and again at 13:05 (after next-day Tibber prices publish) to plan over the available horizon. It compares a full-horizon plan with a today-first settlement plan and exports the selected policy in `planning_policy`. Each run classifies the current slot into one of four control actions — **IDLE**, **RETAIN**, **BUY**, or **SELL** — with a plain-English `Reason` and a machine-readable `reason_code` (also published to state as `ai_mode`/`ai_reason`). Inspect it without applying anything via `python scripts/ai_ess_dryrun.py`.
- IMPORTANT:  See notes below if you plan to run this from a container image.  My image won't work for you as is. Read the notes below
for the things you will need to adjust in your own fork of this repo.
 
For Kubernetes or any deployment that mounts `.env` outside the working directory,
mount the containing directory read/write and set `APP_ENV_PATH` to the writable
file. Avoid mounting `/app/.env` as a single read-only file if you want dashboard
config edits, because the writer uses atomic replace semantics.


### Running from CLI
```python3 main.py```

### Docker Container
If you will be building and running this from a container you will want to fork this repo and make sure you set up your configuration 
to match your wishes and your own system.

Check the entrypoint.sh  for the container. You will need to adjust how you handle secrets & gitops configuration injection for the container.

Finally, use the build.sh script as a template for building an arm64 image and pushing it to a container repository.

---------------
(This package is in its infancy, but contributions and collaborations are welcome.)

Copyright 2022, 2023, 2024, 2025, 2026 Joshua Dodds - All Rights Reserved.
