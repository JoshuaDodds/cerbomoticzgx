# Plan: standardize the 4-state action model + per-slot settlement

Two steps, implemented in one pass after review.

- **Step 1** — one canonical control-action label (IDLE / RETAIN / BUY / SELL) derived
  from the *commanded setpoint*, used identically in the console, the web UI, the
  plan JSON, and the history log. Kills the "console says SELL, web says SOLAR"
  disagreement.
- **Step 2** — when a quarter-hour closes, record what *actually* happened that slot
  (actual import/export/SoC/€) next to what we *predicted*, for forecast-accuracy
  learning and the future timeline view.

---

## The model

Label by **what we command**, not by predicted energy flow.

| State | Meaning | Commanded setpoint |
|---|---|---|
| **IDLE** | Hands off — Victron's own ESS logic runs: self-consume, charge surplus PV, export when full, import when empty. Actual flow only known *retroactively*. | neutral (0 W, net-metering not overridden) |
| **RETAIN** | Force the grid to cover the house load so the battery is **not** discharged (grid-assist). | import = max(0, load − PV) |
| **BUY** | Force charge from the grid. | grid-charge schedule slot |
| **SELL** | Force discharge to the grid (export stored energy). | negative export setpoint |

### How today's internal `action` (buy/sell/hold/self_supply) maps to the label

The DP keeps its internal `action` (it needs charge/discharge for cost scoring and
for grouping Victron charge slots). We add a **derived** `control_action` purely for
applying + display:

```
def control_action(step):           # step has action, grid_energy, soc_start, soc_end
    if step.action == 'buy':                         return 'BUY'
    if step.action == 'sell' and soc_falling(step):  return 'SELL'   # real discharge to grid
    if step.action == 'hold' and grid_energy > EPS:  return 'RETAIN' # forced import to hold battery
    return 'IDLE'                                     # pv-surplus, self-supply, pv-covers-load hold
```

- PV-surplus "fake sells" (SoC flat, tiny export) → **IDLE** ✅ (the current bug)
- self-supply (battery powers loads, neutral setpoint) → **IDLE**
- HOLD that imports to cover load → **RETAIN**; HOLD where PV already covers load → **IDLE**
- real battery discharge to grid → **SELL**; grid charge → **BUY**

The **current/applied** slot's label is set in `run_ai_optimizer` from what it actually
commands (grid-assist import → RETAIN, neutral → IDLE, negative setpoint → SELL,
charge → BUY), so the live label always matches the real setpoint even when live PV
differs from the forecast.

### Display policy for IDLE

IDLE flow is not commanded, so its forecast import/export/€ is a *projection*. In all
surfaces IDLE per-slot numbers render muted as **projected** (generalizes today's
"stored" treatment). The **committed net** (headline €) sums only BUY/RETAIN/SELL
flows — the deliberate actions; IDLE is shown as a separate projected line. Past slots
always show the settled actuals (Step 2).

---

## Step 1 — files & changes

1. **`lib/ai_powered_ess.py`**
   - Add a `control_action(step)` helper (single source of truth) + `CONTROL_LABELS`.
   - `_post_process`: attach `step['control_action']` to every slot; set
     `result['control_action']` for the current slot. Keep `mode`/`action`/`reason_code`
     for internal/DP use.
   - `format_plan_summary` (console): per-slot table + IMMEDIATE DECISION + Battery line
     use `control_action`; replace the old `MODE_LABELS` mapping. IDLE rows show
     `projected` instead of a grid-kWh that implies a commitment.
   - `_explain_action`: keep the reason text; ensure it never says "Exporting…" for IDLE.
2. **`lib/energy_broker.py`**
   - `run_ai_optimizer`: derive the **applied** `control_action` from the branch it
     takes (RETAIN/IDLE/BUY/SELL); publish `STATE['ai_control_action']` (keep `ai_mode`
     for back-compat); pass it to history. Ensure `_publish_plan_json` serialises
     `control_action` per slot.
