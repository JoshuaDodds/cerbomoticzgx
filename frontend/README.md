# cerbomoticzGx Dashboard (`frontend/`)

A self-contained, **read-only** web dashboard for visibility into the ESS service.
It shows the current decision, the full optimizer schedule (expandable hour → 15-min
→ reasoning tree, including a collapsed **previous-day settled** view), a live
power-flow diagram, day/month cost summaries, a Tibber-sourced **month-to-date
profit** chip, a **Trends** view (SoC/price + monthly net), an **AI Advisor** that
reviews recent performance on demand, and allow-listed `.env` config editing.
Nothing here writes to the Victron control path — config edits go to `.env`, and the
advisor only reads history and shells out to a local subscription CLI.

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
  static/js/app.js         # core render + polling; calls the view modules defensively
  static/js/powerflow.js   # self-contained Live power-flow SVG (window.renderPowerFlow) — direct source-coloured flows, no hub
  static/js/charts.js      # self-contained SoC+price horizon SVG + monthly net chart
```

The two view modules are loaded before `app.js` and invoked inside `try/catch`, so a
failure in either is isolated and cannot break the core dashboard.

- The **main service** publishes its plan as JSON (atomic write) to
  `AI_PLAN_EXPORT_PATH` (default `/dev/shm/cerbo_ai_plan.json`) on every optimizer
  run. The dashboard only *reads* that file plus `.env` — it never imports the
  control path or touches MQTT, so it cannot interfere with the optimizer.
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
- **Overview** (always visible): metric cards (action, SoC, price, day net, next SELL,
  PV remaining) + the current decision and its plain-English reason.
- **Live** (tab): real-time power-flow diagram — Solar / Grid / Battery / House
  (+ EV / Gas when present). HASS/Domoticz-style: **no central hub** — energy flows
  along **direct source→sink paths** computed from a flow decomposition (PV→house/
  battery/grid, battery→house, grid→house/battery), and every flow dot keeps its
  **source colour** end-to-end so grid/battery/solar are distinguishable. Thicker
  ribbon connectors with a **watt label** per active flow; per-node live Watts +
  today's kWh, SoC in the battery node. Dependency-free SVG, updated via the live
  SSE push. An **EV** node appears automatically when an `ev_w` value is present.
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
- **Advisor** (tab): a manually-triggered, read-only AI review. Click to stream a
  short markdown report on recent performance, or ask a free-text question ("why did
  we sell at 15:00 yesterday?"). It sends recent history + the allow-listed tunables
  (never secrets) + the current plan to a model via a **subscription-login CLI**
  (`ADVISOR_CLI_CMD` → Claude Code / Gemini / Codex), with extended thinking off and
  a hard prompt cap. For deep questions it pulls extra days from `data/history/` on
  demand (`NEED_HISTORY` protocol). See the advisor config in `.env` / `.secrets`.
- **Configuration** (tab): click any value to edit it (number/select), confirm, and Save.

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
`ai_mode`/`ai_reason`/`feed_in_limit_state`.
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
- `GET /api/config` — settings schema with current values.
- `POST /api/config` — `{ "key": ..., "value": ... }`, writes one allow-listed setting.
- `POST /api/replan` — ask the main service to re-run the optimizer now.
- `GET /healthz` — liveness.

## Notes / roadmap

- No authentication in v1 (intended for a trusted LAN). Add a reverse proxy / auth
  before exposing beyond the LAN, especially now that config is writable.
- **Done:** ✅ (1) SoC + price horizon chart; ✅ (2) live power-flow diagram (rebuilt
  HASS-style, source-coloured direct flows); ✅ (4) **historical performance** — monthly
  net chart + month-to-date chip (sum of settled daily totals); ✅ (6) unified past-actuals +
  forward-plan timeline, plus a collapsed **previous-day settled** view; ✅ **AI
  Advisor** (Phase 1) — read-only, on-demand, plain-language review + Q&A.
- Roadmap (next up):
  - (3) **Control toggles** (enable optimizer, net metering) written via `STATE.set`
    — the second write path, distinct from `.env` config knobs.
  - (5) **Forecast-accuracy overlay** — actual-vs-forecast PV and consumption per slot
    in Trends, from the new `predicted_pv_kwh` / `predicted_load_kwh` /
    `actual_load_kwh` settlement fields (groundwork landed; chart is the next build).
  - (7) **Battery-health** widget — cycles/day and €-per-cycle, to watch wear vs gain.
  - (8) **Auth / reverse-proxy** hardening + a mobile-responsive layout.
  - (9) **CSV export** of plan + history for offline analysis.
  - Advisor **Phase 2/3** — approve-to-apply for bounded tunables (dry-run backtested),
    then model-proposed code changes via PR (human-gated). See root `TODO.md`.
