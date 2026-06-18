# cerbomoticzGx Dashboard (`frontend/`)

A self-contained, **read-only** web dashboard for visibility into the ESS service.
v1 shows the current decision, the full optimizer schedule (expandable hour → 15-min
→ reasoning tree), the day cost summary (actuals + forecast), and the current
configuration. Control "knobs" are a planned next step — nothing here writes to the
Victron system today.

## Architecture / separation of concerns

```
frontend/
  __main__.py        # python -m frontend
  server.py          # Flask routes + run()/run_in_thread()
  data.py            # read-only data layer (plan JSON + .env), hour grouping, day summary
  config_schema.py   # declarative settings schema (drives the config view, future knobs)
  templates/index.html
  static/css/app.css
  static/js/app.js         # core render + polling; calls the view modules defensively
  static/js/powerflow.js   # self-contained Live power-flow SVG (window.renderPowerFlow)
  static/js/charts.js      # self-contained SoC+price horizon SVG (window.renderHorizonChart)
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

- **Overview** (always visible): metric cards (action, SoC, price, day net, next SELL,
  PV remaining) + the current decision and its plain-English reason.
- **Live** (tab): real-time power-flow diagram — Solar / Grid / Battery / House —
  with curved connectors and animated flow dots whose direction follows real power
  (import/export, charge/discharge), per-node live Watts + today's kWh totals, SoC
  in the battery node, and a clock. Dependency-free SVG, updating every ~5s from
  `/api/live` (+ live Tibber daily import/export/cost counters). An **EV** node
  appears automatically when an `ev_w` live value is present (see note below).
- **Trends** (tab): HA-style metric cards — **self-sufficiency %**, **self-consumed
  solar %**, and a **grid balance** bar (import vs export, net) — above a gradient
  SoC% + buy-price line chart across the full horizon with a `now` marker. The
  derived metrics come from the plan's `today` block (computed server-side from
  daily yields, consumption, and grid actuals).
- **Schedule** (tab): expandable hour → 15-min → reasoning tree, color-coded by
  control action, with a per-hour timeline bar and aggregates. For today, the
  timeline starts at midnight with settled history rows (actual import/export,
  net, SoC, and PV production from `kind: "settlement"` records), then flows into
  the forward plan from the active slot onward. The current hour/slot are
  highlighted (`NOW`) and the view auto-scrolls to "now" on open.
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

- `GET /api/plan` — current decision, hour-grouped schedule, day summary, staleness.
- `GET /api/live` — live MQTT values (SoC, price, grid/PV/battery/load/EV W, …).
- `GET /api/live/stream` — Server-Sent Events; pushes a live snapshot on each MQTT update.
- `GET /api/config` — settings schema with current values.
- `POST /api/config` — `{ "key": ..., "value": ... }`, writes one allow-listed setting.
- `GET /healthz` — liveness.

## Notes / roadmap

- No authentication in v1 (intended for a trusted LAN). Add a reverse proxy / auth
  before exposing beyond the LAN, especially now that config is writable.
- **Done:** ✅ (1) SoC + price horizon chart (Trends tab, no CDN); ✅ (2) live
  power-flow mini-diagram (Live tab, no CDN); ✅ (6) unified past-actuals +
  forward-plan timeline in Schedule. These are isolated modules.
- Roadmap (next up):
  - (3) **Control toggles** (enable optimizer, net metering) written via `STATE.set`
    — the second write path, distinct from `.env` config knobs.
  - (4) **Historical performance** view — realised daily € from the history NDJSON.
  - (5) **Forecast-accuracy** view — predicted-vs-actual per slot from the `kind:
    "settlement"` records (net-€ error, PV/load forecast bias over time).
  - (7) **Battery-health** widget — cycles/day and €-per-cycle, to watch wear vs gain.
  - (8) **Auth / reverse-proxy** hardening + a mobile-responsive layout.
  - (9) **CSV export** of plan + history for offline analysis.
