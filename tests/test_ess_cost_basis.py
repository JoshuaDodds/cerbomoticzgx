"""Unit tests for the persistent battery cost-basis tracker (lib/ess_cost_basis.py).

The tracker maintains the weighted €/kWh actually paid for the DC energy now in
the battery so the optimizer never sells stored energy below cost. Energy is
always derived from the measured SoC (robust to manual SoC changes); only the
basis is stateful.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lib.ess_cost_basis as cb  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    """Point the tracker at a throwaway file."""
    monkeypatch.setattr(cb, "_path", lambda: str(tmp_path / "cb.json"))


def test_empty_battery_has_no_basis(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert cb.current_basis() == 0.0
    assert cb.sell_floor(0.90) == 0.0


def test_grid_charge_sets_basis(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # 0% -> 10% of a 45 kWh pack = 4.5 kWh DC. At 90% charge efficiency that needs
    # 5.0 kWh AC; all 5 kWh came from the grid at €0.30 => €1.50 for 4.5 kWh DC.
    cb.update_from_slot(soc_start=0.0, soc_end=10.0, capacity_kwh=45.0,
                        import_kwh=5.0, pv_kwh=0.0, price_buy=0.30,
                        charge_efficiency=0.90)
    assert abs(cb.current_basis() - (1.5 / 4.5)) < 1e-4          # €0.3333/kWh DC
    # Sell floor recovers cost after discharge losses: basis / discharge_eff.
    assert abs(cb.sell_floor(0.90) - (1.5 / 4.5) / 0.90) < 1e-4  # €0.3704/kWh AC


def test_pv_charge_dilutes_basis(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cb.update_from_slot(soc_start=0.0, soc_end=10.0, capacity_kwh=45.0,
                        import_kwh=5.0, pv_kwh=0.0, price_buy=0.30,
                        charge_efficiency=0.90)
    # Next 10% all from PV (no import) -> free energy halves the basis.
    cb.update_from_slot(soc_start=10.0, soc_end=20.0, capacity_kwh=45.0,
                        import_kwh=0.0, pv_kwh=6.0, price_buy=0.30,
                        charge_efficiency=0.90)
    assert abs(cb.current_basis() - (1.5 / 9.0)) < 1e-4          # €0.1667/kWh DC


def test_discharge_keeps_basis_until_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cb.update_from_slot(soc_start=0.0, soc_end=20.0, capacity_kwh=45.0,
                        import_kwh=10.0, pv_kwh=0.0, price_buy=0.30,
                        charge_efficiency=0.90)
    basis = cb.current_basis()
    # Partial discharge: per-kWh basis unchanged.
    cb.update_from_slot(soc_start=20.0, soc_end=10.0, capacity_kwh=45.0,
                        import_kwh=0.0, pv_kwh=0.0, price_buy=0.30,
                        charge_efficiency=0.90)
    assert abs(cb.current_basis() - basis) < 1e-6
    # Drain to empty: basis resets so an empty pack imposes no floor.
    cb.update_from_slot(soc_start=10.0, soc_end=0.0, capacity_kwh=45.0,
                        import_kwh=0.0, pv_kwh=0.0, price_buy=0.30,
                        charge_efficiency=0.90)
    assert cb.current_basis() == 0.0


def test_manual_soc_injection_is_treated_as_free(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # A manual jump from 0 -> 4% with no grid import (energy of unknown/free
    # provenance) must NOT create a high floor that blocks selling it.
    cb.update_from_slot(soc_start=0.0, soc_end=4.0, capacity_kwh=45.0,
                        import_kwh=0.0, pv_kwh=0.0, price_buy=0.30,
                        charge_efficiency=0.90)
    assert cb.current_basis() == 0.0
    assert cb.sell_floor(0.90) == 0.0
