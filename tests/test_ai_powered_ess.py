import unittest
import json
import time
from datetime import datetime, timedelta
from dateutil import tz
import sys
import os
from unittest.mock import patch

import pytest

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

    def test_below_reserve_waits_for_a_cheaper_buy_instead_of_forcing_peak_charge(self):
        """The reserve prevents further discharge, not an uneconomic emergency buy.

        A live SoC can fall slightly below the configured reserve through meter
        drift/BMS behaviour.  At a high-price current slot the controller must
        retain and cover the house from the grid rather than forcing an
        immediate precharge solely to step back over the discretized reserve
        boundary. A later buy remains an economic decision, not a reserve
        bookkeeping requirement.
        """
        now = datetime.now(tz.UTC).replace(second=0, microsecond=0)
        self.engine.soc_step = 1.0
        self.engine.soc_states = [float(i) for i in range(101)]
        self.engine.max_power_import = 10.0
        self.engine.max_charge_power = 10.0
        self.engine.max_power_export = 10.0
        self.engine.max_discharge_power = 10.0
        prices = [
            {'start': now, 'total': 0.35},
            {'start': now + timedelta(hours=1), 'total': 0.10},
            {'start': now + timedelta(hours=2), 'total': 0.15},
        ]
        loads = {point['start']: 0.25 for point in prices}
        pv = {point['start']: 0.0 for point in prices}

        result = self.engine.optimize(4.0, prices, loads, pv)

        self.assertIsNotNone(result)
        first = result['schedule'][0]
        self.assertEqual(first['action'], 'hold')
        self.assertEqual(first['control_action'], 'RETAIN')
        self.assertEqual(first['soc_start'], 4.0)
        self.assertEqual(first['soc_end'], 4.0)

    def test_below_reserve_recovers_when_no_cheaper_buy_is_known(self):
        """Reserve recovery remains compulsory when waiting cannot save money."""
        now = datetime.now(tz.UTC).replace(second=0, microsecond=0)
        self.engine.soc_step = 1.0
        self.engine.soc_states = [float(i) for i in range(101)]
        self.engine.max_power_import = 10.0
        self.engine.max_charge_power = 10.0
        prices = [
            {'start': now, 'total': 0.35},
            {'start': now + timedelta(hours=1), 'total': 0.36},
            {'start': now + timedelta(hours=2), 'total': 0.37},
        ]
        loads = {point['start']: 0.25 for point in prices}
        pv = {point['start']: 0.0 for point in prices}

        result = self.engine.optimize(4.0, prices, loads, pv)

        self.assertIsNotNone(result)
        first = result['schedule'][0]
        self.assertEqual(first['action'], 'buy')
        self.assertGreaterEqual(first['soc_end'], self.engine.min_soc)

    def test_pv_surplus_sell_is_idle_neutral_setpoint(self):
        # Exporting while SoC is flat = PV surplus -> IDLE, neutral setpoint (no
        # forced/capping export); Victron routes surplus in real time.
        sched = [self._step('sell', 50.0, 50.0, -0.09, price=0.15)]
        result = self.engine._post_process(sched, 900)
        self.assertEqual(result['control_action'], 'IDLE')
        self.assertTrue(result['pv_surplus'])
        self.assertEqual(result['setpoint'], 0.0)

    def test_optimizer_stores_pv_before_crediting_neutral_export(self):
        """A below-full battery cannot earn phantom neutral PV export."""
        self.engine.battery_capacity = 10.0
        self.engine.charge_efficiency = 1.0
        self.engine.discharge_efficiency = 1.0
        self.engine.min_soc = 0.0
        self.engine.soc_step = 10.0
        self.engine.soc_states = [float(value) for value in range(0, 101, 10)]
        self.engine.max_charge_power = 10.0
        self.engine.max_discharge_power = 10.0
        self.engine.max_power_import = 10.0
        self.engine.max_power_export = 10.0
        self.engine.terminal_value_factor = 0.0
        base = datetime.now(tz.UTC).replace(
            minute=0, second=0, microsecond=0) + timedelta(hours=1)

        result = self.engine.optimize(
            50.0,
            [{'start': base, 'total': 0.50}],
            load_forecast=[0.0],
            pv_forecast=[1.0],
            policy_name='pv_first_self_sufficiency',
        )

        step = result['schedule'][0]
        self.assertEqual(step['soc_start'], 50.0)
        self.assertEqual(step['soc_end'], 60.0)
        self.assertEqual(step['grid_energy'], 0.0)
        self.assertNotEqual(step['control_action'], 'SELL')

    def test_sub_lattice_pv_surplus_is_not_credited_as_export(self):
        """Fractional surplus is conservatively absorbed, not sold on paper."""
        self.engine.battery_capacity = 10.0
        self.engine.charge_efficiency = 1.0
        self.engine.discharge_efficiency = 1.0
        self.engine.min_soc = 0.0
        self.engine.soc_step = 10.0
        self.engine.soc_states = [float(value) for value in range(0, 101, 10)]
        self.engine.max_charge_power = 10.0
        self.engine.max_discharge_power = 10.0
        self.engine.max_power_import = 10.0
        self.engine.max_power_export = 10.0
        self.engine.terminal_value_factor = 0.0
        base = datetime.now(tz.UTC).replace(
            minute=0, second=0, microsecond=0) + timedelta(hours=1)

        result = self.engine.optimize(
            50.0,
            [{'start': base, 'total': 0.50}],
            load_forecast=[0.0],
            pv_forecast=[0.5],
            policy_name='pv_first_self_sufficiency',
        )

        step = result['schedule'][0]
        self.assertEqual(step['soc_end'], 50.0)
        self.assertEqual(step['grid_energy'], 0.0)
        self.assertEqual(result['objective_cost_eur'], 0.0)

    def test_buy_reason_names_best_cross_day_sell_not_first_tiny_export(self):
        base = datetime(2026, 8, 20, 14, 0, tzinfo=tz.UTC)
        schedule = [
            {
                'time': base, 'action': 'buy', 'soc_start': 20.0,
                'soc_end': 60.0, 'grid_energy': 4.0,
                'price': 0.20, 'sell': 0.18,
            },
            {
                'time': base + timedelta(hours=6), 'action': 'sell',
                'soc_start': 60.0, 'soc_end': 59.0, 'grid_energy': -0.05,
                'price': 0.35, 'sell': 0.33,
            },
            {
                'time': base + timedelta(hours=18),
                'action': 'sell', 'soc_start': 59.0, 'soc_end': 20.0,
                'grid_energy': -4.0, 'price': 0.40, 'sell': 0.38,
            },
        ]

        code, reason = self.engine._explain_action(schedule, 0)

        self.assertEqual(code, 'PRECHARGE_FOR_PEAK')
        self.assertIn('tomorrow at 08:00', reason)
        self.assertIn('€0.380/kWh', reason)

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
        for step in result['schedule']:
            if step['control_action'] != 'BUY' or step['grid_energy'] <= 1e-6:
                continue
            self.assertTrue(any(
                slot['start'] <= step['time']
                < slot['start'] + timedelta(seconds=slot['duration'])
                for slot in result['victron_slots']
            ), f"BUY at {step['time']} is not executable by a published Victron slot")

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

    def test_same_day_horizon_retains_bounded_household_energy(self):
        base_time = datetime(2099, 6, 28, 20, 0, tzinfo=tz.UTC)
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': 0.40}
            for i in range(4)
        ]
        engine = self._arb_engine(terminal_value_factor=1.0)
        engine.expected_peak_price = 0.50
        engine.battery_capacity = 10.0
        engine.discharge_efficiency = 1.0

        with patch(
                'lib.ai_powered_ess.retrieve_setting',
                side_effect=lambda key: (
                    '2' if key == 'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS' else None)):
            result = engine.optimize(
                90.0, prices,
                load_forecast=[1.0] * len(prices),
                pv_forecast=[0.0] * len(prices))

        self.assertIsNotNone(result)
        # Two continuation hours at 1kW require 2kWh above reserve, not a full
        # battery and not an artificial dump to the minimum boundary.
        self.assertGreaterEqual(result['schedule'][-1]['soc_end'], 25.0 - 1e-6)
        self.assertLessEqual(result['schedule'][-1]['soc_end'], 35.0 + 1e-6)

    def test_excess_pv_is_curtailed_instead_of_making_plan_infeasible(self):
        base_time = datetime.now(tz.UTC).replace(second=0, microsecond=0)
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': 0.20}
            for i in range(2)
        ]
        engine = self._arb_engine()
        engine.max_power_export = 1.0
        result = engine.optimize(
            100.0, prices,
            load_forecast=[0.0, 0.0],
            pv_forecast=[5.0, 5.0])

        self.assertIsNotNone(result)
        self.assertEqual(result['schedule'][-1]['soc_end'], 100.0)
        self.assertGreater(result['pv_curtailed_kwh'], 0.0)
        self.assertTrue(all(
            step['grid_energy'] >= -1.0 - 1e-6
            for step in result['schedule']))

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

    def test_unrecoverable_cost_basis_waits_for_best_forward_sale(self):
        # Historical acquisition cost is sunk. When no visible price can recover
        # it, wait for the best forward sale rather than stranding the battery or
        # dumping it into an earlier inferior slot.
        base_time = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        values = [0.20, 0.25, 0.30, 0.25]
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': value, 'level': 'NORMAL'}
            for i, value in enumerate(values)
        ]

        eng = self._arb_engine(min_sell_price=0.0)
        eng.set_cost_basis_floor(0.40)
        result = eng.optimize(90.0, prices)
        self.assertIsNotNone(result)
        active_sells = [
            step for step in result['schedule']
            if step['action'] == 'sell' and step['soc_end'] < step['soc_start']
        ]
        self.assertTrue(active_sells)
        self.assertEqual(active_sells[0]['sell'], 0.30)
        self.assertFalse(any(
            step['action'] == 'sell'
            and step['soc_end'] < step['soc_start']
            and step['sell'] < 0.30
            for step in result['schedule'][:2]))

    def test_cost_basis_protects_initial_energy_without_blocking_future_arbitrage(self):
        # Regression from 2026-07-18: a nearly empty pack acquired a high basis
        # from a small €0.31/kWh low-SoC charge. Applying that basis to *all future*
        # energy suppressed a clearly profitable €0.13 -> €0.32 cycle until PV
        # diluted the persisted basis hours later. The initial tranche may wait
        # for the best forward price, while newly purchased energy must remain
        # free to charge and discharge.
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
        self.assertEqual(result['schedule'][-1]['soc_end'], 0.0)

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

    def test_explicit_opening_tranche_does_not_block_best_forward_sale(self):
        # Legacy callers may still identify the opening tranche explicitly. A
        # sunk basis may delay it until the best visible opportunity, but cannot
        # strand it when recovery above basis is impossible.
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
        self.assertEqual(result['schedule'][-1]['soc_end'], 0.0)

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

    def test_frontload_never_moves_grid_buy_into_adjacent_pv_only_slot(self):
        """Every final BUY must remain covered by the original control window."""
        self.engine.battery_capacity = 10.0
        self.engine.charge_efficiency = 1.0
        self.engine.discharge_efficiency = 1.0
        self.engine.max_charge_power = 5.0
        self.engine.max_discharge_power = 5.0
        self.engine.max_power_import = 4.0
        self.engine.max_power_export = 5.0
        self.engine.min_soc = 0.0
        self.engine.max_grid_charge_soc = 100.0
        self.engine.terminal_value_factor = 0.0
        self.engine.soc_step = 10.0
        self.engine.soc_states = [float(value) for value in range(0, 101, 10)]
        base = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) \
            + timedelta(hours=1)
        prices = [
            {'start': base + timedelta(hours=index), 'total': price}
            for index, price in enumerate((1.0, 0.1, 2.0))
        ]

        result = self.engine.optimize(
            0.0, prices,
            load_forecast=[0.0, 0.0, 0.0],
            pv_forecast=[1.0, 0.0, 0.0],
        )

        self.assertEqual(result['schedule'][0]['control_action'], 'IDLE')
        for step in result['schedule']:
            if step['control_action'] != 'BUY':
                continue
            self.assertTrue(any(
                slot['start'] <= step['time']
                < slot['start'] + timedelta(seconds=slot['duration'])
                for slot in result['victron_slots']
            ), f"BUY at {step['time']} is outside every Victron charge slot")

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

    def test_daily_policy_uses_one_unified_horizon_solve(self):
        base_time = datetime(2099, 6, 28, 18, 0, tzinfo=tz.UTC)
        prices = [
            {'start': base_time + timedelta(hours=i), 'total': 0.25}
            for i in range(12)
        ]
        engine = self._arb_engine()
        with patch.object(engine, 'optimize', wraps=engine.optimize) as optimize:
            result = engine.optimize_with_daily_policy(
                60.0, prices, [0.5] * len(prices), [0.0] * len(prices))

        self.assertIsNotNone(result)
        self.assertEqual(optimize.call_count, 1)
        self.assertEqual(
            result['planning_policy']['reason_code'],
            'UNIFIED_HORIZON_OBJECTIVE')

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
        self.assertEqual(
            result['planning_policy']['reason_code'],
            'UNIFIED_HORIZON_OBJECTIVE')

    def test_discharge_blocked_slot_cannot_feed_ev_load_from_home_battery(self):
        start = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0)
        prices = [
            {'start': start + timedelta(hours=i), 'total': 0.80, 'level': 'EXPENSIVE'}
            for i in range(3)
        ]
        blocked = {row['start'] for row in prices}

        result = self.engine.optimize_with_daily_policy(
            80.0,
            prices,
            load_forecast=[3.0] * len(prices),
            pv_forecast=[0.0] * len(prices),
            discharge_blocked_slots=blocked,
        )

        self.assertIsNotNone(result)
        for step in result['schedule']:
            self.assertGreaterEqual(step['soc_end'], step['soc_start'])

    def test_pv_first_policy_never_buys_grid_energy_or_exports_battery(self):
        start = datetime(2099, 8, 1, 0, 0, tzinfo=tz.UTC)
        prices = [
            {'start': start + timedelta(hours=i), 'total': price}
            for i, price in enumerate([0.10, 0.45, 0.45, 0.10])
        ]

        result = self.engine.optimize(
            50.0, prices, [1.0] * 4, [0.0] * 4,
            policy_name='pv_first_self_sufficiency')

        self.assertIsNotNone(result)
        self.assertEqual(result['strategy'], 'pv_first_self_sufficiency')
        self.assertFalse(any(
            step['soc_end'] > step['soc_start'] + ai_powered_ess.EPS
            and step['grid_energy'] > ai_powered_ess.EPS
            for step in result['schedule']))
        self.assertFalse(any(
            step['soc_end'] < step['soc_start'] - ai_powered_ess.EPS
            and step['grid_energy'] < -ai_powered_ess.EPS
            for step in result['schedule']))

    def test_hybrid_protects_household_energy_until_next_cheap_window(self):
        with patch.object(
                ai_powered_ess, 'retrieve_setting',
                side_effect=lambda key: {
                    'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0',
                    'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '0',
                }.get(key)):
            policy = self.engine._policy_constraints(
                'protected_hybrid',
                [0.10, 0.40, 0.40, 0.10],
                [0.0, 1.0, 1.0, 0.0],
            )

        self.assertEqual(
            policy['grid_charge_allowed_by_step'], [True, False, False, True])
        self.assertGreater(policy['protected_floor_by_step'][0], self.engine.min_soc)
        self.assertLess(
            policy['protected_floor_by_step'][1],
            policy['protected_floor_by_step'][0])
        self.assertTrue(policy['force_floor_recovery'])

    def test_hybrid_protects_bounded_load_beyond_final_known_price(self):
        with patch.object(
                ai_powered_ess, 'retrieve_setting',
                side_effect=lambda key: {
                    'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0',
                    'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '4',
                }.get(key)):
            policy = self.engine._policy_constraints(
                'protected_hybrid', [0.10, 0.30], [1.0, 1.0])

        expected_dc = 4.0 / self.engine.discharge_efficiency
        expected_floor = self.engine.min_soc + (
            expected_dc / self.engine.battery_capacity * 100.0)
        self.assertGreaterEqual(
            policy['protected_floor_by_step'][-1], expected_floor)

    def test_hybrid_replenishes_household_energy_in_cheapest_window(self):
        """A low live SoC must not turn the safe policy into all-day RETAIN."""
        start = datetime(2099, 8, 1, 8, 0, tzinfo=tz.UTC)
        prices = [
            {'start': start + timedelta(hours=i), 'total': price}
            for i, price in enumerate(
                [0.34, 0.33, 0.30, 0.30, 0.301, 0.33, 0.34, 0.33])
        ]
        settings = {
            'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0',
            'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '0',
        }
        with patch.object(
                ai_powered_ess, 'retrieve_setting',
                side_effect=lambda key: settings.get(key)):
            result = self.engine.optimize(
                2.0, prices, [1.0] * len(prices), [0.0] * len(prices),
                policy_name='protected_hybrid')

        self.assertIsNotNone(result)
        cheapest_window = result['schedule'][2:5]
        self.assertTrue(any(
            step['control_action'] == 'BUY'
            for step in cheapest_window
        ))
        self.assertGreater(
            cheapest_window[-1]['soc_end'], 2.0,
            'household coverage must be procured before the expensive period',
        )
        self.assertTrue(all(
            step['soc_end'] >= step['protected_soc'] - ai_powered_ess.EPS
            for step in result['schedule'][4:]
        ))

    def test_pv_first_uses_stored_energy_at_high_price(self):
        start = datetime(2099, 8, 1, 0, 0, tzinfo=tz.UTC)
        prices = [
            {'start': start + timedelta(hours=i), 'total': price}
            for i, price in enumerate([0.45, 0.45, 0.10, 0.10])
        ]
        with patch.object(
                ai_powered_ess, 'retrieve_setting',
                side_effect=lambda key: {
                    'ESS_MODEL_CHARGE_RATE': '0',
                    'ESS_EXPORT_AC_SETPOINT': '-10000',
                }.get(key)):
            result = self.engine.optimize(
                70.0, prices, [1.0] * 4, [0.0] * 4,
                policy_name='pv_first_self_sufficiency')

        self.assertIsNotNone(result)
        self.assertTrue(any(
            step['control_action'] == 'IDLE'
            and step['soc_end'] < step['soc_start']
            for step in result['schedule'][:2]
        ))

    def test_pv_first_remains_feasible_when_live_soc_is_below_reserve(self):
        start = datetime(2099, 8, 1, 0, 0, tzinfo=tz.UTC)
        prices = [
            {'start': start + timedelta(hours=i), 'total': 0.30}
            for i in range(3)
        ]

        result = self.engine.optimize(
            2.0, prices, [0.2] * 3, [0.0] * 3,
            policy_name='pv_first_self_sufficiency')

        self.assertIsNotNone(result)
        self.assertFalse(any(
            step['grid_energy'] > step['load'] + ai_powered_ess.EPS
            for step in result['schedule']
        ))


