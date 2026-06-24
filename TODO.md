# TODO / roadmap

- **Decide EV handling** (#2) — feed predictable EV fast-charging sessions into the
  load forecast so per-slot net predictions don't diverge during 17–19 kW midday
  charging. Still open.

- **Forecast-accuracy overlay** (Trends) — actual-vs-forecast PV and consumption per
  slot/hour, VRM-style, between the SoC/price and monthly-net charts. The data
  groundwork has landed (`predicted_pv_kwh` / `predicted_load_kwh` / `actual_load_kwh`
  on each settlement; past-day actual consumption derivable from cycle counters); the
  chart itself is the next build.

## AI Advisor

- ✅ **Phase 1 — Advisor tab (done).** Manually-triggered, read-only, streaming
  plain-language review of recent history + the tunable subset, plus free-text Q&A,
  with on-demand history retrieval. Authenticates via a subscription-login CLI
  (`ADVISOR_CLI_CMD` → Claude Code / Gemini / Codex), never a metered API key.

- Phase 2 — approve-to-apply for tunables only. Each setting has hard min/max bounds;
  on approval the system runs a dry-run backtest, shows projected €, writes .env
  (hot-reloads, no restart), and auto-reverts if the next day underperforms. Bounded
  numbers can't crash the controller — this is the safe sweet spot.

- Phase 3 — code changes via PR, not hot-patch. Let the model propose a diff + tests;
  the "apply" button opens a PR/branch for human review and your normal pytest gate.
  Keep a human on the actual diff before anything restarts a 16 kW controller.
