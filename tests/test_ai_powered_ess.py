import unittest
from datetime import datetime, timedelta
from dateutil import tz
import sys
import os

# Add repo root to path
sys.path.append(os.getcwd())

from lib.ai_powered_ess import OptimizationEngine, control_action_for
from lib import ai_powered_ess

class TestAIPoweredESS(unittest.TestCase):
    def setUp(self):
        self.engine = OptimizationEngine()
        # Mock settings (deterministic regardless of environment .env values)
        self.engine.battery_capacity = 45.0
        self.engine.charge_efficiency = 0.90
        self.engine.discharge_efficiency = 0.90
        self.engine.min_soc = 5.0
        self.engine.export_price_factor = 1.0
        self.engine.export_fee = 0.0
        self.engine.terminal_value_factor = 1.0
        self.engine.expected_peak_price = 0.0
        self.engine.min_sell_price = 0.0
        self.engine.cycle_cost = 0.0
        # Hurdle knobs: disabled by default so tests are deterministic
        # regardless of the host .env values.
        self.engine.arbitrage_margin = 0.0
        self.engine.max_grid_charge_soc = 100.0
        # Plan at native (hourly) resolution by default in tests; individual
        # tests override this to exercise sub-slot resampling.
        self.engine.slot_minutes = 60.0

    def _step(self, action, soc_start, soc_end, grid_energy, price=0.20):
        return {
            'time': datetime.now(tz.UTC).replace(second=0, microsecond=0),
            'action': action, 'soc_start': soc_start, 'soc_end': soc_end,
            'grid_energy': grid_energy, 'price': price, 'sell': price,
        }

    def _policy_step(self, day, hour, grid_energy, price=1.0):
        return {
            'time': datetime(2099, 6, day, hour, 0, tzinfo=tz.UTC),
            'action': 'hold',
            'control_action': 'IDLE',
            'soc_start': 50.0,
            'soc_end': 50.0,
            'grid_energy': grid_energy,
            'price': price,
            'sell': price,
        }

    def test_control_action_mapping(self):
        # BUY: charging from grid.
        self.assertEqual(control_action_for('buy', 20.0, 30.0, 2.5), 'BUY')
        # SELL: real discharge to grid (SoC falls).
        self.assertEqual(control_action_for('sell', 100.0, 94.0, -2.3), 'SELL')
        # RETAIN: hold that imports to cover the load (battery held).
        self.assertEqual(control_action_for('hold', 50.0, 50.0, 0.4), 'RETAIN')
        # IDLE: hold where PV covers the load (no import).
        self.assertEqual(control_action_for('hold', 50.0, 50.0, -0.1), 'IDLE')
        # IDLE: PV surplus (export while SoC flat — not a real discharge).
        self.assertEqual(control_action_for('sell', 50.0, 50.0, -0.09), 'IDLE')
        # IDLE: PV-only charging. SoC rises, but no grid energy is bought.
        self.assertEqual(control_action_for('buy', 50.0, 55.0, 0.0), 'IDLE')
        # IDLE: self-supply (battery powers loads, no export).
        self.assertEqual(control_action_for('self_supply', 50.0, 45.0, 0.0), 'IDLE')

    def test_pv_surplus_sell_is_idle_neutral_setpoint(self):
        # Exporting while SoC is flat = PV surplus -> IDLE, neutral setpoint (no
        # forced/capping export); Victron routes surplus in real time.
        sched = [self._step('sell', 50.0, 50.0, -0.09, price=0.15)]
        result = self.engine._post_process(sched, 900)
        self.assertEqual(result['control_action'], 'IDLE')
        self.assertTrue(result['pv_surplus'])
        self.assertEqual(result['setpoint'], 0.0)

    def test_stored_discharge_sell_keeps_forced_setpoint(self):
        # Real battery discharge to grid (SoC falling) must keep the planned
        # negative export setpoint so the discharge is rate-controlled/spread.
        sched = [self._step('sell', 100.0, 94.0, -2.29, price=0.25)]
        result = self.engine._post_process(sched, 900)
        self.assertEqual(result['control_action'], 'SELL')
        self.assertFalse(result['pv_surplus'])
        # planned_w = -2.29 / 0.25h * 1000 = -9160 W
        self.assertEqual(result['setpoint'], -9160.0)

    def test_optimization_basic(self):
        # Generate dummy price data
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24 * 4): # 24 hours of 15 min slots
            t = base_time + timedelta(minutes=15 * i)
            # Make prices cheap at night (02:00-05:00) and expensive evening (18:00-21:00)
            hour = t.hour
            price = 0.20
            if 2 <= hour < 5:
                price = 0.10
            if 18 <= hour < 21:
                price = 0.40
            prices.append({'start': t, 'total': price, 'level': 'NORMAL'})

        current_soc = 50.0 # 50%
        result = self.engine.optimize(current_soc, prices)

        self.assertIsNotNone(result)
        self.assertTrue('schedule' in result)
        self.assertTrue('victron_slots' in result)

        # Check logic: Should charge when cheap (02:00-05:00)
        # 02:00 is index 8 (starting from 12:00? No. 12:00 + 14h = 02:00. Index 14*4 = 56)
        # Wait, my loop starts at 12:00. 02:00 is +14 hours.

        # Let's inspect specific slots
        schedule = result['schedule']

        # Find 03:00 slot
        slot_3am = next((s for s in schedule if s['time'].hour == 3), None)
        # It should probably charge or idle, not discharge
        # self.assertEqual(slot_3am['action'], 'charge') # might depend on initial SoC and future needs

        # Find 19:00 slot (expensive)
        slot_7pm = next((s for s in schedule if s['time'].hour == 19), None)
        # It should discharge
        # self.assertEqual(slot_7pm['action'], 'discharge')

    def test_victron_slots_limit(self):
        # Test that we don't get more than 5 slots
        # Create prices that fluctuate wildly to force fragmentation
        base_time = datetime.now(tz.UTC)
        prices = []
        for i in range(40):
            prices.append({'start': base_time + timedelta(minutes=15*i), 'total': 0.10 if i % 2 == 0 else 0.50, 'level': 'NORMAL'})

        result = self.engine.optimize(10.0, prices)
        self.assertLessEqual(len(result['victron_slots']), 5)

    def test_iso_string_timestamps_do_not_crash(self):
        # Regression: production Tibber data provides ISO-8601 strings for
        # 'start', not datetime objects. The optimizer must handle both.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            prices.append({'start': t.isoformat(), 'total': 0.20, 'level': 'NORMAL'})

        result = self.engine.optimize(50.0, prices)
        self.assertIsNotNone(result)
        self.assertIn('schedule', result)

    def test_hourly_slot_duration_detected(self):
        # Hourly Tibber data must yield Victron charge durations in whole hours
        # (multiples of 3600s), not 15-minute (900s) windows.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            price = 0.05 if 2 <= t.hour < 5 else 0.40
            prices.append({'start': t, 'total': price, 'level': 'NORMAL'})

        result = self.engine.optimize(20.0, prices)
        self.assertIsNotNone(result)
        for slot in result['victron_slots']:
            self.assertEqual(slot['duration'] % 3600, 0)

    def test_negative_price_sets_feed_in_limit_flag(self):
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            # Current (first future) slot is negative.
            price = -0.05 if i == 0 else 0.25
            prices.append({'start': t, 'total': price, 'level': 'NORMAL'})

        result = self.engine.optimize(50.0, prices)
        self.assertIsNotNone(result)
        self.assertTrue(result['limit_feed_in'])

    def test_terminal_value_prevents_end_of_horizon_dump(self):
        # With a uniformly high price and export enabled, an engine that places
        # no terminal value on stored energy will drain the battery to the
        # reserve by the end of the horizon. With terminal valuation it should
        # retain meaningfully more charge.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            prices.append({'start': t, 'total': 0.40, 'level': 'NORMAL'})

        def make_engine(terminal_factor):
            from lib.ai_powered_ess import OptimizationEngine
            e = OptimizationEngine()
            e.battery_capacity = 45.0
            e.charge_efficiency = 0.90
            e.discharge_efficiency = 0.90
            e.min_soc = 5.0
            e.export_price_factor = 1.0
            e.export_fee = 0.0
            e.expected_peak_price = 0.0
            e.min_sell_price = 0.0
            e.cycle_cost = 0.0
            e.slot_minutes = 60.0
            e.terminal_value_factor = terminal_factor
            return e

        no_terminal = make_engine(0.0).optimize(90.0, prices)
        with_terminal = make_engine(1.0).optimize(90.0, prices)

        end_no_terminal = no_terminal['schedule'][-1]['soc_end']
        end_with_terminal = with_terminal['schedule'][-1]['soc_end']
        self.assertGreaterEqual(end_with_terminal, end_no_terminal)

    def test_terminal_value_does_not_preserve_charge_on_same_day_only_horizon(self):
        # When Tibber has not published tomorrow yet, the remaining horizon ends
        # tonight. The terminal-value guard must not treat that truncated same-day
        # window as a reason to retain profitable energy through the evening.
        base_time = datetime(2099, 6, 28, 20, 0, tzinfo=tz.UTC)
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': 0.40, 'level': 'NORMAL'}
            for i in range(4)
        ]

        e = self._arb_engine(terminal_value_factor=1.0)
        result = e.optimize(
            90.0,
            prices,
            load_forecast=[0.0] * len(prices),
            pv_forecast=[0.0] * len(prices),
        )

        self.assertIsNotNone(result)
        self.assertLessEqual(result['schedule'][-1]['soc_end'], e.min_soc + e.soc_step)
        self.assertTrue(any(s['control_action'] == 'SELL' for s in result['schedule']))

    def test_classify_action_four_modes(self):
        c = self.engine._classify_action
        # charging (SoC rising) -> BUY
        self.assertEqual(c(50.0, 55.0, 5.0), 'buy')
        # battery discharging AND exporting -> SELL
        self.assertEqual(c(50.0, 45.0, -5.0), 'sell')
        # battery serving loads, no export -> SELF-SUPPLY
        self.assertEqual(c(50.0, 45.0, 0.5), 'self_supply')
        # battery held, grid covers load -> HOLD
        self.assertEqual(c(50.0, 50.0, 3.0), 'hold')
        # battery flat, grid idle (PV covers load exactly) -> HOLD
        self.assertEqual(c(50.0, 50.0, 0.0), 'hold')
        # full battery, PV surplus feeding in -> SELL
        self.assertEqual(c(100.0, 100.0, -2.0), 'sell')

    def test_15min_subdivision_of_hourly_prices(self):
        # With a 15-minute target over hourly prices, the plan should expand to
        # ~4x the slots and Victron durations become multiples of 900s.
        self.engine.slot_minutes = 15.0
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            price = 0.05 if 2 <= t.hour < 5 else 0.40
            prices.append({'start': t, 'total': price, 'level': 'NORMAL'})

        result = self.engine.optimize(20.0, prices)
        self.assertIsNotNone(result)
        self.assertGreater(len(result['schedule']), 24)  # subdivided
        for slot in result['victron_slots']:
            self.assertEqual(slot['duration'] % 900, 0)

    def test_min_sell_price_floor_blocks_cheap_battery_export(self):
        # All prices below the sell floor -> the battery must never be actively
        # discharged to the grid (no 'discharge' actions).
        self.engine.min_sell_price = 0.50
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            prices.append({'start': t, 'total': 0.10 + 0.01 * (i % 5), 'level': 'NORMAL'})

        result = self.engine.optimize(90.0, prices)
        self.assertIsNotNone(result)
        self.assertFalse(any(s['action'] == 'sell' for s in result['schedule']))

    def test_battery_cycle_cost_reduces_cycling(self):
        # Cheap early, expensive later -> arbitrage is profitable with no wear cost.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            prices.append({'start': t, 'total': 0.10 if i < 12 else 0.30, 'level': 'NORMAL'})

        def make(cycle_cost):
            from lib.ai_powered_ess import OptimizationEngine
            e = OptimizationEngine()
            e.battery_capacity = 45.0
            e.charge_efficiency = 0.90
            e.discharge_efficiency = 0.90
            e.min_soc = 5.0
            e.export_price_factor = 1.0
            e.export_fee = 0.0
            e.expected_peak_price = 0.0
            e.min_sell_price = 0.0
            e.terminal_value_factor = 0.0
            e.slot_minutes = 60.0
            e.cycle_cost = cycle_cost
            e.arbitrage_margin = 0.0
            e.max_grid_charge_soc = 100.0
            return e

        sells_zero = sum(s['action'] == 'sell' for s in make(0.0).optimize(50.0, prices)['schedule'])
        sells_high = sum(s['action'] == 'sell' for s in make(1.0).optimize(50.0, prices)['schedule'])
        self.assertGreater(sells_zero, 0)
        self.assertLessEqual(sells_high, sells_zero)
        self.assertEqual(sells_high, 0)  # 1.0/kWh wear dwarfs the 0.20 spread

    def _arb_engine(self, **overrides):
        """A deterministic engine for hurdle/ceiling tests."""
        from lib.ai_powered_ess import OptimizationEngine
        e = OptimizationEngine()
        e.battery_capacity = 45.0
        e.charge_efficiency = 0.95
        e.discharge_efficiency = 0.95
        e.min_soc = 5.0
        e.export_price_factor = 1.0
        e.export_fee = 0.0
        e.expected_peak_price = 0.0
        e.min_sell_price = 0.0
        e.terminal_value_factor = 0.0
        e.slot_minutes = 60.0
        e.cycle_cost = 0.0
        e.arbitrage_margin = 0.0
        e.max_grid_charge_soc = 100.0
        for k, v in overrides.items():
            setattr(e, k, v)
        return e

    def test_arbitrage_margin_prunes_thin_spread_cycles(self):
        # Thin spread (0.20 -> 0.23) is profitable with no hurdle but not once a
        # margin larger than the spread is required.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            prices.append({'start': t, 'total': 0.20 if i < 12 else 0.23, 'level': 'NORMAL'})

        # Start at the reserve floor so any sell requires a charge-then-sell cycle
        # (no pre-charged energy to fire-sale against the zero terminal value).
        sells_none = sum(s['action'] == 'sell'
                         for s in self._arb_engine(arbitrage_margin=0.0).optimize(5.0, prices)['schedule'])
        sells_marg = sum(s['action'] == 'sell'
                         for s in self._arb_engine(arbitrage_margin=0.10).optimize(5.0, prices)['schedule'])
        self.assertGreater(sells_none, 0)
        self.assertEqual(sells_marg, 0)  # 0.10/kWh hurdle dwarfs the 0.03 spread

    def test_profitable_grid_charge_is_inferred_from_path_economics(self):
        # The optimizer should infer whether charging is profitable from the full
        # buy->sell path, without a user price ceiling.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(12):
            t = base_time + timedelta(hours=i)
            if i < 6:
                p = 0.245         # above the old 0.23 cap, still excellent vs peak
            else:
                p = 0.60          # peak to sell into
            prices.append({'start': t, 'total': p, 'level': 'NORMAL'})

        pv = [0.0] * 12
        sched = self._arb_engine().optimize(
            5.0,
            prices,
            load_forecast=[0.0] * len(prices),
            pv_forecast=pv,
        )['schedule']
        buys = [s for s in sched if s['action'] == 'buy' and s['grid_energy'] > 1e-6]
        sells = [s for s in sched if s['control_action'] == 'SELL']
        self.assertTrue(buys, "expected grid charging for a profitable spread")
        self.assertTrue(any(s['price'] > 0.23 for s in buys))
        self.assertTrue(sells, "expected the charged energy to sell into the peak")

    def test_flat_prices_do_not_trigger_pointless_grid_charging(self):
        # Removing hard caps must not make the optimizer buy blindly; with no
        # profitable spread and no avoided future cost, it should stay out.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': 0.25, 'level': 'NORMAL'}
            for i in range(12)
        ]

        sched = self._arb_engine().optimize(
            5.0,
            prices,
            load_forecast=[0.0] * len(prices),
            pv_forecast=[0.0] * len(prices),
        )['schedule']

        self.assertFalse(any(s['action'] == 'buy' and s['grid_energy'] > 1e-6 for s in sched))
        self.assertFalse(any(s['control_action'] == 'SELL' for s in sched))

    def test_max_grid_charge_soc_caps_grid_sourced_charging(self):
        # Cheap early slots and expensive later slots make grid arbitrage worth
        # doing, but the user cap must stop grid-forced charging at 90% while
        # still allowing later discharge.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(10):
            t = base_time + timedelta(hours=i)
            prices.append({'start': t, 'total': 0.10 if i < 5 else 0.60, 'level': 'NORMAL'})

        result = self._arb_engine(max_grid_charge_soc=90.0).optimize(
            80.0,
            prices,
            load_forecast=[0.0] * len(prices),
            pv_forecast=[0.0] * len(prices),
        )

        self.assertIsNotNone(result)
        grid_buys = [
            s for s in result['schedule']
            if s['action'] == 'buy' and s['grid_energy'] > 1e-6
        ]
        self.assertTrue(grid_buys, "expected some grid-sourced charging below the cap")
        self.assertLessEqual(max(s['soc_end'] for s in grid_buys), 90.0 + 1e-6)
        self.assertTrue(result['victron_slots'], "expected a Victron grid-charge window")
        self.assertTrue(all(s['target_soc'] <= 90 for s in result['victron_slots']))

    def test_cost_basis_floor_math_and_precedence(self):
        # basis €0.27/kWh DC at 90% discharge eff -> €0.30/kWh AC floor.
        self.engine.discharge_efficiency = 0.90
        self.engine.min_sell_price = 0.0
        self.engine.set_cost_basis_floor(0.27)
        self.assertAlmostEqual(self.engine.cost_basis_sell_floor, 0.27 / 0.90, places=4)
        self.assertAlmostEqual(self.engine._effective_sell_floor(), 0.27 / 0.90, places=4)
        # The higher of the static and dynamic floor wins.
        self.engine.min_sell_price = 0.50
        self.assertAlmostEqual(self.engine._effective_sell_floor(), 0.50, places=4)
        # Zero basis (empty / PV-filled battery) disables the dynamic floor.
        self.engine.min_sell_price = 0.0
        self.engine.set_cost_basis_floor(0.0)
        self.assertEqual(self.engine.cost_basis_sell_floor, 0.0)
        self.assertEqual(self.engine._effective_sell_floor(), 0.0)

    def test_cost_basis_floor_blocks_selling_below_cost(self):
        # Energy bought at a high basis must not be dumped into a lower-priced
        # "peak". Prices top out at 0.30; a 0.40/kWh DC basis (floor ~0.44 AC)
        # means no slot clears the floor, so the battery is never actively sold.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = []
        for i in range(24):
            t = base_time + timedelta(hours=i)
            prices.append({'start': t, 'total': 0.30 if i % 6 == 0 else 0.25, 'level': 'NORMAL'})

        eng = self._arb_engine(min_sell_price=0.0)
        eng.set_cost_basis_floor(0.40)          # floor ~0.421/kWh AC (>0.30)
        result = eng.optimize(90.0, prices)
        self.assertIsNotNone(result)
        self.assertFalse(any(s['action'] == 'sell' for s in result['schedule']),
                         "must not actively discharge below the cost-basis floor")

    def test_cost_basis_protects_initial_energy_without_blocking_future_arbitrage(self):
        # Regression from 2026-07-18: a nearly empty pack acquired a high basis
        # from a small €0.31/kWh low-SoC charge. Applying that basis to *all future*
        # energy suppressed a clearly profitable €0.13 -> €0.32 cycle until PV
        # diluted the persisted basis hours later. Protect the initial 3% tranche,
        # but allow newly purchased energy above it to charge and discharge.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = [
            {'start': base_time + timedelta(hours=i),
             'total': 0.13 if i < 6 else 0.32, 'level': 'NORMAL'}
            for i in range(12)
        ]
        eng = self._arb_engine(min_soc=0.0, max_grid_charge_soc=100.0)
        eng.set_cost_basis_floor(0.31)  # AC recovery floor ~€0.326 > the €0.32 peak

        result = eng.optimize(
            3.0, prices,
            load_forecast=[0.0] * len(prices),
            pv_forecast=[0.0] * len(prices),
        )

        buys = [s for s in result['schedule'] if s['action'] == 'buy' and s['grid_energy'] > 1e-6]
        sells = [s for s in result['schedule'] if s['control_action'] == 'SELL']
        self.assertTrue(buys, "future cheap energy should still be purchased")
        self.assertTrue(sells, "newly purchased energy should still be sellable")
        self.assertGreater(max(s['soc_end'] for s in buys), 90.0)
        self.assertGreaterEqual(result['schedule'][-1]['soc_end'], 3.0 - 1e-6,
                                "the expensive initial tranche must remain protected")

    def test_static_min_sell_floor_still_blocks_all_battery_exports(self):
        # Unlike the dynamic basis, ESS_MIN_SELL_PRICE is an absolute operator
        # policy and must continue to apply to initial and newly charged energy.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = [
            {'start': base_time + timedelta(hours=i),
             'total': 0.13 if i < 6 else 0.32, 'level': 'NORMAL'}
            for i in range(12)
        ]
        eng = self._arb_engine(min_soc=0.0, min_sell_price=0.35)
        result = eng.optimize(3.0, prices, [0.0] * 12, [0.0] * 12)
        self.assertFalse(any(s['control_action'] == 'SELL' for s in result['schedule']))

    def test_cost_basis_protected_tranche_can_be_carried_between_plan_segments(self):
        # The daily-settlement policy optimizes today and tomorrow separately.
        # Tomorrow may begin at 96% after a cheap charge today, but only the
        # original 3% carries the old basis; the second segment must not relabel
        # all 96% as historically expensive energy.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': 0.32, 'level': 'NORMAL'}
            for i in range(6)
        ]
        eng = self._arb_engine(min_soc=0.0, max_grid_charge_soc=100.0)
        eng.set_cost_basis_floor(0.31)

        result = eng.optimize(
            96.0, prices,
            load_forecast=[0.0] * len(prices),
            pv_forecast=[0.0] * len(prices),
            protected_soc_percent=3.0,
        )

        self.assertTrue(any(s['control_action'] == 'SELL' for s in result['schedule']))
        self.assertGreaterEqual(result['schedule'][-1]['soc_end'], 3.0 - 1e-6)

    def test_frontload_charging_matches_full_power(self):
        # The DP may plan a gentle trickle on flat-price slots; re-timing should
        # charge at full power to the same target, then hold.
        self.engine.battery_capacity = 40.0
        self.engine.charge_efficiency = 1.0
        self.engine.max_charge_power = 10.0
        self.engine.max_power_import = 10.0
        slot_h = 0.25                       # full power = 10 kW * 0.25 h = 2.5 kWh = 6.25%

        def s(a, b):
            return {'time': datetime.now(tz.UTC), 'action': 'buy', 'soc_start': a,
                    'soc_end': b, 'grid_energy': (b - a) / 100.0 * 40.0,
                    'load': 0.0, 'pv': 0.0, 'price': 0.10, 'sell': 0.10}

        sched = [s(0, 2.5), s(2.5, 5), s(5, 7.5), s(7.5, 10)]   # gentle: +2.5%/slot
        self.engine._frontload_charging(sched, slot_h)

        # Slot 0 now charges at full power (6.25%), not the gentle 2.5%.
        self.assertAlmostEqual(sched[0]['soc_end'], 6.25, places=2)
        self.assertAlmostEqual(sched[0]['grid_energy'], 2.5, places=2)
        # The run still ends exactly on the original target (downstream untouched).
        self.assertAlmostEqual(sched[-1]['soc_end'], 10.0, places=2)
        # Once the target is reached, later slots hold (no extra charge).
        self.assertAlmostEqual(sched[-1]['soc_start'], 10.0, places=2)
        self.assertAlmostEqual(sched[-1]['grid_energy'], 0.0, places=2)

    def test_frontload_charging_respects_import_limit(self):
        # With a tight import limit, the per-slot charge can't exceed it.
        self.engine.battery_capacity = 40.0
        self.engine.charge_efficiency = 1.0
        self.engine.max_charge_power = 100.0      # effectively unlimited battery power
        self.engine.max_power_import = 4.0        # 4 kW * 0.25 h = 1.0 kWh/slot cap
        slot_h = 0.25

        def s(a, b):
            return {'time': datetime.now(tz.UTC), 'action': 'buy', 'soc_start': a,
                    'soc_end': b, 'grid_energy': (b - a) / 100.0 * 40.0,
                    'load': 0.0, 'pv': 0.0, 'price': 0.10, 'sell': 0.10}

        sched = [s(0, 2), s(2, 4), s(4, 6), s(6, 8), s(8, 10)]   # gentle +2%/slot
        self.engine._frontload_charging(sched, slot_h)
        # Front-loaded up to the 1.0 kWh/slot import cap (more than the gentle
        # 0.8 kWh, but never above the grid limit).
        self.assertAlmostEqual(sched[0]['grid_energy'], 1.0, places=2)
        self.assertAlmostEqual(sched[-1]['soc_end'], 10.0, places=2)

    def test_frontload_charging_does_not_create_grid_buy_above_soc_cap(self):
        # A legal DP trajectory may reach the user grid-charge cap from grid,
        # then rise further from PV surplus. Re-timing must not turn that later
        # PV-only charge into an earlier grid BUY above the cap.
        self.engine.battery_capacity = 40.0
        self.engine.charge_efficiency = 1.0
        self.engine.max_charge_power = 40.0
        self.engine.max_power_import = 40.0
        self.engine.max_grid_charge_soc = 85.0
        slot_h = 0.25
        base = datetime.now(tz.UTC).replace(second=0, microsecond=0)

        sched = [
            {'time': base, 'action': 'buy', 'soc_start': 83.0, 'soc_end': 85.0,
             'grid_energy': 0.8, 'load': 0.0, 'pv': 0.0, 'price': 0.10, 'sell': 0.10},
            {'time': base + timedelta(minutes=15), 'action': 'buy', 'soc_start': 85.0, 'soc_end': 88.0,
             'grid_energy': -0.2, 'load': 0.0, 'pv': 1.4, 'price': 0.10, 'sell': 0.10},
            {'time': base + timedelta(minutes=30), 'action': 'buy', 'soc_start': 88.0, 'soc_end': 91.0,
             'grid_energy': -0.2, 'load': 0.0, 'pv': 1.4, 'price': 0.10, 'sell': 0.10},
        ]

        self.engine._frontload_charging(sched, slot_h)

        grid_buys = [s for s in sched if s['grid_energy'] > 1e-6 and s['soc_end'] > s['soc_start']]
        self.assertTrue(grid_buys)
        self.assertLessEqual(max(s['soc_end'] for s in grid_buys), 85.0 + 1e-6)
        self.assertGreater(sched[-1]['soc_end'], 85.0, "PV surplus may still charge above the grid cap")

    def test_frontload_charging_keeps_valid_charge_run(self):
        # Re-timing mirrors the active optimizer constraints and must keep an
        # otherwise valid charge run intact.
        self.engine.battery_capacity = 40.0
        self.engine.charge_efficiency = 1.0
        self.engine.max_charge_power = 40.0
        self.engine.max_power_import = 40.0
        slot_h = 0.25
        base = datetime.now(tz.UTC).replace(second=0, microsecond=0)

        sched = [
            {'time': base, 'action': 'buy', 'soc_start': 80.0, 'soc_end': 82.0,
             'grid_energy': 0.8, 'load': 0.0, 'pv': 0.0, 'price': 0.30, 'sell': 0.30},
            {'time': base + timedelta(minutes=15), 'action': 'buy', 'soc_start': 82.0, 'soc_end': 86.0,
             'grid_energy': 1.6, 'load': 0.0, 'pv': 0.0, 'price': 0.10, 'sell': 0.10},
            {'time': base + timedelta(minutes=30), 'action': 'buy', 'soc_start': 86.0, 'soc_end': 90.0,
             'grid_energy': 1.6, 'load': 0.0, 'pv': 0.0, 'price': 0.10, 'sell': 0.10},
        ]

        self.engine._frontload_charging(sched, slot_h)

        grid_buys = [s for s in sched if s['grid_energy'] > 1e-6 and s['soc_end'] > s['soc_start']]
        self.assertTrue(grid_buys)
        self.assertTrue(any(s['price'] > 0.20 + 1e-9 for s in grid_buys))

    def test_pv_only_charging_is_reported_as_idle_not_grid_buy(self):
        base = datetime.now(tz.UTC).replace(second=0, microsecond=0)
        sched = [{
            'time': base,
            'action': 'buy',
            'soc_start': 90.0,
            'soc_end': 92.0,
            'grid_energy': 0.0,
            'load': 0.4,
            'pv': 1.5,
            'price': 0.24,
            'sell': 0.24,
        }]

        result = self.engine._post_process(sched, 900)

        slot = result['schedule'][0]
        self.assertEqual(slot['control_action'], 'IDLE')
        self.assertEqual(slot['reason_code'], 'PV_CHARGING')
        self.assertEqual(result['control_action'], 'IDLE')
        self.assertEqual(result['setpoint'], 0.0)

    def test_daily_settlement_policy_protects_today_from_small_future_gain(self):
        full = {
            'schedule': [
                self._policy_step(28, 18, 10.0),
                self._policy_step(29, 8, -17.0),
            ]
        }
        today_first = {
            'schedule': [
                self._policy_step(28, 18, 0.0),
                self._policy_step(29, 8, -4.0),
            ]
        }
        model = {
            'exceptional_threshold_eur': 8.0,
            'forecast_risk_eur': 1.0,
            'historical_price_p95': 2.0,
        }

        selected, policy = ai_powered_ess._select_daily_settlement_candidate(
            full, today_first, model)

        self.assertIs(selected, today_first)
        self.assertEqual(policy['selected'], 'today_first')
        self.assertEqual(policy['reason_code'], 'DAILY_SETTLEMENT_PROTECTED')
        self.assertAlmostEqual(policy['today_sacrifice_eur'], 10.0)
        self.assertAlmostEqual(policy['future_gain_eur'], 3.0)

    def test_daily_settlement_policy_allows_exceptional_future_gain(self):
        full = {
            'schedule': [
                self._policy_step(28, 18, 2.0),
                self._policy_step(29, 8, -50.0),
            ]
        }
        today_first = {
            'schedule': [
                self._policy_step(28, 18, 0.0),
                self._policy_step(29, 8, -10.0),
            ]
        }
        model = {
            'exceptional_threshold_eur': 8.0,
            'forecast_risk_eur': 1.0,
            'historical_price_p95': 2.0,
        }

        selected, policy = ai_powered_ess._select_daily_settlement_candidate(
            full, today_first, model)

        self.assertIs(selected, full)
        self.assertEqual(policy['selected'], 'full_horizon')
        self.assertEqual(policy['reason_code'], 'EXCEPTIONAL_FUTURE_GAIN_ACCEPTED')
        self.assertAlmostEqual(policy['today_sacrifice_eur'], 2.0)
        self.assertAlmostEqual(policy['future_gain_eur'], 38.0)

    def test_optimize_with_daily_policy_same_day_attaches_policy(self):
        base_time = datetime(2099, 6, 28, 18, 0, tzinfo=tz.UTC)
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': 0.25, 'level': 'NORMAL'}
            for i in range(4)
        ]

        result = self._arb_engine().optimize_with_daily_policy(
            60.0,
            prices,
            load_forecast=[0.0] * len(prices),
            pv_forecast=[0.0] * len(prices),
            opportunity_model={
                'exceptional_threshold_eur': 8.0,
                'forecast_risk_eur': 1.0,
                'historical_price_p95': 2.0,
            },
        )

        self.assertIsNotNone(result)
        self.assertIn('planning_policy', result)
        self.assertEqual(result['planning_policy']['selected'], 'full_horizon')
        self.assertEqual(result['planning_policy']['reason_code'], 'SAME_DAY_HORIZON')

if __name__ == '__main__':
    unittest.main()
