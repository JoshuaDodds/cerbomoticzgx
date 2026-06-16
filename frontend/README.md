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
  static/js/app.js
```

- The **main service** publishes its plan as JSON (atomic write) to
  `AI_PLAN_EXPORT_PATH` (default `/dev/shm/cerbo_ai_plan.json`) on every optimizer
  run. The dashboard only *reads* that file plus `.env` — it never imports the
  control path or touches MQTT, so it cannot interfere with the optimizer.
- Mode colors: BUY (blue), SELL (green), HOLD (amber), SELF-SUPPLY (teal).

## Running

Standalone (recommended — own process / container sidecar):

```bash
pip install -r requirements.txt          # adds flask
python -m frontend                        # serves on FRONTEND_HOST:FRONTEND_PORT
```

Optional in-process (daemon thread) from the main service:

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
| `AI_PLAN_EXPORT_PATH` | `/dev/shm/cerbo_ai_plan.json` | where main publishes the plan / dashboard reads it |
| `FRONTEND_HOST` | `0.0.0.0` | bind address |
| `FRONTEND_PORT` | `8080` | bind port |

## Views

- **Overview** (always visible): metric cards (mode, SoC, price, day net, next SELL,
  PV remaining) + the current decision and its plain-English reason.
- **Schedule**: expandable hour → 15-min → reasoning tree, color-coded by mode, with
  a per-hour timeline bar and aggregates. The current hour/slot are highlighted
  (`NOW`) and the view auto-scrolls to "now" on open.
- **Configuration**: click any value to edit it (number/select), confirm, and Save.

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
power, AC setpoint, and the published `ai_mode`/`ai_reason`/`feed_in_limit_state`.
The UI polls `/api/live` every ~5s and overlays these live values on the slower
plan snapshot, so the Overview (mode, SoC, price, grid flow, power-flow strip) is
truly live. A green/grey dot on the "Now" card shows whether the live feed is
connected; if it's offline the UI falls back to plan values. No new config — it
reuses `MOSQUITTO_IP` and `VRM_PORTAL_ID`.

## API

- `GET /api/plan` — current decision, hour-grouped schedule, day summary, staleness.
- `GET /api/live` — live MQTT values (SoC, price, grid/PV/battery/load W, mode, …).
- `GET /api/config` — settings schema with current values.
- `POST /api/config` — `{ "key": ..., "value": ... }`, writes one allow-listed setting.
- `GET /healthz` — liveness.

## Notes / roadmap

- No authentication in v1 (intended for a trusted LAN). Add a reverse proxy / auth
  before exposing beyond the LAN, especially now that config is writable.
- Roadmap (next up): (1) SoC + price line chart across the horizon (bundled, no CDN);
  (2) live power-flow mini-diagram (grid↔battery↔PV↔house); (3) control toggles
  (enable optimizer, net metering) written via `STATE.set` (the second write path);
  (4) historical performance (daily realised €).