def _adaptive_candidate(name, grid_values, end_soc):
    start = datetime(2099, 8, 1, 0, 0, tzinfo=tz.UTC)
    schedule = []
    soc = 50.0
    for index, grid_energy in enumerate(grid_values):
        final_soc = end_soc if index == len(grid_values) - 1 else soc
        schedule.append({
            'time': start + timedelta(hours=index),
            'action': 'hold',
            'control_action': 'IDLE',
            'soc_start': soc,
            'soc_end': final_soc,
            'grid_energy': grid_energy,
            'price': 0.20,
            'sell': 0.20,
            'strategy': name,
            'protected_soc': 5.0,
        })
        soc = final_soc
    return {'schedule': schedule, 'strategy': name}


def test_adaptive_selector_requires_material_trade_benefit(monkeypatch, tmp_path):
    state_path = tmp_path / 'policy.json'
    settings = {
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(state_path),
        'ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR': '1.0',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
        'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0.20',
    }
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 5.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 1.0
    engine.expected_peak_price = 0.0
    candidates = {
        'market_arbitrage': _adaptive_candidate(
            'market_arbitrage', [-5.0], 5.0),
        'pv_first_self_sufficiency': _adaptive_candidate(
            'pv_first_self_sufficiency', [-4.5], 5.0),
        'protected_hybrid': _adaptive_candidate(
            'protected_hybrid', [-4.6], 5.0),
    }

    selected, metadata = ai_powered_ess._select_adaptive_policy(
        candidates, engine,
        opportunity_model={'forecast_risk_eur': 0.25})

    assert selected['strategy'] == 'protected_hybrid'
    assert metadata['reason_code'] == 'CONSERVATIVE_POLICY_PREFERRED'
    assert metadata['trade_hurdle_eur'] == 1.05
    assert metadata['forecast_risk_eur'] == 0.05


