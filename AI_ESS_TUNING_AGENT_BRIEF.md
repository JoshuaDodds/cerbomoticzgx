# AI ESS Optimizer — Tuning-Agent Brief

You are a tuning agent with SSH + a real bash shell on the host running the
`cerbomoticzGx` home-energy controller. Your job: **find the AI-ESS tunable
settings that maximize the forecast euro return over the coming horizon**, using
the read-only dry-run tool, then **stop and report**. You make NO changes to the
live system and you do NOT run the service.

---

## 1. Environment setup (do this first)

The repo is at `/Development/cerbomoticzgx`. The project runs on **pyenv Python
3.11.11**, and all dependencies are already installed in that runtime — you do
NOT need to pip install anything. Just make sure you're using the right runtime:

```bash
cd /Development/cerbomoticzgx
pyenv shell 3.11.11          # or rely on the repo's .python-version
python --version             # MUST print: Python 3.11.11
which python                 # should resolve under ~/.pyenv/versions/3.11.11
```

If `python --version` is not 3.11.11, fix the runtime before doing anything else
(wrong interpreter = missing deps / misleading errors). Use `python` (the pyenv
shim) for all commands once verified; if `python3` resolves elsewhere, prefer
`python`.

---

## 2. The only tool you run

```bash
python scripts/ai_ess_dryrun.py            # full horizon, current live data
python scripts/ai_ess_dryrun.py --hours 24 # truncate the per-slot TABLE only
python scripts/ai_ess_dryrun.py --soc 50   # override starting SoC (optional)
python scripts/ai_ess_dryrun.py --json     # also dump raw plan as JSON
```

This script is **strictly read-only**: it computes the plan from live data
(battery SoC, Tibber prices, PV/load forecasts) and prints it. It writes NOTHING
to the Victron bus, MQTT, or state. Run it from the repo root (it relies on the
current working directory).

**Hard rules:**
- ONLY run `scripts/ai_ess_dryrun.py`. Never run `main.py`, the service, or
  anything that could write to the inverter.
- The dry run re-reads `.env` fresh on every invocation — so edit `.env`, re-run,
  see the effect immediately. No restart needed.
- The top of the output has an **ENGINE TUNABLES** block. After every `.env`
  edit, confirm your change is reflected there before trusting the result.

---

## 3. The objective

At the bottom of the output is a `DAY COST SUMMARY`. Use the **`TOTAL ... net
€X.XX profit/cost`** line as your objective — **maximize profit** (equivalently,
minimize cost). Example:

```
  TOTAL    import 97.22 kWh € 23.16   export 77.79 kWh € 26.21   net €3.05 profit
```

Notes:
- The "actual so far" portion of today is **sunk and constant** during your
  session, so deltas in TOTAL net cleanly reflect your tuning. (You're really
  optimizing the "forecast rest".)
- The TOTAL net is over the full horizon regardless of `--hours` (that flag only
  truncates the printed table).
- The horizon is whatever Tibber currently provides (~today + tomorrow). Do all
  your runs in a tight time window so the baseline market data doesn't shift
  under you; if you suspect prices/PV refreshed mid-session, re-run your baseline.

---

## 4. Tunables you MAY change (all in the "NEW ESS ALGORITHM SETTINGS" block of `.env`)

| Setting | Meaning | Sensible range to explore |
|---|---|---|
| `ESS_BATTERY_CYCLE_COST` | €/kWh wear cost on discharge; raises the bar to cycle | 0.00 – 0.08 |
| `ESS_ARBITRAGE_MARGIN` | €/kWh extra profit cushion on discharge (on top of wear) | 0.00 – 0.08 |
| `ESS_MAX_GRID_CHARGE_PRICE` | Absolute €/kWh ceiling; block grid charging above it (0 = off) | 0 or 0.10 – 0.40 |
| `ESS_GRID_CHARGE_CHEAP_PCT` | Relative ceiling: only grid-charge in cheapest N% of horizon (0/≥100 = off) | 0 or 15 – 60 |
| `ESS_MIN_SELL_PRICE` | €/kWh floor; never actively discharge to grid below this | 0.00 – 0.30 |
| `ESS_TERMINAL_VALUE_FACTOR` | Value of end-of-horizon stored energy × horizon-mean price | 0.0 – 1.2 |
| `ESS_EXPECTED_PEAK_PRICE` | Holds charge for an expected peak (0 = off) | 0 or 0.25 – 0.45 |
| `OPTIMIZER_SOC_STEP_PCT` | DP granularity (smaller = finer/slower). Affects runtime, barely profit | 1 – 5 (leave 1–2) |

