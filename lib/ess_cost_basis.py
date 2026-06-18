"""Persistent battery cost-basis tracker.

The AI ESS optimizer is a stateless MPC: every 15-minute cycle it rebuilds the
plan from scratch using only the *current* SoC and the *forward* price curve. It
has no memory of what was paid for the energy already sitting in the battery, so
on a flat-but-high morning it will happily dump a freshly (or expensively)
charged battery for a few cents of intra-day "spread" and then re-import to cover
load — a round-trip loss.

This module maintains a small, persisted weighted-average **cost basis** (the €/kWh
actually paid for the DC energy currently stored) that survives re-plans and
service restarts. The optimizer reads it and refuses to *actively* discharge the
battery to the grid below that basis (plus losses), so it can never sell stored
energy for less than it cost to put there. PV-charged energy is treated as free,
so the floor naturally relaxes once the battery is solar-filled.

Design notes
------------
* Energy is always **derived from the measured SoC** at settlement time, never
  integrated independently — so a manual SoC change (or sensor jump) can't make
  the tracker drift out of sync with reality. Only the *basis* (€/kWh) is stateful.
* Charging cost is attributed PV-first: grid imports that coincide with a charge
  raise the basis; PV charging dilutes it toward zero. The grid attribution is
  deliberately conservative (biases the basis slightly upward) so the floor errs
  toward *not* selling at a loss — the safe direction for a critical system.
* Best-effort throughout: any failure logs and leaves control unaffected.
"""
import json
import os

from lib.config_retrieval import retrieve_setting
from lib.constants import logging

_EPS = 1e-6
_DEFAULT_PATH = "data/ess_cost_basis.json"


def _path() -> str:
    return retrieve_setting("ESS_COST_BASIS_PATH") or _DEFAULT_PATH


def load_state() -> dict:
    """Return ``{'basis': <eur/kWh DC>, 'energy_kwh': <kWh>, 'updated': <iso>}``.

    Missing/corrupt file -> a zeroed state (no basis, no stored energy).
    """
    try:
        with open(_path()) as fh:
            s = json.load(fh)
        return {
            "basis": float(s.get("basis", 0.0) or 0.0),
            "energy_kwh": float(s.get("energy_kwh", 0.0) or 0.0),
            "updated": s.get("updated"),
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return {"basis": 0.0, "energy_kwh": 0.0, "updated": None}


def save_state(state: dict) -> None:
    """Atomically persist the cost-basis state (write-then-rename)."""
    path = _path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError as e:
        logging.warning(f"ESS cost-basis: failed to persist state: {e}")


def current_basis() -> float:
    """Current weighted-average cost basis (€/kWh of DC energy stored).

    Returns 0.0 when the battery holds no tracked energy (nothing to protect).
    """
    s = load_state()
    if s["energy_kwh"] <= _EPS:
        return 0.0
    return max(0.0, s["basis"])


def sell_floor(discharge_efficiency: float = 0.90) -> float:
    """Minimum sell price (€/kWh AC) needed to recover what the stored energy cost.

    Selling 1 kWh of DC energy yields ``discharge_efficiency`` kWh of AC revenue,
    so break-even on cost recovery is ``basis / discharge_efficiency``. Returns 0.0
    when there's no basis to protect. The optimizer adds its own profit cushion
    (cycle_cost + arbitrage_margin) on top via the discharge hurdle, so this is a
    pure cost-recovery floor.
    """
    basis = current_basis()
    if basis <= _EPS:
        return 0.0
    eff = discharge_efficiency if discharge_efficiency and discharge_efficiency > _EPS else 0.90
    return basis / eff


def update_from_slot(*, soc_start, soc_end, capacity_kwh, import_kwh,
                     pv_kwh, price_buy, charge_efficiency=0.90) -> dict:
    """Update the cost basis from one settled slot's measured outcome.

    :param soc_start/soc_end: measured battery SoC (%) at slot open/close.
    :param capacity_kwh: usable battery capacity (kWh).
    :param import_kwh: grid energy imported during the slot (kWh, >=0).
    :param pv_kwh: PV energy produced during the slot (kWh, >=0). (Used implicitly:
                   any charge not covered by grid import is treated as PV/free.)
    :param price_buy: buy price during the slot (€/kWh).
    :param charge_efficiency: AC->DC charge efficiency.
    :returns: the new persisted state dict.

    Charging raises the basis by the *grid* cost of the energy added (PV-first
    attribution; PV is free). Discharging leaves the per-kWh basis unchanged
    (weighted average) and zeroes it once the battery empties.
    """
    try:
        cap = float(capacity_kwh)
        ss = float(soc_start)
        se = float(soc_end)
    except (TypeError, ValueError):
        return load_state()
    if cap <= _EPS:
        return load_state()

    e_start = max(0.0, ss) / 100.0 * cap
    dc_delta = (se - ss) / 100.0 * cap

    state = load_state()
    basis = max(0.0, state.get("basis", 0.0))

    ceff = charge_efficiency if charge_efficiency and charge_efficiency > _EPS else 0.90
    imp = max(0.0, float(import_kwh or 0.0))
    pbuy = max(0.0, float(price_buy or 0.0))

    if dc_delta > _EPS:
        # Charging. AC energy needed to store dc_delta of DC energy:
        ac_into_batt = dc_delta / ceff
        # Conservative PV-first split: imports that coincided with the charge are
        # attributed to the battery (up to what it absorbed); the rest is PV/free.
        grid_charge_ac = min(imp, ac_into_batt)
        cost_added = grid_charge_ac * pbuy
        e_new = e_start + dc_delta
        if e_start <= _EPS:
            basis = cost_added / dc_delta if dc_delta > _EPS else 0.0
        else:
            basis = (e_start * basis + cost_added) / e_new
        energy = e_new
    elif dc_delta < -_EPS:
        # Discharging. Weighted-average basis per kWh is unchanged; just track the
        # remaining energy and reset the basis once effectively empty.
        energy = max(0.0, e_start + dc_delta)
        if energy <= _EPS:
            basis = 0.0
    else:
        energy = e_start  # flat

    # Energy is authoritative from the measured end-SoC (keeps us in sync with a
    # manual SoC change rather than drifting on integrated deltas).
    energy = max(0.0, se / 100.0 * cap)
    if energy <= _EPS:
        basis = 0.0

    new_state = {"basis": round(basis, 6), "energy_kwh": round(energy, 4)}
    try:
        from datetime import datetime
        new_state["updated"] = datetime.now().astimezone().isoformat()
    except Exception:
        new_state["updated"] = None
    save_state(new_state)
    return new_state
