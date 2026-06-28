"""Regression tests for the grid-vs-PV charging trade-off.

Motivated by 2026-06-22: the battery filled to 100% (grid + PV) by ~15:00, then PV
surplus exported cheaply for hours. The worry: was grid charging blindly crowding out
free PV and inflating the cost basis?

The optimizer's DP already nets PV against load (``net_load = load - pv``) and prices
export revenue (``step_cost = import*buy - export*sell``), so the *effective* cost of
charging from PV is the export price it forgoes. The DP therefore must:

  * LEAVE ROOM for PV (not grid-charge) when the afternoon export price is BELOW the
    grid-charge price — storing free PV is cheaper than buying grid; and
  * grid-charge when the grid price is BELOW the PV-export price — buying cheap grid
    and exporting the PV at the higher price beats storing the PV.

These tests pin that adaptive behaviour so a future change can't silently turn it into
a blind "always precharge from grid".
"""
import unittest
from datetime import datetime, timedelta
from dateutil import tz

from lib.ai_powered_ess import OptimizationEngine


def _engine():
    e = OptimizationEngine()
    e.battery_capacity = 10.0          # small pack: one slot of PV can fill it
    e.charge_efficiency = 0.95
    e.discharge_efficiency = 0.95
    e.min_soc = 0.0
    e.export_price_factor = 1.0
    e.export_fee = 0.0
    e.terminal_value_factor = 1.0
    e.expected_peak_price = 0.0
    e.min_sell_price = 0.0
    e.cycle_cost = 0.0
    e.arbitrage_margin = 0.0
    e.max_grid_charge_price = 0.0      # ceilings off — exercise the pure cost trade-off
    e.grid_charge_cheap_pct = 0.0
    e.slot_minutes = 60.0
    e.max_charge_power = 20.0
    e.max_discharge_power = 20.0
    e.max_power_import = 20.0
    e.max_power_export = 20.0
    return e


def _prices(totals):
    base = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0)
    return [{'start': base + timedelta(hours=i), 'total': t, 'level': 'NORMAL'}
            for i, t in enumerate(totals)]


def _total_import_kwh(result):
    return sum(max(0.0, s.get('grid_energy', 0.0)) for s in result['schedule'])


class TestPVvsGridCharge(unittest.TestCase):
    def test_leaves_room_for_pv_when_export_cheaper_than_grid(self):
        # Grid @0.10 is MORE expensive than the slot-1 PV-export price @0.02, so storing
        # the free PV (forgoing a 0.02 export) is cheaper than buying grid. The optimiser
        # must NOT grid-charge — PV fills the battery in slot 1 for the 0.50 evening peak.
        e = _engine()
        prices = _prices([0.10, 0.02, 0.50, 0.50])
        pv = [0.0, 15.0, 0.0, 0.0]     # large PV surplus in slot 1 only
        load = [0.0, 0.0, 0.0, 0.0]
        res = e.optimize(0.0, prices, load, pv)
        self.assertIsNotNone(res)
        self.assertLess(_total_import_kwh(res), 2.0,
                        "Expected the pack to fill from PV, not grid, when the PV-export "
                        "price is below the grid-charge price.")

    def test_grid_charges_when_grid_cheaper_than_export(self):
        # Same PV, but the slot-1 export price is now 0.30 — ABOVE grid @0.10. Buying
        # cheap grid and exporting the PV at 0.30 beats storing the PV, so the optimiser
        # SHOULD grid-charge in slot 0.
        e = _engine()
        prices = _prices([0.10, 0.30, 0.50, 0.50])
        pv = [0.0, 15.0, 0.0, 0.0]
        load = [0.0, 0.0, 0.0, 0.0]
        res = e.optimize(0.0, prices, load, pv)
        self.assertIsNotNone(res)
        self.assertGreater(_total_import_kwh(res), 4.0,
                           "Expected grid charging when the grid price is below the "
                           "PV-export price.")


if __name__ == '__main__':
    unittest.main()