## 5. Tunables you MUST NOT change

These model physical/market/safety reality — changing them produces fake profit
or unsafe behaviour:

- `ESS_EXPORT_PRICE_FACTOR`, `ESS_EXPORT_FEE` — the real Tibber export tariff.
  Under NL net-metering (saldering, in force until 1 Jan 2027) `1.0 / 0.0` is
  correct. Raising the factor just inflates imaginary revenue. **Treat as fixed.**
- `MIN_SOC_RESERVE_WINTER` / `MIN_SOC_RESERVE_SUMMER` — safety/backup policy.
- `BATTERY_CAPACITY_KWH`, `AC_DC_*_EFFICIENCY`, `ESS_MAX_*_KW`,
  `ESS_EXPORT_AC_SETPOINT` — hardware facts.
- Anything outside the AI-ESS block (Tibber, VRM, secrets, module toggles).

---

## 6. Traps — don't be fooled by a bigger number

1. **Forecast inflation via churn.** Dropping `ESS_BATTERY_CYCLE_COST` and
   `ESS_ARBITRAGE_MARGIN` toward 0 lets the optimizer chase tiny intraday price
   wiggles. The forecast net goes up, but it's high-churn, fragile to forecast
   error, and adds battery wear — it usually realizes WORSE in practice. A higher
   number that comes purely from more cycling is not a win.
2. **Report cycling, not just €.** For each candidate, also estimate battery
   throughput so we can see €-per-cycle. From the `--json` schedule, sum the
   per-slot |Δsoc| and convert: `cycles ≈ Σ|soc_end-soc_start|/100 × 42kWh / (2 ×
   42kWh) = Σ|Δsoc| / 200`. Prefer settings with high € AND low/normal cycling.
3. **Don't overfit one snapshot.** The best setting should be robust across the
   day's shape, not a knife-edge fit to this exact forecast. Relative ceilings
   (`ESS_GRID_CHARGE_CHEAP_PCT`) adapt better day-to-day than a brittle absolute
   `ESS_MAX_GRID_CHARGE_PRICE`; weight that in your recommendation.
4. **Load forecast may be high.** The dry run header prints "forecast house load
   (horizon)". If it looks far above ~18 kWh/day, the VRM consumption forecast may
   be overestimating, which depresses the net. Flag it; don't try to tune around it.

---

## 7. System context (so your reasoning is grounded)

- 16 kW 3-phase Victron ESS, **42 kWh** LFP battery, DC-coupled PV, on Pi-class hardware.
- Tibber **dynamic 15-minute** pricing, Netherlands. Saldering (net metering) in
  force until 1 Jan 2027 (hence export factor 1.0).
- Summer min-SoC reserve = 0%, winter = 40%.
- The optimizer is a DP over discretized SoC and re-plans every 15 min (MPC); the
  dry run is a single snapshot of the current horizon.
- Known structure from prior analysis: the **midday-charge → evening-peak sell**
  cycle is the money-maker (wide spread); a **overnight-charge → morning-sell**
  cycle is usually marginal churn. Good settings keep the former and prune the latter.
- Tunable resolution order is `.secrets` > STATE > `.env`; the dry run reads
  `.env` live, so edits apply on the next run.

---

## 8. Workflow

1. Back up the current config: `cp .env .env.tuning.bak`.
2. Run a **baseline** dry run; record the TOTAL net € and estimated cycles.
3. Change **one knob at a time** (or a small deliberate grid), re-run, record.
   Confirm each change shows in the ENGINE TUNABLES block.
4. Keep a results table: settings → TOTAL net € → est. cycles → notes.
5. Converge on the best **robust** setting (not just the paper max — apply §6).
6. **Restore** the original config: `cp .env.tuning.bak .env` (leave the system as
   you found it), unless explicitly told to keep the winner.
7. **Stop and report.**

## 9. Report format

- Baseline net € and cycles.
- Best settings found (exact `.env` keys/values) and the resulting net € + cycles.
- The € improvement vs baseline, and how much (if any) came from extra cycling.
- A robustness note: would this generalize to other days, or is it fit to this snapshot?
- Any forecast-quality observations (PV vs actual, load vs actual).
- Confirm you restored `.env` and ran nothing but the dry-run script.
