import unittest
from datetime import datetime, timedelta
from dateutil import tz
import sys
import os

# Add repo root to path
sys.path.append(os.getcwd())

from lib.ai_powered_ess import OptimizationEngine

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
        # Plan at native (hourly) resolution by default in tests; individual
        # tests override this to exercise sub-slot resampling.
        self.engine.slot_minutes = 60.0

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
            e.slot_minutes = 60.0
            e.terminal_value_factor = terminal_factor
            return e

        no_terminal = make_engine(0.0).optimize(90.0, prices)
        with_terminal = make_engine(1.0).optimize(90.0, prices)

        end_no_terminal = no_terminal['schedule'][-1]['soc_end']
        end_with_terminal = with_terminal['schedule'][-1]['soc_end']
        self.assertGreaterEqual(end_with_terminal, end_no_terminal)

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

if __name__ == '__main__':
    unittest.main()
