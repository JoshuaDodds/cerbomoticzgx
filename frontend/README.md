# cerbomoticzGx Dashboard (`frontend/`)

A self-contained web dashboard for visibility into the ESS service, with a small
set of explicit operator actions.
It shows the current decision, the full optimizer schedule (expandable hour → 15-min
→ reasoning tree, including a collapsed **previous-day settled** view), a live
power-flow diagram, day/month cost summaries, a Tibber-sourced **month-to-date
profit** chip, a **Trends** view (SoC/price + forecast accuracy + monthly net), a
desktop **Weather** view, an **AI Advisor** that
reviews recent performance on demand, and allow-listed `.env` config editing.
The only direct Victron control action exposed here is the guarded **Victron
Schedule** clear button, which disables the five scheduled-charge slots; config
edits go to `.env`; **Restart** publishes the existing supervised restart MQTT
message (`Cerbomoticzgx/system/shutdown=True`) and does not kill the process from
HTTP; the advisor only reads history and shells out to a local subscription CLI.

## Architecture / separation of concerns

```
frontend/
  __main__.py        # python -m frontend
  server.py          # Flask routes + run()/run_in_thread()
  data.py            # plan/history/.env readers, hour grouping, day/month/accuracy summary, Tibber MTD
  live.py            # read-only MQTT subscriber -> live snapshot (SSE source)
  advisor.py         # read-only AI advisor: builds the prompt, shells out to a subscription CLI, streams the review
  config_schema.py   # declarative settings schema (drives the config view + advisor's allow-listed tunables)
  templates/index.html
  static/css/app.css
  static/css/app.mobile.css  # phone-only overrides at <=680px; desktop rules stay untouched
  static/js/app.js         # core render + polling; calls the view modules defensively
  static/js/powerflow.js   # self-contained Live power-flow SVG (window.renderPowerFlow) — VRM-style cards in the Victron topology; responsive (desktop 3-column / phone centre-hub)
  static/js/charts.js      # self-contained SoC+price horizon SVG + monthly net chart
```

The two view modules are loaded before `app.js` and invoked inside `try/catch`, so a
failure in either is isolated and cannot break the core dashboard.

- The **main service** publishes its plan as JSON (atomic write) to
  `AI_PLAN_EXPORT_PATH` (default `/dev/shm/cerbo_ai_plan.json`) on every optimizer
  run, including `planning_policy` metadata for the daily-settlement selector.
  Plan/history/live views only read that file plus `.env`; explicit operator POST
  routes import control helpers inside the request handler.
- Control-action colors: IDLE (grey), RETAIN (amber), BUY (blue), SELL (green).

## Running

Standalone (recommended — own process / container sidecar):

```bash
pip install -r requirements.txt          # adds flask
python -m frontend                        # serves on FRONTEND_HOST:FRONTEND_PORT
```

Optional in-process (daemon thread) from the main service — set `FRONTEND_ENABLED=True`
in `.env` and the main service launches it during `post_startup()`. The launch is
guarded (try/except), so a dashboard failure can never crash the controller:

```python
from frontend.server import run_in_thread
run_in_thread()
```

### Container sidecar

Run a second container/process from the same image with command `python -m frontend`,
sharing the host's `/dev/shm` (so it can read the published plan). Expose
`FRONTEND_PORT`.

## Config

| Setting | Default | Purpose |
|---|---|---|
| `FRONTEND_ENABLED` | `False` | run the dashboard in-process (daemon thread) from the main service |
| `AI_PLAN_EXPORT_PATH` | `/dev/shm/cerbo_ai_plan.json` | where main publishes the plan / dashboard reads it |
| `FRONTEND_HOST` | `0.0.0.0` | bind address |
| `FRONTEND_PORT` | `8080` | bind port |

## Views

- **Header**: a sticky status strip (action, SoC, price, **Today** net, **Month**
  net) + clock. **Today** and **Month** are signed € chips — green `+` for profit,
  red `−` for loss, no "profit"/"cost" word. **Month** is the sum of this month's
  settled daily totals (`Σ export_reward − Σ import_cost` from the history).
  On phones, `app.mobile.css` compacts the header into logo + action/SoC pill + clock
  with the full status strip as a horizontal swipe row; the current price chip sits at
  the end of that swipe row. External Battery/Venus iframe views are scaled to 90%
  inside their panes on phones so more of the embedded page is visible.