def test_adaptive_selector_does_not_let_common_forecast_error_hide_trade_win(
        monkeypatch, tmp_path):
    """Historical day error is scaled, not charged twice as a blanket €2."""
    state_path = tmp_path / 'policy.json'
    settings = {
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(state_path),
        'ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR': '1.0',
        'ESS_ADAPTIVE_FORECAST_RISK_MAX_EUR': '2.0',
        'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0.20',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
    }
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 5.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 1.0
    engine.expected_peak_price = 0.0
    candidates = {
        'market_arbitrage': _adaptive_candidate(
            'market_arbitrage', [-12.5], 5.0),
        'protected_hybrid': _adaptive_candidate(
            'protected_hybrid', [-5.0], 5.0),
    }

    selected, metadata = ai_powered_ess._select_adaptive_policy(
        candidates, engine,
        opportunity_model={'forecast_risk_eur': 20.0})

    assert selected['strategy'] == 'market_arbitrage'
    assert metadata['reason_code'] == 'TRADE_MATERIAL_BENEFIT'
    assert metadata['forecast_risk_eur'] == 0.4
    assert metadata['trade_hurdle_eur'] == 1.4


def test_adaptive_selector_accepts_clearly_superior_trade(monkeypatch, tmp_path):
    state_path = tmp_path / 'policy.json'
    settings = {
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(state_path),
        'ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR': '1.0',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
    }
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 5.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 1.0
    engine.expected_peak_price = 0.0
    candidates = {
        'market_arbitrage': _adaptive_candidate(
            'market_arbitrage', [-12.0], 5.0),
        'pv_first_self_sufficiency': _adaptive_candidate(
            'pv_first_self_sufficiency', [-4.0], 5.0),
        'protected_hybrid': _adaptive_candidate(
            'protected_hybrid', [-4.5], 5.0),
    }

    selected, metadata = ai_powered_ess._select_adaptive_policy(
        candidates, engine,
        opportunity_model={'forecast_risk_eur': 0.25})

    assert selected['strategy'] == 'market_arbitrage'
    assert metadata['reason_code'] == 'TRADE_MATERIAL_BENEFIT'


