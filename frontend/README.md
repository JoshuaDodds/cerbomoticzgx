# cerbomoticzGx Dashboard (`frontend/`)

A self-contained web dashboard for visibility into the ESS service, with a small
set of explicit operator actions.
It shows the current decision, the full optimizer schedule (expandable hour → 15-min
→ reasoning tree, including a collapsed **previous-day settled** view), a live
power-flow diagram, day/month cost summaries, a Tibber-sourced **month-to-date
profit** chip, a **Trends** view (SoC/price + monthly net), an **AI Advisor** that
reviews recent performance on demand, and allow-listed `.env` config editing.
The only direct Victron control action exposed here is the guarded **Import
Schedule** clear button, which disables the five scheduled-charge slots; config
edits go to `.env`, and the advisor only reads history and shells out to a local
subscription CLI.

## Architecture / separation of concerns

```
frontend/
  __main__.py        # python -m frontend
  server.py          # Flask routes + run()/run_in_thread()
  data.py            # read-only data layer (plan/history/.env), hour grouping, day/month summary, Tibber MTD
  live.py            # read-only MQTT subscriber -> live snapshot (SSE source)
  advisor.py         # read-only AI advisor: builds the prompt, shells out to a subscription CLI, streams the review
  config_schema.py   # declarative settings schema (drives the config view + advisor's allow-listed tunables)
  templates/index.html
  static/css/app.css
  static/css/app.mobile.css  # phone-only overrides at <=680px; desktop rules stay untouched
  static/js/app.js         # core render + polling; calls the view modules defensively
  static/js/powerflow.js   # self-contained Live power-flow SVG (window.renderPowerFlow) — direct source-coloured flows, no hub
  static/js/charts.js      # self-contained SoC+price horizon SVG + monthly net chart
```

The two view modules are loaded before `app.js` and invoked inside `try/catch`, so a
failure in either is isolated and cannot break the core dashboard.

- The **main service** publishes its plan as JSON (atomic write) to
  `AI_PLAN_EXPORT_PATH` (default `/dev/shm/cerbo_ai_plan.json`) on every optimizer
  run. Plan/history/live views only read that file plus `.env`; explicit operator
  POST routes import control helpers inside the request handler.
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
  PV remaining) + the current decision and its plain-English reason.
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
  layout** — 2×2 corner cards (Grid · AC Loads on top, Battery · Solar below) around a
  centre **MP-II hub**, each card showing a big split value (large number, small unit)
  over compact labelled detail rows (Grid/Loads per-phase W; Battery Voltage/Current/
  Temp), with EV and a small **Gas** card below the fold. The desktop 3-column layout
  is untouched. Dependency-free, built once and mutated in
  place, updated via the live SSE push; the **EV** and **Gas** cards appear when
  `ev_w` / the plan's `gas_m³` are present. (Note: the **top-nav "Live"** entry is a
  different thing — an iframe to the external `http://192.168.1.163/app/` dashboard;
  this SVG power-flow is the **ESS view's "Live" sub-tab**.)
- **Trends** (tab): HA-style metric cards (**self-sufficiency %**, **self-consumed
  solar %**, **grid balance** bar) above a gradient SoC% + buy-price line chart with
  a `now` marker, plus a **monthly net chart** — per-day €/profit for the current
  month with hover tooltips (`/api/history/month`).
- **Schedule** (tab): expandable hour → 15-min → reasoning tree, color-coded by
  control action, with a per-hour timeline bar and aggregates. For today, the
  timeline starts at midnight with settled history rows, then flows into the forward
  plan from the active slot onward (`NOW` highlighted, auto-scrolls to now). A
  collapsed **"Previous day"** row above the tree expands into the prior day's
  *settled* schedule (`/api/history/day`) for a continuous 2–3 day view; past-day
  consumption is derived from the cumulative load counter in the cycle records.
  On phones, the wide table reflows into stacked hour cards and the current hour
  starts expanded.
- **Import Schedule** (tab): mirrors the five Victron/CerboGX scheduled-charge
  slots from the published optimizer plan. The **Clear schedule** button asks for
  confirmation and then calls the same broker helper used internally to disable
  those five Victron slots.
