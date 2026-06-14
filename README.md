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
reading more below.  Also, many of the cool features the modules in this project offer are visualized and controllable 
via a by a frontend React based Dashboard which you can find over here: [venus-nextgen Energy Dashboard](https://github.com/JoshuaDodds/venus-nextgen)

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
- **AI Powered ESS Optimization**: A feature-flagged module that optimizes battery charging and discharging schedules using a dynamic-programming search over battery state-of-charge. It plans across the full available Tibber price horizon (today, and tomorrow once day-ahead prices publish around 13:00), accounts for charge/discharge efficiency, PV and load forecasts, seasonal SoC reserves, and a buy/sell price spread, and assigns a terminal value to stored energy so it is not dumped to the grid at the end of the planning window. It re-runs every 15 minutes and again at 13:05 once next-day prices are available, aiming to maximise revenue over the monthly settlement period. When the current price is negative it also limits Victron grid feed-in to 0W (auto-reverting when prices recover).
- HomeConnect supported appliance control. Schedules appliances to run at cheapest time of day without user intervention

Configuration for your CerboGX IP Address, VRM instance ID, and Domoticz IP/Port are configured in 
the ```.env``` configuration file. 

Note: The name of this project is a nod to both Victron Energy & the Domoticz project.


### Installation
```pip install -r requirements.txt```

### Configuration / Setup
- Read the ```.env-example``` file carefully and adjust as needed. Rename to ```.env```
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
  - `BATTERY_CAPACITY_KWH`: Your battery capacity in kWh (default 45.0).
  - `AC_DC_CHARGE_EFFICIENCY`: Efficiency of charging (e.g. 0.90).
  - `AC_DC_DISCHARGE_EFFICIENCY`: Efficiency of discharging (e.g. 0.90).
  - `MIN_SOC_RESERVE_WINTER` / `MIN_SOC_RESERVE_SUMMER`: Minimum SoC reserve (%) the optimizer always keeps (defaults 20 / 5).
  - `OPTIMIZER_SOC_STEP_PCT`: DP SoC discretization step in percentage points (default 5.0; smaller = finer control, more compute).
  - `ESS_MAX_GRID_IMPORT_KW` / `ESS_MAX_GRID_EXPORT_KW`: Grid power limits (kW) for the optimizer's feasibility checks.
  - `ESS_MAX_CHARGE_KW` / `ESS_MAX_DISCHARGE_KW`: Optional battery power caps (default to the grid limits).
  - `ESS_EXPORT_PRICE_FACTOR` / `ESS_EXPORT_FEE`: Export price model — `sell = buy * factor - fee` (defaults 1.0 / 0.0).
  - `ESS_TERMINAL_VALUE_FACTOR`: Value of end-of-horizon stored energy as a multiple of the horizon mean buy price (default 1.0; 0.0 disables).
  - `ESS_EXPECTED_PEAK_PRICE`: Expected peak buy price (currency/kWh). When set, end-of-horizon stored energy is valued at the higher of the horizon mean and this peak, so charge is held for the typical morning/evening peaks (0 disables).
  - `ESS_MIN_SELL_PRICE`: Hard floor below which the battery is never actively discharged to the grid (PV-surplus feed-in still allowed; 0 disables).
  - `OPTIMIZER_SLOT_MINUTES`: Planning resolution (default 15). Sub-divides hourly Tibber prices and auto-uses finer native data when available.
  - `TIBBER_PRICE_RESOLUTION`: `QUARTER_HOURLY` (default) pulls true 15-minute prices via a direct Tibber GraphQL query (how Tibber bills as of Oct 2025); `HOURLY` requests hourly. Auto-falls back to hourly if quarter-hourly is unavailable.
  - `LOAD_PROFILE_HOURLY`: Optional 24-value house-load shape for self-consumption forecasting. The daily total comes from the VRM consumption forecast (or measured-so-far, or `DAILY_HOME_ENERGY_CONSUMPTION`) and is distributed across slots so SoC predictions account for self-usage (notably the evening peak).
  - `NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED=True`: Limit Victron system feed-in to 0W while the current price is negative, auto-reverting to unlimited afterward.
  - The optimizer runs every 15 minutes and again at 13:05 (after next-day Tibber prices publish) to plan over the full 48h horizon. Each run classifies the current slot into one of four modes — **BUY** (store energy into the battery), **SELL** (export to the grid), **HOLD** (preserve SoC; loads covered by grid/PV), or **SELF-SUPPLY** (battery powers the house) — with a plain-English `Reason` and a machine-readable `reason_code` (also published to state as `ai_mode`/`ai_reason`). The full plan is logged each run. Inspect it without applying anything via `python scripts/ai_ess_dryrun.py`.
- IMPORTANT:  See notes below if you plan to run this from a container image.  My image won't work for you as is. Read the notes below
for the things you will need to adjust in your own fork of this repo.
 
**TODO: handle this issue automatically in a universal container build** 


### Running from CLI
```python3 main.py```

### Docker Container
If you will be building and running this from a container you will want to fork this repo and make sure you set up your configuration 
to match your wishes and your own system.

Check the entrypoint.sh  for the container. You will need to adjust how you handle secrets & gitops configuration injection for the container.

Finally, use the build.sh script as a template for building an arm64 image and pushing it to a container repository.

---------------
(This package is in its infancy, but contributions and collaborations are welcome.)

Copyright 2022, 2023, 2024, 2025 Joshua Dodds - All Rights Reserved.