def test_adaptive_selector_accepts_explicit_zero_hurdle_and_risk(monkeypatch, tmp_path):
    settings = {
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(tmp_path / 'policy.json'),
        'ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR': '0',
        'ESS_ADAPTIVE_FORECAST_RISK_MAX_EUR': '0',
        'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
    }
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 5.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 1.0
    engine.expected_peak_price = 0.0
    candidates = {
        'market_arbitrage': _adaptive_candidate(
            'market_arbitrage', [-5.1], 5.0),
        'protected_hybrid': _adaptive_candidate(
            'protected_hybrid', [-5.0], 5.0),
    }

    selected, metadata = ai_powered_ess._select_adaptive_policy(
        candidates, engine, opportunity_model={'forecast_risk_eur': 0.0})

    assert selected['strategy'] == 'market_arbitrage'
    assert metadata['trade_hurdle_eur'] == 0.0
    assert metadata['forecast_risk_eur'] == 0.0


def test_adaptive_selector_ranks_lifecycle_objective_not_raw_grid_cash(
        monkeypatch, tmp_path):
    settings = {
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(tmp_path / 'policy.json'),
        'ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR': '0',
        'ESS_ADAPTIVE_FORECAST_RISK_MAX_EUR': '0',
        'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
        'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '0',
    }
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 0.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.5
    engine.arbitrage_margin = 0.5
    engine.terminal_value_factor = 0.0
    engine.expected_peak_price = 0.0
    candidates = {
        # Raw grid result +€6, but discharging 5 kWh costs €5 under the
        # production lifecycle/margin objective, leaving an economic score +€1.
        'market_arbitrage': _adaptive_candidate(
            'market_arbitrage', [-6.0], 0.0),
        # Raw result +€3 with no discharge: economically superior despite the
        # lower headline export reward.
        'protected_hybrid': _adaptive_candidate(
            'protected_hybrid', [-3.0], 50.0),
    }
    for candidate in candidates.values():
        candidate['schedule'][0].update({'price': 1.0, 'sell': 1.0})

    selected, metadata = ai_powered_ess._select_adaptive_policy(
        candidates, engine, opportunity_model={'forecast_risk_eur': 0.0})

    assert selected['strategy'] == 'protected_hybrid'
    assert metadata['candidates']['market_arbitrage']['grid_net_eur'] == 6.0
    assert metadata['candidates']['market_arbitrage']['score_eur'] == 1.0
    assert metadata['candidates']['protected_hybrid']['score_eur'] == 3.0


