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
- EV Charge Controller - Tesla vehicle charging at lowest rates or using only excess solar energy.
  While the car is home and plugged in, this controller is authoritative: charging is permitted
  only during an applied smart block, from protected PV surplus, or by the explicit combination
  of Vehicle **Start** plus **Grid assist**. Neither manual flag starts the car by itself, and
  Tesla-app/onboard starts cannot bypass those gates. Vehicle **Stop** is imperative and suppresses
  the current smart block so the controller cannot immediately restart it. Manual/grid starts restore the
  configured full-rate request, bounded by the configured kW and per-phase ceilings, then confirm the pushed
  requested-current state within 60 seconds and continue guarded reconciliation while the
  explicit override remains active. Actual ABB delivery is observed
  but never used to fight Maxem throttling; Maxem's transient
  `ChargeCurrentRequestMax` availability also does not rewrite the durable plan target.
- **Deadline-aware EV smart charging** (off by default): create one target-SoC/ready-by job on
  the Vehicle tab, preview a plain-language daily charge plan, then optionally
  apply it through budget-gated Tesla Fleet commands. A deterministic onboard Tesla schedule
  protects the latest safe start, while Maxem remains authoritative for instantaneous 25 A/phase
  overload throttling. Planned EV energy is added explicitly to both Summer and Winter ESS plans;
  by default the home battery is not discharged into the car. Jobs longer than 48 hours make
  gentle daily progress, but forecast solar may advance that progress when its lost export value
  is cheaper than the later planned energy it replaces. Forecast PV is reserved for the home
  battery up to `MINIMUM_ESS_SOC` before it is offered to the EV; unknown future sources remain
  visibly pending rather than being labelled as grid. Shorter jobs remain deadline-first.
  Live smart control requires Fleet Telemetry and confirms charge limit, requested current, and
  charging state against pushed data. Saving or editing an applied job immediately makes its
  target SoC a controller obligation, independently of the optimizer plan and Tesla fallback
  schedule. The controller retries the limit at 60-second acknowledgement intervals until
  `ChargeLimitSoc` confirms it; confirmed values are not resent. Current/start requests likewise
  remain visibly `at_risk` and continue guarded reconciliation while their intent is active,
  while owned schedule mutations remain bounded. ABB delivery is observed for actual power flow
  but never used to fight Maxem throttling. Every Tesla write records a concise
  `EvCharger [Tesla API]` sent/result line, with pushed confirmation logged separately. A command
  rejected because the vehicle buses are asleep gets a 10-second passive grace and one command
  retry before the controller spends an explicit wake; after that wake, command delivery waits
  for Tesla's 10–60 second connection window instead of retrying immediately. While an
  applied job waits between planned blocks, the established excess-PV path may advance it after the home battery reaches
  `MINIMUM_ESS_SOC`. A solar-only planned block is likewise capped to live surplus rather than
  silently becoming a full-power grid block. Tesla's immediate charge-on-plug, app and onboard
  starts are stopped whenever controller conditions do not authorize charging, or adopted and
  reconciled when they occur during an authorized block. A Tesla-app/onboard stop during an active
  block is likewise reconciled; only this dashboard's **Stop** suppresses that block. Use Vehicle
  **Start** together with **Grid assist** for an intentional immediate grid-backed charge.
  For an applied plan that fits one contiguous charging window on one day, **Run Now** moves
  that window to the present, recalculates its cost/Timeline metadata, enables Grid assist,
  replaces the application-owned Tesla schedule and verifies a configured-ceiling start. The
  same visible window is installed in Tesla, with only its end rounded upward to minute
  precision; multi-window plans show their separate continuous deadline fallback explicitly.
  Once accepted delivery rises above 5 A, the controller releases current regulation to Maxem
  rather than repeatedly asserting the configured ceiling. The same handoff occurs
  when a command was locally blocked/rejected but a newer ABB sample proves the
  requested full-rate ramp happened anyway. On startup, an ordinary authorized
  block that is already physically charging does not install a redundant future
  Tesla start schedule; explicit Run Now retains its requested schedule-replacement
  contract.
  Completion removes that fallback, releases the Victron grid-assist setpoint, restores a
  5 A idle request and deletes the finished job only after those cleanup effects succeed.
  Reaching the requested SoC or passing `ready_by` terminates the job. The controller first
  removes its exact Tesla fallback schedule IDs (including the branch's one legacy ID), then
  deletes the matching local job/plan snapshots and returns the Vehicle UI to idle. Failed
  Tesla cleanup retains the terminal marker for bounded retry instead of hiding an old schedule.
  An accepted stop is observed against the ABB meter for 60 seconds before another stop may be
  sent, preventing duplicate commands during normal charger ramp-down. Fresh ABB power below
  250 W is authoritative charger-idle evidence even if Tesla's change-driven charging flag is
  stale; if the ABB sample is unavailable, the controller falls back conservatively to Tesla.
  Run Now records whether it enabled Grid assist: cancellation releases a module-owned toggle
  immediately, while a Grid assist setting that was already enabled by the user is preserved.
- **Tesla Fleet Telemetry** (`TESLA_TELEMETRY_ENABLED`, off by default): an optional streaming push
  mode where the car reports state via Tesla's Fleet Telemetry instead of REST polling, eliminating
  billable `vehicle_data` reads/wakes for status. `lib/tesla_telemetry_bridge.py` translates the
  stream to the same internal topics/state the REST path uses, so the rest of the system is
  unaffected; falls back to the REST polling path when disabled. The bridge also exposes Tesla's
  connection lifecycle: a disconnected stream is shown on the Vehicle tab, and an explicit
  refresh uses one budget-guarded REST read instead of presenting retained telemetry as fresh.
  Tesla's connectivity `CreatedAt` is preserved as the source-of-truth event time; broker
  arrival time never replaces it. The bridge subscriber and dashboard use instance-unique
  MQTT client IDs, so a development process can overlap the deployed process without either
  losing QoS-0 lifecycle events. Subscriber transport health is tracked separately and command
  acknowledgement fails closed until a source lifecycle event synchronizes a reconnected bridge.
  A live home-to-away location transition clears an otherwise impossible retained home-cable
  plugged/charging state; a later explicit public-charging event remains valid while away.
  Known away or unplugged state keeps no-intent PV-surplus control dormant and makes no Tesla
  call, while the telemetry-disabled mode preserves its rate-limited discovery fallback.
  The bridge counts only live (not retained replay) signals and flushes partial batches during
  disconnect/shutdown. Tesla's developer portal remains authoritative for billing; use
  `scripts/tesla_seed_usage.py` to establish one dated portal baseline. The local all-in estimate
  then includes paid requests and approximate streaming cost when preserving the €0.25 credit
  margin. Tesla's lightweight vehicle-state endpoint is unpriced and is not counted as Data.
  Fleet OAuth uses Tesla's current
  Fleet Auth host and automatically refreshes and atomically persists rotated access/refresh tokens;
  the runtime `.secrets` file must therefore be writable by the controller process.
  Billable requests use burst-safe daily runaway caps (300 commands, 150 data
  reads and 20 wakes by default) plus a per-call $9.75 hard monthly guard against
  Tesla's $10 credit. Safety-critical charge stops and the wake needed to deliver
  them remain exempt from the spend block and are still recorded.
- Energy Broker module which attempts to buy energy at the lowest possible rate in a 48 hour period and store this in your home battery
- Tibber graphing module to generate visuals of the upcoming electricity prices (Thanks to [Tibberios](https://github.com/Lef-F/tibberios))
- Tibber API integration to constantly monitor current energy rates, daily consumption and production, forecasted pricing, etc (Thanks to [Tibber.py](https://github.com/BeatsuDev/tibber.py))
- deep integration with Victron system for monitoring and control via the cerbo Gx MQTT broker
- Creates, exports, and updates a number of custom metrics to the victron MQTT broker for consumption by the [venus-nextgen Energy Dashboard](https://github.com/JoshuaDodds/venus-nextgen)
- dynamic ESS algorithms for automated buy and sell of energy
- solar forecasting data specific to your installation using ML models and AI for quite accurate current day production forecasts (courtesy of new VRM API features developed by Victron Energy). Note: A Victron VRM portal account is needed for this feature.
- **AI Powered ESS Optimization**: A feature-flagged module that optimizes battery charging and discharging schedules using a dynamic-programming search over battery state-of-charge. It plans across the full available Tibber price horizon, accounts for conversion efficiency, PV/load forecasts, battery wear, stored-energy cost and seasonal reserves, and keeps Summer and Winter engines restart-isolated. Summer can optionally compare Trading, PV-first and protected-hybrid constraint sets through the same live engine; Trading is accepted only when its benefit clears a fixed minimum plus learned forecast risk, otherwise the best conservative plan runs. Winter replenishes only enough in low-price windows to protect household coverage and permits routine self-supply above its emergency reserve. Manual Override and an explicit Victron grid outage suppress **writes**, not sensing, settlement or dashboard updates. During an outage the logical 40% winter reserve is released for emergency house use; it is never written as Victron Recharge. The separate `scripts/evaluate_ess_strategies.py` tool remains read-only research and never selects dispatch.
- HomeConnect supported appliance control. Schedules appliances to run at cheapest time of day without user intervention
- **Web dashboard** (`frontend/`): a self-contained operator dashboard (Flask). It shows the Overview entry point, ESS tabs, current decision, a plain-language remaining-day P/L strategy, expandable hour->15-min->reasoning schedule tree (with a collapsed **previous-day settled** view and a moving today-so-far ledger row), a **live power-flow** diagram, **Trends** (toggleable SoC/price chart, actual-vs-forecast PV/load overlay, and daily final-net forecast spread with settled/latest-projection dots), a desktop **Weather** tab with toggleable chart series, sticky header status chips and a stale-backend **Server Offline** banner, guarded Victron Schedule clearing, Replan/Restart/Override/Grid assist operator buttons, and allow-listed `.env` config editing. Runs as its own process/sidecar (`python -m frontend`) or an optional in-process thread. Set `APP_ENV_PATH` when the writable `.env` lives outside the app working directory. See `frontend/README.md`.
- **Weather shadow mode** (`lib/weather.py` + dashboard Weather tab): fetches keyless Open-Meteo forecasts using `HOME_ADDRESS_LAT` / `HOME_ADDRESS_LONG`, caches them in `data/weather/`, computes Summer cooling or Winter heating anomalies relative to the trailing three-day load baseline, and builds GTI-shaped PV shadow forecasts. Panel azimuth uses a conventional compass bearing (`0=N`, `90=E`, `180=S`, `270=W`) and is converted for the provider. Fair PV comparison records both final branches after the same confidence-qualified live nowcast; a single daylight low/0 W observation is held pending, while a confirmed second source update or fresh near-sunset reading may lower the near-term forecast. `HVAC_LOAD_APPLY` and `PV_WEATHER_APPLY` default to `False`; use `python scripts/validate_forecasts.py --dir data/history` and review its fail-closed evidence gate before enabling either.
- **Daikin ONECTA HVAC integration** (`lib/onecta_monitor.py` + top-level **HVAC** page): optional collection of all configured Daikin units in one request every 20 minutes. It publishes retained `hvac/#` state, stores per-unit and combined today/yesterday heating/cooling kWh, and adds shadow context to optimizer history. Monitoring defaults off (`ONECTA_ENABLED=False`); startup reuses snapshots younger than 19 minutes. The separate `ONECTA_CONTROL_ENABLED=False` gate leaves unit cards visible with disabled controls and, when explicitly enabled, permits only capability-advertised, validated manual commands with delayed confirmation.
- **AI Advisor** (dashboard "Advisor" tab): a manually-triggered, **read-only** AI review of optimizer behaviour. Daily review uses a deterministic live/plan/performance evidence pack; open questions maintain bounded conversation memory and may make multiple capped retrievals from history, the in-process log buffer, allow-listed source/documentation excerpts, live state, and explicitly named runtime JSON artifacts. Every retrieval reports its source, freshness, and truncation, while secret/environment files, arbitrary paths, shell access, writes, network tools, and control operations remain unavailable. The built-in Claude CLI path disables tools, MCP, customizations, and session persistence; API authentication is also supported. `ADVISOR_CLI_CMD` is reserved for an operator-audited **text-only wrapper** named in `ADVISOR_CLI_SAFE_EXECUTABLES`—raw agentic Claude, Gemini, and Codex commands are rejected. The Advisor recommends changes but cannot apply them. The tab also hosts two explicitly-triggered read-only evidence panels — forecast validation and the ESS strategy counterfactual — which render the same reports as their CLI counterparts; the strategy panel lists the active plan and every alternative on the identical whole-day basis described below, so its rows are directly comparable with each other and with the Today tile.

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
  - `WINTER_MODE=False`: Select the restart-isolated optimizer policy. `False` preserves summer trading behavior; `True` activates winter self-sufficiency behavior after the supervised restart requested by the dashboard/config watcher.
  - `ESS_ADAPTIVE_POLICY_ENABLED=False`: Summer-only attended-validation gate. When enabled, each replan evaluates Trading, PV-first self-sufficiency and protected hybrid through the production optimizer. Trading must beat the best conservative candidate by `ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR` plus the configured fraction of learned forecast risk; the raw error is first bounded by `ESS_ADAPTIVE_FORECAST_RISK_MAX_EUR`, and common forecast error is not charged a second time in full after lifecycle/arbitrage risk. Protected hybrid must buy enough during the merged low-price valley to cover forecast household demand until the next valley, rather than retaining indefinitely below its own energy target. Strategy dwell/switch-margin settings prevent quarter-hour flapping. `ESS_ADAPTIVE_FORECAST_RISK_FACTOR` applies to household coverage and the capped forecast-risk premium, `ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS` limits both protected continuation and terminal energy credit, and `ESS_ADAPTIVE_POLICY_STATE_PATH` persists the selected policy across restarts. This does not affect the separate Winter engine.
  - `APPLIANCE_OPTIMIZATION_ENABLED=False`: Enable lower-cost appliance start deferral in either Summer or Winter Mode when `HOME_CONNECT_APPLIANCE_SCHEDULING` is enabled. The Home Connect setting remains the master switch, while preferred dishwasher-program enforcement remains active in either season whenever that master switch is enabled. A non-preferred dishwasher run is aborted unconditionally, allowed to return to `Ready`, and then replaced with the preferred program; transient door/remote-start flags do not gate that workflow. Changing this setting requests a supervised restart.
  - `EV_SMART_CHARGE_ENABLED=False` / `EV_SMART_CHARGE_APPLY=False`: separate plan and control gates for one target-SoC/ready-by EV job. Enable planning first and validate the Vehicle-tab schedule before enabling Fleet commands.
  - `EV_CHARGER_MAX_KW` is the requested power ceiling; `EV_EXPECTED_DELIVERY_KW` is the conservative sustained rate used to calculate feasibility/latest-safe-start when Maxem or taper reduces delivery. `EV_CHARGER_MAX_AMPS` accepts 1–25 A/phase (decimal values are floored to Tesla's whole-amp command) and is the durable command ceiling. Fleet Telemetry is read-only: `ChargeCurrentRequest` confirms the sent command, while the last valid change-driven `ChargeCurrentRequestMax` is used only as a conservative live-PV cap. Maxem availability never rewrites grid/smart-plan targets.
    Per-slot EV energy is further capped to forecast grid headroom after house load/PV, so a 16 kW request is never modelled as 16 kW on top of other site demand.
    Once a selected block begins it remains committed through quarter-hour replans. Grid/mixed blocks send the full configured current ceiling and represent a partial final energy allocation as a shorter full-rate interval. Command acceptance is not treated as physical success: current requires a newer matching Fleet signal, while charge start requires a newer Fleet charging edge or local ABB delivery; missing acknowledgement receives guarded retries during the active block. Retained normalized `Tesla/vehicle0/*` state survives application restarts; raw `telemetry/<VIN>/v/*` nulls are receiver-owned change-driven signals and are never rewritten by this service.
  - `EV_CHARGE_BLOCK_START_PENALTY_EUR` is a small virtual optimization penalty per charging block, preventing needless start/stop cycles for tiny price differences without changing the reported electricity cost.
  - `EV_ALLOW_ESS_DISCHARGE=False`: hold stationary-battery SoC flat during planned EV slots so grid/PV supplies the flexible load. Set true only when deliberately allowing the home battery to charge the car.
  - `EV_PV_SURPLUS_REMINDER_ENABLED=True`: send at most one normal-priority Pushover nudge per local day when pushed Fleet state explicitly says the car is home, unplugged, and below `ChargeLimitSoc`; protected live surplus must persist for `EV_PV_SURPLUS_REMINDER_CONFIRM_MINUTES` and the existing ESS forecast must show at least `EV_PV_SURPLUS_REMINDER_FORECAST_MINUTES` of continuous PV remaining after normal house load. The check uses no Tesla API calls and fails closed on unknown vehicle state or a stale/incomplete forecast.
  - `BATTERY_CAPACITY_KWH`: Your battery capacity in kWh (default 42.0).
  - `AC_DC_CHARGE_EFFICIENCY`: Efficiency of charging (e.g. 0.90).
  - `AC_DC_DISCHARGE_EFFICIENCY`: Efficiency of discharging (e.g. 0.90).
  - `MIN_SOC_RESERVE_WINTER` / `MIN_SOC_RESERVE_SUMMER`: Optimizer planning reserve (%) selected explicitly by `WINTER_MODE` (defaults 40 / 5). The winter value is a grid-connected emergency backup buffer, not energy permanently withheld from the house: an explicit Victron grid-offline signal suspends optimizer control and permits the system to discharge below it. These logical floors do not write Victron's hard minimum.
  - `VICTRON_HARDWARE_MIN_SOC`: Independent Victron `MinimumSocLimit` (default 0). Raising it above current SoC triggers Victron Recharge immediately, outside optimizer scheduling. Invalid manual values are rejected without changing the live Victron limit.
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
  - **Override and outage observability:** Manual Override continues the normal price/forecast fetch, optimization, history append, prior-slot settlement and plan publication while issuing no Victron control writes. An explicit `ac_in_connected=0` follows the same observational path with `GRID_OFFLINE_PASS_THROUGH`; missing startup telemetry is treated as unknown, never as an outage.
  - Solar is forecast for **both today and tomorrow** (VRM `solar_yield_forecast`), so day-2 charging plans around expected solar instead of assuming zero.
  - **Intraday PV self-correction**: the per-slot PV forecast is a daily magnitude distributed by a learned **daylight-only** shape (`ESS_PV_SHAPE_DAYS`, default 3). Because VRM anchors the magnitude to a fixed daily total, a better-than-forecast day's "remaining" would otherwise collapse toward zero; `ESS_PV_INTRADAY_CORRECTION` (0-1 damping, default 0.6; 0 disables) scales today's remaining **up** toward a projection from actual production so far, capped by `ESS_PV_INTRADAY_MAX_RATIO` (default 1.6) and only engaged once `ESS_PV_INTRADAY_MIN_ELAPSED` (default 0.10) of the day's solar has elapsed.
  - **PV nowcast correction**: after the baseline VRM/history/weather forecast is built, the optimizer anchors the next few current-day slots to live PV power and the latest settled PV slot, then fades that correction by Open-Meteo GTI/sunset shape. One daylight low/0 W reading is a pending observation; a second distinct fresh source update at least 45 seconds later confirms the bounded downward correction. A fresh near-sunset low/0 W reading is applied immediately. This prevents a transient cloud/telemetry point from collapsing a forecast while still correcting stale sunset output. The dashboard solar card shows the adjusted remaining PV as the main value and keeps the original `VRM forecast` value as source-labelled subtext.
  - **Weather shadow mode**: `WEATHER_ENABLED=True` fetches Open-Meteo forecasts without an API key. Summer Mode evaluates cooling only; Winter Mode evaluates heating only. HVAC corrects temperature anomalies relative to the same trailing three days used by the load forecast instead of double-counting absolute HVAC energy. `PV_PANEL_AZIMUTH` is a conventional compass bearing and is converted to Open-Meteo's south-relative convention. Keep `HVAC_LOAD_APPLY=False` and `PV_WEATHER_APPLY=False` until `python scripts/validate_forecasts.py --dir data/history` shows a reviewed, statistically adequate improvement; passing the report never changes either gate automatically.
- **ESS strategy counterfactual**: `python scripts/evaluate_ess_strategies.py --plan /dev/shm/cerbo_ai_plan.json --json` compares a frozen plan against simplified market-arbitrage, PV-first self-sufficiency, and protected-hybrid candidates. Its human report uses dashboard terminology: **Grid result** is export reward minus import cost; **After battery wear** additionally subtracts estimated battery cycle cost. It reads only the supplied plan/configuration snapshot and never imports the live broker, writes MQTT/Victron, or chooses a dispatch policy. After upgrading, run one optimizer replan first so the exported plan includes its explicit assumptions; an older plan can instead be examined only with the deliberately labelled `--use-research-defaults` option. It is research evidence, not a second controller.
  - **Whole-day comparison basis (report `schema_version` 2).** Every row — including the active plan — is one calendar day, midnight to midnight, matching the dashboard's Today tile: the already-settled part of today (`plan.today_actuals`, identical in every row) plus that policy's planned remainder. Candidates still optimize over the **full** known horizon; only their reported result is limited to today. Truncating the horizon instead would remove the terminal value of retained energy and let every candidate empty the battery by midnight for free.
  - **PV surplus is stored, not sold, while the battery has room.** The optimizer never imposes an export setpoint on PV surplus (see `_post_process` in `lib/ai_powered_ess.py`): the setpoint stays neutral and the Victron stores surplus until the battery cannot accept more. The evaluator enforces this twice — candidates may not plan a non-discharging export while one more SoC step would still fit without importing, and `summarize_steps` settles every row's export by absorbing surplus first, limited by remaining headroom and charge rate. A commanded battery discharge is untouched. `stored_surplus_kwh` reports what a row stored instead of selling. `PV_SURPLUS_FULL_SOC` lives in `lib/forecast_projection.py` so the dashboard and the evaluator cannot drift apart.
  - **`plan_baseline` is the comparison row.** The active plan's own per-slot flows are re-totalled by the same function as the candidates, so the only difference left between rows is the policy. It is withheld (`available: false`) rather than estimated when the published plan lacks `grid_energy`/`soc_start`/`soc_end`; whole-day totals are likewise withheld when the plan carries no `today_actuals`. Do **not** score candidates against the dashboard `day_summary` tile: that number applies different per-slot rules (it suppresses IDLE PV-surplus export revenue and fraction-weights the active slot), and mixing the two bases previously overstated the alternatives by more than €10 by counting tomorrow's revenue as today's.
  - **Approximate Winter-Mode row (opt-in).** When the plan carries `winter_reserve_soc_percent` (published beside `strategy_candidate_config`, never inside it — that mapping validates against a closed field list), a fourth `winter_self_sufficiency` row is added: cheap-window grid replenishment permitted, **no** routine battery-to-grid export, held above `MIN_SOC_RESERVE_WINTER`. That combination is what distinguishes it from `pv_first_self_sufficiency`, which forbids grid charging outright — the very mechanism Winter Mode depends on. Override with `--winter-reserve-soc-percent`, suppress with `--no-winter-candidate`. **It is not the winter engine.** `lib/ai_powered_ess_winter.py` is selected once at startup by `WINTER_MODE` and cannot be previewed from a running Summer plan; the approximation holds a static reserve instead of one sized from forecast household demand to the next replenishment window plus a learned uncertainty margin, never takes the exceptional-spread export the real engine allows, and has no replenishment-window charge scheduling. The report ships these caveats in `winter_candidate.caveats` and both the CLI and the Advisor panel render them next to the figure; keep it that way.
  - **`carried_energy_kwh` / `carried_energy_value_eur`.** A today-only total credits a policy for selling stored energy without debiting the emptier battery it hands to tomorrow. These disclose the usable energy left above each policy's own floor at midnight, valued with the same terminal price the evaluator uses. Read them alongside the whole-day figure: a policy that ends the day flat can outscore one that ends it full purely by borrowing from tomorrow. They are `null` when the plan horizon does not reach tomorrow.
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

### History storage (file-based, daemonless)
Per-cycle history is stored under `HISTORY_DIR` (default `data/history`). The current
month is append-only NDJSON (`ess-YYYY-MM-DD.ndjson`); complete past months roll up into
one immutable ZSTD Parquet each (`ess-YYYY-MM.parquet`) via DuckDB. This keeps the format
robust on network/hostpath (Gluster) storage — no daemon and no mutable DB file to corrupt
— and makes cross-zone migration a plain file copy (`history_store.latest_ts` /
`store_status` identify the freshest zone). All readers go through `lib/history_store.py`,
which serves either format transparently. Roll up cold months with:

EV charging is retained in the same ledger. Cycle rows capture instantaneous
`ev_w` and the ABB meter's `ev_actual_today_kwh`; settlement rows record measured
`ev_charge_kwh`, average charge kW, EV SoC endpoints, meter quality, and an explicitly
labelled proportional attribution of site grid import/cost to the EV. The attributed
cost does not pretend that PV or home-battery energy is free—it reports only the
measured grid cost assigned to the EV's share of simultaneous site load. Completed
EV intervals therefore remain available to the Timeline and Advisor after the live
charge plan advances or the job is removed. Settled Timeline actions use measured
outcomes, never the stored prediction. ABB power start/stop transitions are persisted
as lightweight `ev_charge_transition` rows so new sessions show observed start/stop
times; older data without those events is explicitly shown as a 15-minute measurement
interval rather than an invented exact start time.

```
python scripts/compact_history.py --status     # what's stored, in which format
python scripts/compact_history.py              # compact all complete past months (safe to cron)
```

Compaction requires the `duckdb` package; without it the store stays pure-NDJSON.

### Docker Container
If you will be building and running this from a container you will want to fork this repo and make sure you set up your configuration 
to match your wishes and your own system.

Check the entrypoint.sh  for the container. You will need to adjust how you handle secrets & gitops configuration injection for the container.

Finally, use the build.sh script as a template for building an arm64 image and pushing it to a container repository.

---------------
(This package is in its infancy, but contributions and collaborations are welcome.)

Copyright 2022, 2023, 2024, 2025, 2026 Joshua Dodds - All Rights Reserved.
