# TODO / roadmap

- **Decide EV handling** (#2) — feed predictable EV fast-charging sessions into the
  load forecast so per-slot net predictions don't diverge during 17–19 kW midday
  charging. Still open.

- **Forecast-accuracy overlay** (Trends) — actual-vs-forecast PV and consumption per
  slot/hour, VRM-style, between the SoC/price and monthly-net charts. The data
  groundwork has landed (`predicted_pv_kwh` / `predicted_load_kwh` / `actual_load_kwh`
  on each settlement; past-day actual consumption derivable from cycle counters); the
  chart itself is the next build.

## Dashboard / UX

- **Projected Today profit point on the "Daily net — month so far" chart.** Today's
  point currently shows only the *settled-so-far* net (e.g. −€2.80 in the morning,
  which looks like a loss while the header forecasts a +€6.07 day). Add a second,
  visually-distinct point for the **forecast full-day** net so both are visible:
  the in-progress real amount AND the projected day total (e.g. a hollow/dashed
  marker, or a faint "projected" segment from the settled point up to the forecast).

- **HASS Live Flow as inspiration for our power-flow v2.** Ask the user to open the
  Home Assistant Energy / power-flow card in a Chrome browser so we can inspect its
  JS/SVG, debug ours against it, and borrow its rendering ideas to build v2 of our
  Live flow visualization.

- **Mobile (iPhone 12 Pro) UX pass.** Make the dashboard genuinely usable and
  beautiful on a phone. Mobile-only via media queries — **must NOT change how the
  desktop renders** (verify no desktop regressions while iterating). See the detailed
  findings + plan in `MOBILE_UX_PLAN.md`.

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