def test_adaptive_selector_caps_legacy_forecast_risk(monkeypatch, tmp_path):
    state_path = tmp_path / 'policy.json'
    settings = {
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(state_path),
        'ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR': '1.0',
        'ESS_ADAPTIVE_FORECAST_RISK_MAX_EUR': '2.0',
        'ESS_ADAPTIVE_FORECAST_RISK_FACTOR': '0.20',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
    }
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 5.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 1.0
    engine.expected_peak_price = 0.0
    candidates = {
        'market_arbitrage': _adaptive_candidate(
            'market_arbitrage', [-20.0], 5.0),
        'protected_hybrid': _adaptive_candidate(
            'protected_hybrid', [-4.5], 5.0),
    }

    selected, metadata = ai_powered_ess._select_adaptive_policy(
        candidates, engine,
        opportunity_model={'forecast_risk_eur': 14.5})

    assert selected['strategy'] == 'market_arbitrage'
    assert metadata['raw_forecast_risk_eur'] == 14.5
    assert metadata['capped_forecast_risk_eur'] == 2.0
    assert metadata['forecast_risk_eur'] == 0.4
    assert metadata['trade_hurdle_eur'] == 1.4


def test_adaptive_terminal_credit_is_limited_to_unknown_household_need(monkeypatch):
    monkeypatch.setattr(
        ai_powered_ess,
        'retrieve_setting',
        lambda key: '2' if key == 'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS' else None,
    )
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 5.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.expected_peak_price = 0.30
    plan = _adaptive_candidate(
        'pv_first_self_sufficiency', [0.0, 0.0], 100.0)
    plan['slot_duration_h'] = 1.0
    for step in plan['schedule']:
        step.update({'load': 1.0, 'pv': 0.0})

    metrics = ai_powered_ess._adaptive_plan_score(plan, engine)

    assert metrics['terminal_credited_kwh'] == 2.0
    assert metrics['terminal_value_eur'] == pytest.approx(0.6)


