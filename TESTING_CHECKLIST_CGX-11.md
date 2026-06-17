# CGX-11 — Functional / Smoke Testing Checklist

> ⚠️ This system controls a live 16kW 3-phase ESS. Run phases 0–2 off-hardware
> first. Only enable on the live system (phase 3+) during a low-risk window and
> keep a way to revert (`AI_POWERED_ESS_ALGORITHM=False`).

---

## Phase 0 — Pre-flight (no hardware)

- [ ] `.env` created from `.env.example`; new keys present: `ESS_MAX_GRID_IMPORT_KW`,
      `ESS_MAX_GRID_EXPORT_KW`, `ESS_MAX_CHARGE_KW`, `ESS_MAX_DISCHARGE_KW`,
      `ESS_EXPORT_PRICE_FACTOR`, `ESS_EXPORT_FEE`, `ESS_TERMINAL_VALUE_FACTOR`,
      `NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED`.
- [ ] `.secrets` present with Tibber + VRM credentials.
- [ ] `pip install -r requirements.txt` succeeds (confirm `python-dateutil` available).
- [ ] Modules import without error:
      `python -c "import lib.ai_powered_ess, lib.energy_broker, lib.victron_integration"`
- [ ] Import does NOT crash when a setting is blank/missing (regression for the
      import-time `float()` bug): temporarily blank `MAX_TIBBER_BUY_PRICE` and
      re-run the import above — it should still load.

## Phase 1 — Unit tests

- [ ] `export DEV=1 && pytest -q` — full suite green.
- [ ] `pytest -q tests/test_ai_powered_ess.py` specifically passes, including:
  - [ ] `test_iso_string_timestamps_do_not_crash` (string `start` handled)
  - [ ] `test_hourly_slot_duration_detected` (durations are multiples of 3600s)
  - [ ] `test_negative_price_sets_feed_in_limit_flag`
  - [ ] `test_terminal_value_prevents_end_of_horizon_dump`
  - [ ] `test_victron_slots_limit` (≤ 5 slots)
- [ ] `pytest -q tests/test_energy_broker.py` passes (nightly skip logic intact).

## Phase 2 — Optimizer logic smoke (no hardware)

- [ ] `python scripts/ai_ess_dryrun.py` (read-only) prints the four-section plan
      using live data. Try `--soc 1`, `--soc 50`, `--soc 90` and confirm the plan
      and the NET GRID COST / PROFIT line make sense.
- [ ] Confirm the per-slot `mode` column shows the four modes correctly:
      `BUY`, `SELL`, `HOLD`, `SELF-SUPPLY`. In particular, a full battery exporting
      only PV surplus should read `SELL` (reason PV_SURPLUS_BATTERY_FULL), and a
      held battery whose load is covered by grid/PV should read `HOLD`. Check the
      `Reason:` line matches the situation.
- [ ] With `ESS_MIN_SELL_PRICE` set above the day's high, the dry run shows no
      `discharge` slots (battery not sold cheaply); PV feed-in still allowed.
- [ ] With `ESS_EXPECTED_PEAK_PRICE` set high, the plan holds more charge / uses
      `grid_assist` instead of selling into a low intra-day high.
- [ ] Set `OPTIMIZER_SLOT_MINUTES=15` and confirm the per-slot plan has ~4x the
      rows (sub-slots) and Victron charge durations are multiples of 15 min.
- [ ] Self-consumption: the dry run header prints "forecast house load (horizon)"
      > 0, and the per-slot SoC now declines through the afternoon/evening from
      self-usage (battery not left at 100% into the evening peak unrealistically).