- **Overview** (always visible): metric cards (action, SoC, price, day net, next SELL,
  PV remaining) + the current decision and its plain-English reason. The Solar card's
  main number is the optimizer-adjusted remaining PV for the rest of today; the
  original source value is shown underneath as `VRM forecast: ... kWh remaining`, so
  the card and schedule use the same number without hiding where the raw forecast
  came from.
- **Live** (tab): real-time power-flow diagram (v2) — **VRM-style info cards** laid
  out in the real Victron **physical topology**: Grid — **Inverter/Charger** — AC
  Loads across the AC bus; the Inverter/Charger linked down to the Battery; **Solar
  DC-coupled to the Battery**; and **EV** + **Gas** hanging off the AC Loads as two
  compact cards. Box sizes are **deliberately non-uniform** — a wider central
  Inverter/Charger + Battery column with smaller EV/Gas cards, echoing the Victron
  GUI-v2 proportions (fonts scale per box). Cards are wired with smooth **HASS-style
  connectors**; each wire stays **faintly visible** so the topology always reads, and
  **source-coloured dots** ride it in the direction of real power (grid import/export,
  battery charge/discharge). Each card carries richer telemetry — Grid & AC-Loads
  **per-phase L1/L2/L3**, Battery **temp · V · A · SoC · time-to-go**, Solar Watts +
  today's kWh, EV power + lifetime energy, Gas m³ — and the **Inverter/Charger** card
  shows the live SystemState word (mirroring `lib/constants.py`); the Grid headline
  shows **► import / ◄ export**. The SVG is **responsive** — it measures its container
  and re-lays everything to fill the full width **and** height (good for embedding on
  any screen) via a `ResizeObserver`. On phones it switches to a **VRM-style portrait
  layout** built around a centre **MP-II hub**: Grid and AC Loads as the top corners,
  Battery bottom-left, and the right column stacking **EV above a dropped-down Solar**
  (EV wired up to AC Loads), with a small **Gas** card centred at the bottom. The four
  hub-adjacent cards (Grid, AC Loads, Battery, EV) are evenly spaced, and the
  Solar→Battery line curves so its flowing particles read clearly. Each card shows a
  big split value (large number, small unit) over compact labelled detail rows
  (Grid/Loads per-phase W; Battery Voltage/Current/Temp). The desktop 3-column layout
  is untouched. Dependency-free, built once and mutated in
  place, updated via the live SSE push; the **EV** and **Gas** cards appear when
  `ev_w` / the plan's `gas_m³` are present. (Note: the **top-nav "Live"** entry is a
  different thing — an iframe to the external `https://venus.hs.mfis.net/app/` dashboard;
  this SVG power-flow is the **ESS view's "Live" sub-tab**.)
- **Trends** (tab): HA-style metric cards (**self-sufficiency %**, **self-consumed
  solar %**, **grid balance** bar) above a gradient SoC% + buy-price line chart with
  a `now` marker and clickable series legends, an actual-vs-forecast PV/load overlay,
  plus a **monthly net chart** with today's projected full-day marker
  (`/api/history/month`).
- **Schedule** (tab): expandable hour → 15-min → reasoning tree, color-coded by
  control action, with a per-hour timeline bar and aggregates. For today, the
  timeline starts at midnight with settled history rows, then flows into the forward
  plan from the active slot onward (`NOW` highlighted, auto-scrolls to now). A
  moving ledger row sits above the currently running hour and shows the settled
  cost/profit accumulated from midnight to now. A collapsed **"Previous day"** row
  above the tree expands into the prior day's
  *settled* schedule (`/api/history/day`) for a continuous 2–3 day view; past-day
  consumption is derived from the cumulative load counter in the cycle records.
  On phones, the wide table reflows into stacked hour cards and the current hour
  starts expanded.
- **Victron Schedule** (tab): mirrors the five Victron/CerboGX scheduled-charge
  slots from the published optimizer plan. The **Clear schedule** button calls the
  same broker helper used internally to disable those five Victron slots.
