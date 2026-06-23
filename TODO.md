- **Decide EV handling** (#2) — feed predictable EV sessions into the load forecast.

- Phase 1 — Advisor tab (do this now). A scheduled daily job feeds recent history + the tunable subset + each setting's doc to a capable model and renders a prioritized, plain-language report in a new tab. Read-only. All upside, no control risk. This alone is like having an expert review your logs every morning.

- Phase 2 — approve-to-apply for tunables only. Each setting has hard min/max bounds; on approval the system runs a dry-run backtest, shows projected €, writes .env (hot-reloads, no restart), and auto-reverts if the next day underperforms. Bounded numbers can't crash the controller — this is the safe sweet spot.

- Phase 3 — code changes via PR, not hot-patch. Let the model propose a diff + tests; the "apply" button opens a PR/branch for human review and your normal pytest gate. Keep a human on the actual diff before anything restarts a 16 kW controller.