- [ ] Tibber 15-min prices: with `TIBBER_PRICE_RESOLUTION=QUARTER_HOURLY`, the dry
      run shows ~96 price points/day ("plan slot ~0.25h"); confirm the service log
      shows quarter-hourly data (or the documented hourly fallback if your market
      doesn't yet provide it).
- [ ] `python scripts/backtest_ess.py` runs to completion, prints charge slots and
      a `SoC start -> end` per-step table (no `KeyError: 'soc'`).
- [ ] Sanity-check the plan in a Python shell with crafted prices:
  - [ ] **Cheap-night / expensive-evening** curve → charge slots land in the cheap
        window; current setpoint is `0` (charge) or export setpoint (discharge).
  - [ ] **All-negative prices** → `result['limit_feed_in'] is True`.
  - [ ] **Flat price, start SoC 90%** → end-of-horizon `soc_end` stays well above
        the reserve (terminal value working), not dumped to `min_soc`.
  - [ ] **Hourly Tibber-shaped data** (24–48 points) → every `victron_slots[i]['duration']`
        is a multiple of 3600 and `target_soc` ≤ 100.
  - [ ] Empty / all-past price list → returns `None`, no exception.

## Phase 3 — Live integration, AI disabled (baseline)

- [ ] `AI_POWERED_ESS_ALGORITHM=False`. Start service; confirm legacy
      `set_charging_schedule` (09:30 / 21:30) and hourly `manage_sale_of_stored_energy_to_the_grid`
      still run and are unaffected.
- [ ] Confirm no writes to `.../CGwacs/MaxFeedInPower` occur while disabled.

## Phase 4 — Live integration, AI enabled

- [ ] Set `AI_POWERED_ESS_ALGORITHM=True`; ensure Tibber price feed is publishing
      to `Tibber/home/price_info/all` (set `TIBBER_UPDATES_ENABLED=1`).
- [ ] First 15-min cycle: logs show `AI_ESS: Optimization complete. Action: ...`
      with NO `start.tzinfo` / normalisation error (the original crash).
- [ ] `STATE['ai_success_timestamp']` updates each cycle.
- [ ] Victron charge schedule slots (`.../Schedule/Charge/0..4/`) are written with:
  - [ ] whole-hour `Duration` (3600 × n),
  - [ ] correct `Start` (seconds from midnight), `Day` (weekday), and a
        per-slot `Soc` target (not always 100).
- [ ] AC setpoint (`CGwacs/AcPowerSetPoint`) matches the current action
      (export setpoint when discharging, 0 otherwise).
- [ ] Legacy path correctly *skips* while AI is healthy (log: "AI Optimizer is
      active and healthy. Skipping legacy ...").
- [ ] Each run logs the full plan (`AI_ESS: Optimization complete.` followed by the
      plan table) — confirm it matches the dry-run output and that re-running later
      shows the plan changing as prices/SoC change.
- [ ] When the current slot is `grid_assist`, confirm `grid_charging_enabled` and
      `ess_net_metering_overridden` are set and the grid setpoint tracks house load
      on `ac_out_power` events (battery held). Confirm it clears when the next slot
      is not `grid_assist` (`STATE['ai_grid_assist']` flips off, only on change).

## Phase 5 — Negative-price feed-in protection

- [ ] With `NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED=True`, when the current price is
      negative: `MaxFeedInPower` is set to `0`, log shows "Limiting system grid
      feed-in to 0W", and `STATE['feed_in_limit_state'] == "limited:0"`.
- [ ] Confirm on the Cerbo GUI that **Settings → ESS → Grid feed-in → "Limit
      system feed-in"** is ON and the limit reads 0W.
- [ ] When price returns ≥ 0: `MaxFeedInPower` reverts to `-1`, log shows
      "Restored unlimited system grid feed-in", `feed_in_limit_state == "unlimited"`.
- [ ] Idempotency: across consecutive cycles with the same price sign, no
      repeated `MaxFeedInPower` writes occur.
- [ ] With the flag `False`, no feed-in writes occur regardless of price.

## Phase 6 — 13:05 next-day re-plan

- [ ] At 13:05 local, `run_daily_price_update_and_optimize` fires: pricing refresh
      log followed by "running optimizer over 48h horizon".
- [ ] After tomorrow's prices publish, the plan spans the day boundary (charge/
      discharge slots scheduled for tomorrow when cheaper/dearer than today).
- [ ] Verify behaviour both *before* ~13:00 (24h horizon, today only) and *after*
      (48h horizon, today + tomorrow).

## Phase 7 — Fallback & resilience

- [ ] Stall the optimizer (e.g. block the Tibber feed) for > 1h → legacy logic
      resumes (log: "AI Optimizer enabled but stale ... fallback").
- [ ] Kill price data mid-run → `run_ai_optimizer` logs a warning and returns
      without touching setpoints/schedules.
- [ ] Restart the broker → confirm no stale retained `MaxFeedInPower` command
      re-applies an unexpected limit (we publish with `retain=False`).

## Phase 8 — Rollback verification

- [ ] Set `AI_POWERED_ESS_ALGORITHM=False` and confirm the system returns to
      legacy scheduling within one cycle.
- [ ] Manually confirm feed-in is unlimited (`MaxFeedInPower = -1`) after disabling.

---

### Quick reference — what to watch
| Concern | Where to look |
|---|---|
| Optimizer ran | log `AI_ESS: Optimization complete...`, `STATE['ai_success_timestamp']` |
| Charge slots | MQTT `.../CGwacs/BatteryLife/Schedule/Charge/0..4/*` |
| Setpoint | MQTT `.../CGwacs/AcPowerSetPoint` |
| Feed-in limit | MQTT `.../CGwacs/MaxFeedInPower`, `STATE['feed_in_limit_state']` |
| PV forecast input | `STATE['pv_projected_remaining']` |
| Prices feeding AI | `Tibber/home/price_info/all` |