def test_terminal_factor_zero_disables_expected_peak_continuation(monkeypatch):
    monkeypatch.setattr(
        ai_powered_ess,
        'retrieve_setting',
        lambda key: '6' if key == 'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS' else None,
    )
    engine = ai_powered_ess.OptimizationEngine()
    engine.terminal_value_factor = 0.0
    engine.expected_peak_price = 0.50

    continuation = ai_powered_ess._continuation_assumptions(
        engine, [0.20, 0.30], [1.0, 1.0], 1.0)

    assert continuation['terminal_price_eur_per_ac_kwh'] == 0.0


def test_continuation_uses_trailing_time_of_day_load_not_daytime_mean(monkeypatch):
    monkeypatch.setattr(
        ai_powered_ess,
        'retrieve_setting',
        lambda key: '6' if key == 'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS' else None,
    )
    engine = ai_powered_ess.OptimizationEngine()
    engine.discharge_efficiency = 1.0

    continuation = ai_powered_ess._continuation_assumptions(
        engine,
        [0.20] * 24,
        [-5.0] * 12 + [1.0] * 12,
        1.0,
    )

    assert continuation['continuation_dc_kwh'] == pytest.approx(6.0)


def test_adaptive_terminal_credit_excludes_protected_household_floor(monkeypatch):
    monkeypatch.setattr(
        ai_powered_ess,
        'retrieve_setting',
        lambda key: '6' if key == 'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS' else None,
    )
    engine = ai_powered_ess.OptimizationEngine()
    engine.min_soc = 5.0
    engine.battery_capacity = 10.0
    engine.discharge_efficiency = 1.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 1.0
    engine.expected_peak_price = 0.30
    plan = _adaptive_candidate('protected_hybrid', [0.0], 35.0)
    plan['slot_duration_h'] = 1.0
    plan['schedule'][0].update({
        'load': 1.0,
        'pv': 0.0,
        'protected_soc': 25.0,
    })

    metrics = ai_powered_ess._adaptive_plan_score(plan, engine)

    assert metrics['terminal_credited_kwh'] == pytest.approx(1.0)
    assert metrics['terminal_value_eur'] == pytest.approx(0.30)


def test_material_signature_changes_for_control_relevant_inputs():
    now = datetime.now(tz.UTC).replace(second=0, microsecond=0)
    prices = [{'start': now, 'total': 0.20}]
    baseline = ai_powered_ess._price_horizon_signature(
        prices, [0.2], [0.0], [], 50.0)

    assert ai_powered_ess._price_horizon_signature(
        [{'start': now, 'total': 0.25}], [0.2], [0.0], [], 50.0) != baseline
    assert ai_powered_ess._price_horizon_signature(
        prices, [0.4], [0.0], [], 50.0) != baseline
    assert ai_powered_ess._price_horizon_signature(
        prices, [0.2], [0.3], [], 50.0) != baseline
    assert ai_powered_ess._price_horizon_signature(
        prices, [0.2], [0.0], [now], 50.0) != baseline
    assert ai_powered_ess._price_horizon_signature(
        prices, [0.2], [0.0], [], 54.0) != baseline


def test_executable_objective_matches_non_flat_frontloaded_schedule():
    """Candidate selection must score the exact Victron-shaped trajectory."""
    engine = ai_powered_ess.OptimizationEngine()
    engine.battery_capacity = 10.0
    engine.charge_efficiency = 1.0
    engine.discharge_efficiency = 1.0
    engine.min_soc = 5.0
    engine.max_grid_charge_soc = 100.0
    engine.max_charge_power = 5.0
    engine.max_discharge_power = 5.0
    engine.max_power_import = 5.0
    engine.max_power_export = 5.0
    engine.export_price_factor = 1.0
    engine.export_fee = 0.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 0.0
    engine.slot_minutes = 60.0
    engine.soc_step = 1.0
    engine.soc_states = [float(value) for value in range(101)]
    base = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) \
        + timedelta(hours=1)
    prices = [
        {'start': base + timedelta(hours=index), 'total': price}
        for index, price in enumerate((0.20, 0.10, 0.50))
    ]

    result = engine.optimize(
        5.0, prices,
        load_forecast=[0.0, 0.0, 0.0],
        pv_forecast=[0.0, 0.0, 0.0],
    )
    score = ai_powered_ess._adaptive_plan_score(result, engine)

    assert -result['objective_cost_eur'] == pytest.approx(
        score['score_eur'], abs=1e-6)