- **Advisor** (tab): a manually-triggered, read-only AI review. Click to stream a
  short markdown report on recent performance, or ask a free-text question ("why did
  we sell at 15:00 yesterday?"). It sends recent history + the allow-listed tunables
  (never secrets) + the current plan to a model via a **subscription-login CLI**
  (`ADVISOR_CLI_CMD` → Claude Code / Gemini / Codex), with extended thinking off and
  a hard prompt cap. For deep questions it pulls extra days from `data/history/` on
  demand (`NEED_HISTORY` protocol). The latest completed report or latest error is
  saved to `data/advisor_latest.json` and restored into the Advisor tab on browser
  refresh. Starting a new review/question clears the previous saved result and
  replaces it when that run finishes. See the advisor config in `.env` / `.secrets`.
- **Configuration** (tab): click any value to edit it (number/select), confirm, and Save.
  On phones, descriptions sit behind an info toggle so edit targets remain large.

## Config knobs — how writes propagate

Editing a setting writes it to `.env` (the durable source of truth) via an
allow-listed, type-validated, atomic write. The main service picks it up because:

- `lib.config_retrieval.retrieve_setting()` re-reads `.env` on every call, so the
  value applies on the **next optimization/decision cycle** (and it republishes the
  `Cerbomoticzgx/config/<KEY>` MQTT mirror on that read), and
- `lib.config_change_handler.ConfigWatcher` detects the file change and runs any
  per-key handler (e.g. a restart for `ACTIVE_MODULES`).

Only keys in `config_schema.py` are writable. Runtime *control* values that live in
`GlobalState`/the MQTT bus (e.g. `ess_net_metering_enabled`) are a separate future
class of knob and will be written via `STATE.set()` instead of `.env`.

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

- `GET /api/plan` — current decision, hour-grouped schedule, day summary,
  month-to-date net (`mtd_net`, from settled history), staleness.
- `GET /api/live` — live MQTT values (SoC, price, grid/PV/battery/load/EV W, …).
- `GET /api/live/stream` — Server-Sent Events; pushes a live snapshot on each MQTT update.
- `GET /api/history/month` — per-day net €/profit for the current month (Trends chart).
- `GET /api/history/day?days_back=1` — a prior day's settled hour-tree (previous-day view).
- `POST /api/advisor` — run the read-only advisor (default review, or `{ "question": … }`).
- `GET /api/advisor/stream?question=…` — Server-Sent Events stream of the advisor run.
- `GET /api/advisor/latest` — latest saved advisor report/error restored on refresh.
- `GET /api/config` — settings schema with current values.
- `POST /api/config` — `{ "key": ..., "value": ... }`, writes one allow-listed setting.
- `POST /api/replan` — ask the main service to re-run the optimizer now.
- `POST /api/victron/clear-schedule` — clear the five Victron scheduled-charge slots.
- `GET /healthz` — liveness.

## Notes / roadmap

- No authentication in v1 (intended for a trusted LAN). Add a reverse proxy / auth
  before exposing beyond the LAN, especially now that config is writable and
  schedule clearing is exposed.
- **Done:** ✅ (1) SoC + price horizon chart; ✅ (2) live power-flow diagram (rebuilt
  HASS-style, source-coloured direct flows); ✅ (4) **historical performance** — monthly
  net chart + month-to-date chip (sum of settled daily totals); ✅ (6) unified past-actuals +
  forward-plan timeline, plus a collapsed **previous-day settled** view; ✅ **AI
  Advisor** (Phase 1) — read-only, on-demand, plain-language review + Q&A; ✅
  mobile-responsive phone layout with bottom navigation, a guarded Menu sheet, and
  focused Live/Trends/Advisor/Import Schedule/Configuration views that hide the
  overview cards on phones.
- Roadmap (next up):
  - (3) **Control toggles** (enable optimizer, net metering) written via `STATE.set`
    — the second write path, distinct from `.env` config knobs.
  - (5) **Forecast-accuracy overlay** — actual-vs-forecast PV and consumption per slot
    in Trends, from the new `predicted_pv_kwh` / `predicted_load_kwh` /
    `actual_load_kwh` settlement fields (groundwork landed; chart is the next build).
  - (7) **Battery-health** widget — cycles/day and €-per-cycle, to watch wear vs gain.
  - (8) **Auth / reverse-proxy** hardening.
  - (9) **CSV export** of plan + history for offline analysis.
  - Advisor **Phase 2/3** — approve-to-apply for bounded tunables (dry-run backtested),
    then model-proposed code changes via PR (human-gated). See root `TODO.md`.