3. **`frontend/static/js/app.js`**
   - Replace `MODE_LABEL` / `modeChip` / `slotLabel` / `isPvSurplus` / `isStoredSurplus`
     with one `control_action`-driven chip + colour. IDLE slot import/export/net render
     muted "projected". Now-card chip + battery descriptor follow `control_action`.
4. **`frontend/templates/index.html`** — legend → IDLE / RETAIN / BUY / SELL.
5. **`frontend/static/css/app.css`** — colour vars: `mode-idle` (grey), `mode-retain`
   (amber), `mode-buy` (blue), `mode-sell` (green). Drop `sell-pv`.
6. **`frontend/data.py`** — `group_by_hour` / `day_summary` key off `control_action`;
   committed net excludes IDLE (projected bucket renamed from `unrealized_solar_*` →
   `projected_idle_*`).
7. **`tests/test_ai_powered_ess.py`** — add `control_action` mapping tests; update the
   PV-surplus test to assert IDLE (not SELL); replace the `_post_process` setpoint tests'
   label expectations.
8. **Docs** — refresh `PR_DESCRIPTION_CGX-11.md` + README action-model section.

---

## Step 2 — per-slot settlement

**Goal:** at each quarter-hour boundary, emit one record pairing the *prediction we made
for the slot that just closed* with *what actually happened*.

**How we get the actuals (no new hardware reads):** diff the cumulative daily counters we
already snapshot each cycle (`day_import_kwh/_cost`, `day_export_kwh/_reward`) plus the
MPPT daily yields and SoC, between the previous cycle and this one. The optimizer is
clock-aligned to :00/:15/:30/:45, so each run closes exactly the prior slot.

**Mechanism:**
- Persist a tiny "last cycle" snapshot (prediction for slot[0] + the counter values +
  SoC + ts) — in `/dev/shm` (JSON) so it survives across cycles but not reboots.
- At the top of `run_ai_optimizer` (after the realized-power snapshot, before planning),
  call `_settle_prior_slot()`:
  - `actual_import = day_import_kwh − last.day_import_kwh` (etc.); **guard the midnight
    reset** (counter goes down → skip/clip) and **gaps** (ts delta > ~20 min → mark
    `incomplete: true`).
  - `actual_soc_delta = soc − last.soc`; `actual_pv_kwh = (c1+c2) − last.(c1+c2)`.
  - `predicted_*` come from `last.prediction` (that slot's plan row: control_action,
    grid_energy, net €).
  - Write one line to `data/history/ess-settle-YYYY-MM-DD.ndjson`.
- Then store this cycle's snapshot for next time.

**Settlement record fields:**
`slot_start, slot_end, incomplete, predicted_control_action, predicted_import_kwh,
predicted_export_kwh, predicted_net_eur, actual_import_kwh, actual_export_kwh,
actual_cost, actual_reward, actual_net_eur, soc_start, soc_end, soc_delta,
actual_pv_kwh, price_buy, price_sell`.

**Files:**
- `lib/energy_broker.py` — `_settle_prior_slot()` + snapshot persistence; call from
  `run_ai_optimizer`. Best-effort (wrapped, never affects control).
- `scripts/history_report.py` — add a predicted-vs-actual section: net-€ MAE, PV forecast
  bias, load/consumption bias, per-day.
- `tests/` — unit-test the diff/midnight-reset/gap logic with synthetic snapshots.

This is the data backbone for the roadmap timeline (historical actuals + forward plan).

---

## Decisions to confirm before I build

1. **Committed-net policy:** headline € counts only BUY/RETAIN/SELL; IDLE shown as a
   separate "projected" line. OK, or would you rather the headline keep including IDLE's
   projection as today?
2. **Settlement source:** counter-diff (no new reads, proposed) vs. sampling Victron
   energy registers directly at the boundary (more precise, more code). Start with
   counter-diff?
3. **Settlement file:** separate `ess-settle-*.ndjson` (proposed) vs. a `kind` field in
   the existing per-cycle file.
4. **`RETAIN` vs `IDLE` naming** — keep `RETAIN`, or prefer `GRID-ASSIST` / `HOLD` /
   something else for that chip?