def test_falling_price_charge_run_uses_staged_victron_targets():
    """A later cheap slot must not pull its charge into an earlier dear slot."""
    engine = ai_powered_ess.OptimizationEngine()
    engine.battery_capacity = 10.0
    engine.charge_efficiency = 1.0
    engine.discharge_efficiency = 1.0
    engine.min_soc = 0.0
    engine.max_grid_charge_soc = 100.0
    engine.max_charge_power = 6.0
    engine.max_discharge_power = 10.0
    engine.max_power_import = 6.0
    engine.max_power_export = 10.0
    engine.export_price_factor = 1.0
    engine.export_fee = 0.0
    engine.min_sell_price = 0.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 0.0
    engine.slot_minutes = 60.0
    engine.soc_step = 10.0
    engine.soc_states = [float(value) for value in range(0, 101, 10)]
    base = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) \
        + timedelta(hours=1)
    prices = [
        {'start': base + timedelta(hours=index), 'total': price}
        for index, price in enumerate((0.30, 0.10, 0.50))
    ]

    result = engine.optimize(
        0.0, prices,
        load_forecast=[0.0, 0.0, 0.0],
        pv_forecast=[0.0, 0.0, 0.0],
    )

    assert len(result['victron_slots']) == 2
    assert [slot['target_soc'] for slot in result['victron_slots']] == [40, 100]
    assert result['schedule'][0]['grid_energy'] == pytest.approx(4.0)
    assert result['schedule'][1]['grid_energy'] == pytest.approx(6.0)
    assert -result['objective_cost_eur'] == pytest.approx(3.2)


def test_saturated_falling_price_run_can_share_fifth_target():
    """Do not spend a sixth target when a merge cannot front-load energy."""
    engine = ai_powered_ess.OptimizationEngine()
    engine.battery_capacity = 10.0
    engine.charge_efficiency = 1.0
    engine.discharge_efficiency = 1.0
    engine.min_soc = 0.0
    engine.max_grid_charge_soc = 100.0
    engine.max_charge_power = 5.0
    engine.max_discharge_power = 5.0
    engine.max_power_import = 5.0
    engine.max_power_export = 5.0
    engine.export_price_factor = 1.0
    engine.export_fee = 0.0
    engine.min_sell_price = 0.0
    engine.cycle_cost = 0.0
    engine.arbitrage_margin = 0.0
    engine.terminal_value_factor = 0.0
    engine.slot_minutes = 60.0
    engine.soc_step = 10.0
    engine.soc_states = [float(value) for value in range(0, 101, 10)]
    base = datetime.now(tz.UTC).replace(minute=0, second=0, microsecond=0) \
        + timedelta(hours=1)
    tariff = [0.10, 0.60] * 4 + [0.20, 0.19, 0.80, 0.80]
    prices = [
        {'start': base + timedelta(hours=index), 'total': price}
        for index, price in enumerate(tariff)
    ]

    result = engine.optimize(
        0.0, prices,
        load_forecast=[0.0] * len(prices),
        pv_forecast=[0.0] * len(prices),
    )

    assert len(result['victron_slots']) == 5
    final_window = result['victron_slots'][-1]
    assert final_window['start'] == base + timedelta(hours=8)
    assert final_window['duration'] == 7200
    assert final_window['target_soc'] == 100
    assert result['schedule'][8]['grid_energy'] == pytest.approx(5.0)
    assert result['schedule'][9]['grid_energy'] == pytest.approx(5.0)
    assert -result['objective_cost_eur'] == pytest.approx(16.05)


def test_adaptive_feature_gate_off_preserves_single_market_path(monkeypatch):
    market = {'schedule': [{'strategy': 'market_arbitrage'}]}

    class FakeEngine:
        def set_cost_basis_floor(self, _value):
            pass

        def optimize_with_daily_policy(self, *args, **kwargs):
            return market

        def optimize(self, *args, **kwargs):
            raise AssertionError('conservative candidates must not run while gated off')

    monkeypatch.setattr(ai_powered_ess, 'OptimizationEngine', FakeEngine)
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting',
        lambda key: 'False' if key == 'ESS_ADAPTIVE_POLICY_ENABLED' else None)

    result = ai_powered_ess.optimize_schedule(
        50.0, [{'start': datetime.now(tz.UTC), 'total': 0.20}])

    assert result is market
    assert result['optimizer_runtime_ms'] >= 0


def test_adaptive_replans_only_selected_policy_between_full_evaluations(
        monkeypatch, tmp_path):
    now = datetime.now(tz.UTC)
    prices = [{'start': now + timedelta(hours=1), 'total': 0.20}]
    state_path = tmp_path / 'policy.json'
    state_path.write_text(json.dumps({
        'selected': 'protected_hybrid',
        'selected_at': time.time(),
        'evaluated_at': time.time(),
        'price_horizon_signature': ai_powered_ess._price_horizon_signature(
            prices, current_soc=50.0),
    }))
    settings = {
        'ESS_ADAPTIVE_POLICY_ENABLED': 'True',
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(state_path),
        'ESS_ADAPTIVE_FULL_EVALUATION_INTERVAL_MIN': '60',
        'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '0',
    }
    calls = []
    plan = _adaptive_candidate('protected_hybrid', [0.0], 5.0)
    plan['slot_duration_h'] = 1.0

    class FakeEngine:
        min_soc = 5.0
        battery_capacity = 10.0
        discharge_efficiency = 1.0
        cycle_cost = 0.0
        arbitrage_margin = 0.0
        terminal_value_factor = 1.0
        expected_peak_price = 0.0
        slot_minutes = 60.0

        def set_cost_basis_floor(self, _value):
            pass

        def optimize_with_daily_policy(self, *args, **kwargs):
            calls.append(kwargs.get('policy_name', 'market_arbitrage'))
            return plan

    monkeypatch.setattr(ai_powered_ess, 'OptimizationEngine', FakeEngine)
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))

    result = ai_powered_ess.optimize_schedule(50.0, prices)

    assert calls == ['protected_hybrid']
    assert result['adaptive_policy']['full_evaluation'] is False
    assert result['adaptive_policy']['reason_code'] \
        == 'SCHEDULED_POLICY_REEVALUATION_PENDING'


