# TODO / roadmap

- **Weather forecast validation / apply tuning** — Open-Meteo fetch/cache, Weather tab,
  history recording, and the HVAC/PV apply gates are implemented. Production `.env`
  may enable `HVAC_LOAD_APPLY` / `PV_WEATHER_APPLY`, and the current plan confirms
  they change forecasts (`hvac_apply=True`, `pv_apply=True`). Do **not** mark this
  effective yet: the first partial 2026-06-28 sample only has afternoon/evening
  weather rows and the load counterfactual was slightly worse with weather
  (weather MAE ~0.240 kWh vs base ~0.227 kWh on non-trivial load rows). Keep
  collecting data, fit/tune `HVAC_ALPHA_COOL` / `HVAC_ALPHA_HEAT`, and only call the
  apply phase done after multiple full days show lower forecast error.

- **Tesla EV charger — monitor now, control later.** The current Tesla Wall Connector
  (**v1, non-controllable**) is monitor-only: we read its power (`evcharger/42` /
  Domoticz `ev_power`) but cannot command it. A later iteration controls charging on the
  **2018 Model S 100D** via the **Tesla Fleet API** (NOT the legacy Owner API) — which
  first requires standing up a Tesla developer **OAuth app** (registered partner app,
  OAuth 2.0 + partner-authentication token, signed vehicle commands). Only once that's in
  place can the optimizer schedule EV charging into the cheapest slots and feed the
  planned session into the load forecast. EV is **out of scope** for the weather story.

## Dashboard / UX

- ✅ **Best daily settlement + economic grid charging — done.** The optimizer now
  compares today-first settlement against the full horizon and only sacrifices today
  for clearly exceptional future upside. Grid charging is selected by modeled path
  economics, not a user-facing hard price cap; `ESS_MAX_GRID_CHARGE_SOC` only limits
  forced grid-charge target SoC while PV can still top up above it.

- ✅ **Projected Today profit point — done.** The "Daily net — month so far" chart now
  keeps today's settled-so-far point and adds a hollow projected full-day marker.

- ✅ **Weather forecast shadow mode — done.** Open-Meteo fetch/cache, degree-day HVAC
  shadow load, GTI-shaped PV shadow, Weather tab charts, and weather history tracking
  are implemented without changing optimizer control by default.

- ✅ **PV nowcast + Solar card alignment — done.** The optimizer now corrects today's
  near-term PV forecast from live PV power and the latest settled PV slot, faded by
  GTI/sunset shape. The Overview Solar card shows that adjusted remaining PV as the
  main number and explicitly labels the original source as `VRM forecast` in subtext.

- ✅ **Power-flow v2 — done.** Mashed up the two references the operator liked:
  **HASS Energy-distribution pathing** (perpendicular-exit + quarter-turn Bézier
  connectors, source-coloured dots) around **Victron GUI-v2 rich info cards**
  (Grid/AC-Loads per-phase L1/L2/L3, Battery temp·V·A·SoC·time-to-go, EV session)
  with a top-centre inverter/charger state pill. New read-only `live.py`
  subscriptions feed the cards (topics mirror `lib/constants.py`). Restart the
  dashboard process to pick up the new subscriptions.

- ✅ **Forecast-accuracy overlay — done.** Trends now includes an actual-vs-forecast
  PV/load overlay between the SoC/price and monthly-net charts, using settlement
  rows and cycle-derived actual load where older rows need it.

- ✅ **Uniform chart toggles + running ledger — done.** SoC/price, forecast accuracy,
  Weather forecast, and Forecast impact charts now use clickable legends to hide/show
  series. The schedule timeline also has a moving today-so-far ledger row above the
  current hour.

- ✅ **Runtime override controls — done.** Desktop footer and mobile hamburger menu
  expose Override (AI ESS stands down after idling Victron once) and Grid assist
  (existing manual retain/grid-assist path via `grid_charging_enabled`).

- ✅ **Mobile (iPhone 12 Pro) UX pass — done.** Main navigation, Overview/ESS
  reorganization, sticky mobile subheader, bottom controls, external-frame scaling,
  and hidden-scrollbar-but-scrollable Battery/Venus panes are implemented.

## AI Advisor

- ✅ **Phase 1 — Advisor tab (done).** Manually-triggered, read-only, streaming
  plain-language review of recent history + the tunable subset, plus persisted
  newest-first chat, follow-up questions, copy/delete exchange controls, markdown
  rendering, and on-demand history retrieval. Authenticates via a subscription-login
  CLI (`ADVISOR_CLI_CMD` → Claude Code / Gemini / Codex), never a metered API key.

- Phase 2 — approve-to-apply for tunables only. Each setting has hard min/max bounds;
  on approval the system runs a dry-run backtest, shows projected EUR, writes .env
  (hot-reloads, no restart), and auto-reverts if the next day underperforms. Bounded
  numbers can't crash the controller — this is the safe sweet spot.

- Phase 3 — code changes via PR, not hot-patch. Let the model propose a diff + tests;
  the "apply" button opens a PR/branch for human review and your normal pytest gate.
  Keep a human on the actual diff before anything restarts a 16 kW controller.