- **Weather** (desktop tab): visualizes cached Open-Meteo temperature/cloud patterns
  and shadow-mode HVAC load / GTI summaries with clickable series legends. It is
  observational unless `HVAC_LOAD_APPLY` or `PV_WEATHER_APPLY` are deliberately
  enabled after validation.
- **Advisor** (tab): a manually-triggered, read-only AI review. Click to stream a
  short markdown report on recent performance, or ask a free-text question ("why did
  we sell at 15:00 yesterday?"). It sends recent history + the allow-listed tunables
  (never secrets) + the current plan to a model via a **subscription-login CLI**
  (`ADVISOR_CLI_CMD` → Claude Code / Gemini / Codex), with extended thinking off and
  a hard prompt cap. For deep questions it pulls extra days from `data/history/` on
  demand (`NEED_HISTORY` protocol). The Advisor tab is a persisted chat session:
  timestamped prompts and responses are saved to `data/advisor_latest.json`, restored
  on browser refresh, and shown newest-first. Follow-up prompts include a compact
  transcript of the current chat so the model has session context. Individual
  messages can be copied, and a saved exchange can be deleted as a prompt/response
  pair. **Clear chat** empties the saved JSON and starts a fresh session. See the
  advisor config in `.env` / `.secrets`.
- **Configuration** (tab): click any value to edit it (number/select), confirm, and Save.
  Numeric settings expose schema min/max bounds and the server rejects out-of-range
  writes. On phones, descriptions sit behind an info toggle so edit targets remain large.

## Config knobs — how writes propagate

Editing a setting writes it to `.env` (the durable source of truth) via an
allow-listed, type-validated, atomic write. If Kubernetes mounts the writable env
file somewhere else, set `APP_ENV_PATH` so the dashboard writer, runtime reader,
and watcher all use the same file. The main service picks it up because:

- `lib.config_retrieval.retrieve_setting()` re-reads `.env` on every call, so the
  value applies on the **next optimization/decision cycle** (and it republishes the
  `Cerbomoticzgx/config/<KEY>` MQTT mirror on that read), and
- `lib.config_change_handler.ConfigWatcher` detects the file change and runs any
  per-key handler (e.g. a restart for `ACTIVE_MODULES`).

Only keys in `config_schema.py` are writable. Runtime *control* values that live in
`GlobalState`/the MQTT bus (e.g. `ess_net_metering_enabled`) are a separate future
class of knob and will be written via `STATE.set()` instead of `.env`.

The config view intentionally omits obsolete grid-charge price cap knobs. Forced
grid charging is now selected by modeled path economics; the remaining
`ESS_MAX_GRID_CHARGE_SOC` setting only limits the SoC target of grid charging, while
PV surplus may still charge above it.

## Real-time data

`frontend/live.py` subscribes (read-only) to the same broker the main service uses
(`MOSQUITTO_IP`) and caches the latest value for SoC, price, grid/PV/battery/load
power, AC setpoint, Tibber daily import/export/cost counters, and the published
`ai_mode`/`ai_reason`/`feed_in_limit_state`. For the **v2 power-flow cards** it also
caches the richer per-component telemetry: **grid & AC-loads per-phase L1/L2/L3**,
**battery temperature / voltage (LFP pack) / current / time-to-go**, the
**inverter system-state code**, and **EV lifetime energy + session time**. Topic
choices mirror `lib/constants.py`; any topic a given Venus OS build doesn't publish
simply stays `None` and the UI hides that line. **Newly-added subscriptions only
take effect when the dashboard process (re)starts** — the MQTT subscriber registers
its topic list once at startup.
The UI receives these via a **Server-Sent Events push** (`/api/live/stream`) — the
server streams a fresh snapshot the instant a new MQTT value arrives, so the
Overview, day summary, and Live diagram update in real time with no polling lag.
A slow 20s poll of `/api/live` remains as a fallback if the stream drops or is
proxy-buffered. The values overlay the slower plan snapshot. A green/grey dot on
the "Now" card shows whether the live feed is
connected; if it's offline the UI falls back to plan values. No new config — it
reuses `MOSQUITTO_IP` and `VRM_PORTAL_ID`.

## API

- `GET /api/plan` — current decision, `planning_policy`, hour-grouped schedule, day
  summary, month-to-date net (`mtd_net`, from settled history), staleness, and both
  raw VRM PV remaining (`pv_remaining_raw_*`) and optimizer-adjusted PV remaining
  (`pv_adjusted_remaining_*`) for the Solar card.
- `GET /api/live` — live MQTT values (SoC, price, grid/PV/battery/load/EV W, …).
- `GET /api/live/stream` — Server-Sent Events; pushes a live snapshot on each MQTT update.
- `GET /api/history/month` — per-day net €/profit for the current month (Trends chart).
- `GET /api/history/accuracy` — recent actual-vs-forecast PV/load slots.
- `GET /api/weather` — cached Open-Meteo forecast and shadow-mode impact summary.
- `GET /api/history/day?days_back=1` — a prior day's settled hour-tree (previous-day view).
- `POST /api/advisor` — run the read-only advisor (default review, or `{ "question": … }`).
- `GET /api/advisor/stream?question=…` — Server-Sent Events stream of the advisor run.
- `GET /api/advisor/latest` — persisted advisor chat session restored on refresh.
- `POST /api/advisor/clear` — clear the persisted advisor chat session.
- `GET /api/config` — settings schema with current values.
- `POST /api/config` — `{ "key": ..., "value": ... }`, writes one allow-listed setting.
- `POST /api/replan` — ask the main service to re-run the optimizer now.
- `POST /api/restart` — request the existing supervised service restart via MQTT.
- `POST /api/control/ai-override` — toggle AI ESS override; enabling idles Victron once
  and makes the optimizer stand down until toggled off.
- `POST /api/control/grid-assist` — toggle the existing manual grid-assist/retain mode
  (`grid_charging_enabled`) so grid covers loads and the battery is held. This is the
  house-battery hold only — it no longer starts/stops the car.
- `POST /api/control/ev-charge` — manual EV **Start/Stop** (Vehicle tab). Sets the dedicated
  `ev_charge_requested` intent (decoupled from grid-assist); the EV controller then starts/stops
  the car with its safety checks (home + plugged + non-supercharging, wake escalation,
  local-meter stop verification).
- `POST /api/victron/clear-schedule` — clear the five Victron scheduled-charge slots.
- `GET /healthz` — liveness.

## Notes / roadmap

- No authentication in v1 by design: this is an internally deployed trusted-LAN
  operator app, no egress is allowed, and unauthenticated LAN users are intended
  to have full access. Add a reverse proxy / auth before exposing beyond the LAN,
  especially now that config is writable and schedule clearing is exposed.
- **Done:** ✅ (1) SoC + price horizon chart; ✅ (2) live power-flow diagram (rebuilt
  HASS-style, source-coloured direct flows); ✅ (4) **historical performance** — monthly
  net chart + month-to-date chip (sum of settled daily totals); ✅ (6) unified past-actuals +
  forward-plan timeline, plus a collapsed **previous-day settled** view; ✅ **AI
  Advisor** (Phase 1) — read-only, on-demand, plain-language review + Q&A; ✅
  mobile-responsive phone layout with bottom navigation, a guarded Menu sheet, and
  focused Live/Trends/Advisor/Victron Schedule/Configuration views that hide the
  overview cards on phones; ✅ forecast-accuracy overlay; ✅ desktop Weather tab;
  ✅ best-daily-settlement selector and path-economic grid charging; ✅ clickable
  chart legends; ✅ runtime Override and Grid assist controls; ✅ Solar card alignment
  with the optimizer PV nowcast while preserving the source-labelled VRM forecast.
- Roadmap (next up):
  - (3) More runtime control toggles, if needed, should follow the `STATE.set` /
    retained-MQTT pattern used by Override and Grid assist.
  - (7) **Battery-health** widget — cycles/day and €-per-cycle, to watch wear vs gain.
  - (8) **Auth / reverse-proxy** hardening.
  - (9) **CSV export** of plan + history for offline analysis.
  - Advisor **Phase 2/3** — approve-to-apply for bounded tunables (dry-run backtested),
    then model-proposed code changes via PR (human-gated). See root `TODO.md`.