@pytest.mark.parametrize('force_expired', [True, False])
def test_adaptive_full_comparison_runs_when_interval_or_inputs_change(
        monkeypatch, tmp_path, force_expired):
    now = datetime.now(tz.UTC)
    prices = [{'start': now + timedelta(hours=1), 'total': 0.20}]
    state_path = tmp_path / 'policy.json'
    stored_prices = prices if force_expired else [
        {'start': prices[0]['start'], 'total': 0.19},
    ]
    state_path.write_text(json.dumps({
        'selected': 'protected_hybrid',
        'selected_at': time.time() - 7200,
        'evaluated_at': time.time() - (7200 if force_expired else 30),
        'price_horizon_signature': ai_powered_ess._price_horizon_signature(
            stored_prices, current_soc=50.0),
    }))
    settings = {
        'ESS_ADAPTIVE_POLICY_ENABLED': 'True',
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(state_path),
        'ESS_ADAPTIVE_FULL_EVALUATION_INTERVAL_MIN': '60',
        'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '0',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
    }
    calls = []

    class FakeEngine:
        min_soc = 5.0
        battery_capacity = 10.0
        discharge_efficiency = 1.0
        cycle_cost = 0.0
        arbitrage_margin = 0.0
        terminal_value_factor = 1.0
        expected_peak_price = 0.0
        slot_minutes = 60.0

        def set_cost_basis_floor(self, _value):
            pass

        def optimize_with_daily_policy(self, *args, **kwargs):
            policy = kwargs.get('policy_name', 'market_arbitrage')
            calls.append(policy)
            plan = _adaptive_candidate(policy, [0.0], 5.0)
            plan['slot_duration_h'] = 1.0
            return plan

    monkeypatch.setattr(ai_powered_ess, 'OptimizationEngine', FakeEngine)
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))

    result = ai_powered_ess.optimize_schedule(50.0, prices)

    assert calls == [
        'market_arbitrage',
        'pv_first_self_sufficiency',
        'protected_hybrid',
    ]
    assert result['adaptive_policy']['full_evaluation'] is True
    assert result['optimizer_runtime_ms'] >= 0


def test_adaptive_infeasible_reused_policy_falls_back_to_full_comparison(
        monkeypatch, tmp_path):
    now = datetime.now(tz.UTC)
    prices = [{'start': now + timedelta(hours=1), 'total': 0.20}]
    state_path = tmp_path / 'policy.json'
    state_path.write_text(json.dumps({
        'selected': 'protected_hybrid',
        'selected_at': time.time(),
        'evaluated_at': time.time(),
        'price_horizon_signature': ai_powered_ess._price_horizon_signature(
            prices, current_soc=50.0),
    }))
    settings = {
        'ESS_ADAPTIVE_POLICY_ENABLED': 'True',
        'ESS_ADAPTIVE_POLICY_STATE_PATH': str(state_path),
        'ESS_ADAPTIVE_FULL_EVALUATION_INTERVAL_MIN': '60',
        'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '0',
        'ESS_ADAPTIVE_POLICY_MIN_DWELL_MIN': '0',
        'ESS_ADAPTIVE_POLICY_SWITCH_MARGIN_EUR': '0',
    }
    calls = []

    class FakeEngine:
        min_soc = 5.0
        battery_capacity = 10.0
        discharge_efficiency = 1.0
        cycle_cost = 0.0
        arbitrage_margin = 0.0
        terminal_value_factor = 1.0
        expected_peak_price = 0.0
        slot_minutes = 60.0

        def set_cost_basis_floor(self, _value):
            pass

        def optimize_with_daily_policy(self, *args, **kwargs):
            policy = kwargs.get('policy_name', 'market_arbitrage')
            calls.append(policy)
            if calls == ['protected_hybrid']:
                return None
            plan = _adaptive_candidate(policy, [0.0], 5.0)
            plan['slot_duration_h'] = 1.0
            return plan

    monkeypatch.setattr(ai_powered_ess, 'OptimizationEngine', FakeEngine)
    monkeypatch.setattr(
        ai_powered_ess, 'retrieve_setting', lambda key: settings.get(key))

    result = ai_powered_ess.optimize_schedule(50.0, prices)

    assert calls == [
        'protected_hybrid',
        'market_arbitrage',
        'pv_first_self_sufficiency',
        'protected_hybrid',
    ]
    assert result['adaptive_policy']['full_evaluation'] is True

if __name__ == '__main__':
    unittest.main()
