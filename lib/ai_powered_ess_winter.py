"""Isolated Winter Mode ESS optimizer.

Winter Mode prioritises household self-sufficiency between low-price
replenishment windows.  Routine battery-to-grid export is disabled.  A second,
independently evaluated candidate may export only when an exceptional spread
clears all configured economic guardrails and the post-sale battery still
protects forecast household demand until the next replenishment window.

This module is deliberately self-contained.  In particular, it must not import
``lib.ai_powered_ess``: summer planning is a separately selected engine and
changes to this policy must not alter it through shared implementation state.
"""

from __future__ import annotations

import logging
import math
import json
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from dateutil import parser as date_parser

from lib.config_retrieval import retrieve_setting


EPS = 1e-6
SOC_STEP = 5.0
MAX_VICTRON_CHARGE_WINDOWS = 5

# Code-only initial policy constants.  They are intentionally conservative and
# can be promoted to configuration after historical backtesting demonstrates a
# real operational need.
WINTER_UNCERTAINTY_RATE = 0.12
WINTER_UNCERTAINTY_MIN_KWH = 0.50
WINTER_UNCERTAINTY_MAX_KWH = 3.00
WINTER_UNKNOWN_HORIZON_HOURS = 4.0
WINTER_LOW_PRICE_QUANTILE = 0.30
WINTER_EXCEPTIONAL_HURDLE_EUR_PER_KWH = 0.20
WINTER_EXCEPTIONAL_MIN_BENEFIT_EUR = 1.00
WINTER_MIN_ADAPTIVE_SOC_STEP_PCT = 0.25
WINTER_UNCERTAINTY_HISTORY_DAYS = 21
WINTER_UNCERTAINTY_MIN_SAMPLES = 48
WINTER_UNCERTAINTY_QUANTILE = 0.80
WINTER_UNCERTAINTY_MAX_RATE = 0.25


def _safe_float(setting_name: str, default: float) -> float:
    raw = retrieve_setting(setting_name)
    if raw in (None, "", "None"):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.warning(
            "WINTER_ESS: Unable to parse %s value %r; using %s.",
            setting_name, raw, default,
        )
        return default


def _coerce_datetime(value) -> datetime:
    """Return a datetime for Tibber ISO strings and native datetimes."""
    if isinstance(value, datetime):
        return value
    return date_parser.parse(value)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _percentile(values, quantile):
    values = sorted(float(value) for value in values)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = _clamp(float(quantile), 0.0, 1.0) * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def control_action_for(action, soc_start, soc_end, grid_energy):
    if action == 'buy' and grid_energy > EPS:
        return 'BUY'
    if action == 'sell' and soc_end < soc_start - EPS:
        return 'SELL'
    if action == 'hold' and grid_energy > EPS:
        return 'RETAIN'
    return 'IDLE'


def _history_revision(history_dir):
    """Return a cheap cache key that changes when hot history changes."""
    try:
        files = list(Path(history_dir).glob('ess-*.ndjson'))
        stats = [path.stat() for path in files]
        return (
            len(stats),
            max((stat.st_mtime_ns for stat in stats), default=0),
            sum(stat.st_size for stat in stats),
        )
    except OSError:
        return (0, 0, 0)


@lru_cache(maxsize=8)
def _load_uncertainty_model(history_dir, _revision=None):
    """Estimate under-forecast risk from recent measured settlement records.

    Only positive load misses matter for winter coverage. Reads are capped to the
    newest 21 hot-history files and cached until their revision changes, keeping
    the optimizer suitable for Pi-class hardware. Sparse or malformed history
    uses the bounded policy fallback.
    """
    fallback = {
        'source': 'bounded_fallback',
        'samples': 0,
        'rate': WINTER_UNCERTAINTY_RATE,
        'quantile': WINTER_UNCERTAINTY_QUANTILE,
        'rate_cap': WINTER_UNCERTAINTY_MAX_RATE,
    }
    try:
        files = sorted(Path(history_dir).glob('ess-*.ndjson'))[
            -WINTER_UNCERTAINTY_HISTORY_DAYS:]
        misses = []
        predicted_values = []
        for path in files:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                        if record.get('kind') != 'settlement' or record.get('incomplete'):
                            continue
                        predicted = float(record.get('predicted_load_kwh'))
                        # Only the EV-excluded, quality-classified base load is
                        # safe training input. Legacy raw actual_load may contain
                        # car charging and would inflate household protection.
                        if record.get('load_meter_quality') != 'measured':
                            continue
                        if record.get('ev_meter_quality') != 'measured':
                            continue
                        actual_raw = record.get('base_load_kwh')
                        if actual_raw is None:
                            continue
                        actual = float(actual_raw)
                        if predicted < 0 or actual < 0:
                            continue
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    misses.append(max(0.0, actual - predicted))
                    predicted_values.append(predicted)
        if len(misses) < WINTER_UNCERTAINTY_MIN_SAMPLES:
            fallback['samples'] = len(misses)
            return fallback
        typical_prediction = max(0.05, _percentile(predicted_values, 0.50))
        p80_underforecast = _percentile(misses, WINTER_UNCERTAINTY_QUANTILE)
        return {
            'source': 'settlement_history',
            'samples': len(misses),
            'rate': _clamp(
                p80_underforecast / typical_prediction,
                WINTER_UNCERTAINTY_RATE,
                WINTER_UNCERTAINTY_MAX_RATE,
            ),
            'quantile': WINTER_UNCERTAINTY_QUANTILE,
            'rate_cap': WINTER_UNCERTAINTY_MAX_RATE,
        }
    except (OSError, ValueError) as exc:
        logging.debug("WINTER_ESS: load uncertainty history unavailable (%s).", exc)
        return fallback


class OptimizationEngine:
    """Small dynamic-programming planner dedicated to Winter Mode."""

    def __init__(self):
        self.battery_capacity = max(0.1, _safe_float('BATTERY_CAPACITY_KWH', 45.0))
        self.charge_efficiency = _clamp(
            _safe_float('AC_DC_CHARGE_EFFICIENCY', 0.90), 0.01, 1.0)
        self.discharge_efficiency = _clamp(
            _safe_float('AC_DC_DISCHARGE_EFFICIENCY', 0.90), 0.01, 1.0)
        self.max_power_import = max(0.0, _safe_float('ESS_MAX_GRID_IMPORT_KW', 10.0))
        self.max_power_export = max(0.0, _safe_float('ESS_MAX_GRID_EXPORT_KW', 10.0))
        self.max_charge_power = max(
            0.0, _safe_float('ESS_MAX_CHARGE_KW', self.max_power_import))
        self.max_discharge_power = max(
            0.0, _safe_float('ESS_MAX_DISCHARGE_KW', self.max_power_export))
        self.export_price_factor = _safe_float('ESS_EXPORT_PRICE_FACTOR', 1.0)
        self.export_fee = _safe_float('ESS_EXPORT_FEE', 0.0)
        self.min_sell_price = _safe_float('ESS_MIN_SELL_PRICE', 0.0)
        self.cycle_cost = max(0.0, _safe_float('ESS_BATTERY_CYCLE_COST', 0.0))
        self.arbitrage_margin = max(0.0, _safe_float('ESS_ARBITRAGE_MARGIN', 0.0))
        self.slot_minutes = max(1.0, _safe_float('OPTIMIZER_SLOT_MINUTES', 15.0))
        self.daily_load_kwh = max(
            0.0, _safe_float('DAILY_HOME_ENERGY_CONSUMPTION', 16.0))
        history_dir = str(retrieve_setting('HISTORY_DIR') or 'data/history')
        self.uncertainty_model = _load_uncertainty_model(
            history_dir, _history_revision(history_dir))

        # Mode selection is external and frozen at process start.  A winter
        # engine always uses the explicit winter reserve, never calendar logic.
        self.min_soc = _clamp(_safe_float('MIN_SOC_RESERVE_WINTER', 20.0), 0.0, 100.0)
        self.max_soc = 100.0
        # Preserve the operator's ceiling exactly. A conflicting ceiling below
        # the winter reserve is not silently raised; the optimizer instead
        # recovers only as far as permitted and reports the configuration fault.
        self.max_grid_charge_soc = _clamp(
            _safe_float('ESS_MAX_GRID_CHARGE_SOC', 100.0), 0.0, 100.0)

        self.soc_step = max(1.0, _safe_float('OPTIMIZER_SOC_STEP_PCT', SOC_STEP))
        self.configured_soc_step = self.soc_step
        self.soc_states = self._build_soc_states(self.soc_step)

        self.cost_basis_eur_per_dc_kwh = 0.0
        self.cost_basis_sell_floor = 0.0
        self.initial_protected_soc = 0.0
        self._last_warning = None

    def _build_soc_states(self, step, opening_soc=None):
        states = {0.0, 100.0, self.min_soc, self.max_grid_charge_soc}
        if opening_soc is not None:
            states.add(_clamp(float(opening_soc), 0.0, 100.0))
        value = 0.0
        while value < 100.0 + EPS:
            states.add(round(min(100.0, value), 6))
            value += step
        return sorted(states)

    def _adapt_soc_lattice(self, slots, opening_soc):
        """Choose a step small enough for no-export household self-supply.

        A 1% transition in a 45 kWh battery releases roughly 0.43 kWh AC,
        greater than a typical quarter-hour winter load. The fixed lattice would
        call that transition an export and reject it. Bound the winter lattice by
        the smallest material positive net load, but never below 0.25 percentage
        points. Reachability slicing in the DP keeps this tractable.
        """
        positive_net = sorted(
            slot['load'] - slot['pv']
            for slot in slots
            if slot['load'] - slot['pv'] > 0.05
        )
        if positive_net:
            representative_small_load = _percentile(positive_net, 0.10)
            load_step = (
                representative_small_load / self.discharge_efficiency
                / self.battery_capacity * 100.0
            )
            self.soc_step = min(
                self.configured_soc_step,
                max(WINTER_MIN_ADAPTIVE_SOC_STEP_PCT, load_step),
            )
        else:
            self.soc_step = self.configured_soc_step
        self.soc_states = self._build_soc_states(self.soc_step, opening_soc)

    def set_cost_basis_floor(self, basis_eur_per_dc_kwh):
        try:
            basis = max(0.0, float(basis_eur_per_dc_kwh))
        except (TypeError, ValueError):
            basis = 0.0
        self.cost_basis_eur_per_dc_kwh = basis
        self.cost_basis_sell_floor = basis / self.discharge_efficiency if basis else 0.0

    def _sell_price(self, buy_price):
        return buy_price * self.export_price_factor - self.export_fee

    def _snap_soc(self, value):
        value = _clamp(float(value), 0.0, 100.0)
        return min(self.soc_states, key=lambda state: abs(state - value))

    @staticmethod
    def _lookup_forecast(forecast, index, slot_start, default):
        if forecast is None:
            return default
        if isinstance(forecast, dict):
            return forecast.get(slot_start, forecast.get(slot_start.isoformat(), default))
        if index < len(forecast):
            return forecast[index]
        return default

    @staticmethod
    def _detect_slot_duration_h(prices):
        gaps = []
        for index in range(1, len(prices)):
            gap = (prices[index]['start'] - prices[index - 1]['start']).total_seconds()
            if gap > 0:
                gaps.append(gap / 3600.0)
        return min(gaps) if gaps else 1.0

    def _normalise(self, price_data, load_forecast, pv_forecast):
        prices = []
        for index, point in enumerate(price_data or []):
            try:
                price = float(point['total'])
                if not math.isfinite(price):
                    raise ValueError('non-finite price')
                prices.append((index, {
                    'start': _coerce_datetime(point['start']),
                    'total': price,
                }))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                logging.warning("WINTER_ESS: Skipping malformed price point %r (%s).", point, exc)
        prices.sort(key=lambda item: item[1]['start'])
        if not prices:
            return [], 1.0

        native = [point for _, point in prices]
        native_h = self._detect_slot_duration_h(native)
        target_h = self.slot_minutes / 60.0
        subdivisions = max(1, int(round(native_h / target_h)))
        slot_h = native_h / subdivisions
        default_native_load = self.daily_load_kwh * native_h / 24.0
        expanded = []
        for original_index, point in prices:
            try:
                load = float(self._lookup_forecast(
                    load_forecast, original_index, point['start'], default_native_load))
                if not math.isfinite(load):
                    raise ValueError('non-finite load')
                load = max(0.0, load)
            except (TypeError, ValueError):
                load = default_native_load
            try:
                pv = float(self._lookup_forecast(
                    pv_forecast, original_index, point['start'], 0.0))
                if not math.isfinite(pv):
                    raise ValueError('non-finite PV')
                pv = max(0.0, pv)
            except (TypeError, ValueError):
                pv = 0.0
            for sub_index in range(subdivisions):
                expanded.append({
                    'start': point['start'] + timedelta(hours=slot_h * sub_index),
                    'buy': point['total'],
                    'load': load / subdivisions,
                    'pv': pv / subdivisions,
                })

        tzinfo = expanded[0]['start'].tzinfo
        now = datetime.now(tzinfo)
        keep_after = now - timedelta(hours=slot_h)
        return [slot for slot in expanded if slot['start'] > keep_after], slot_h

    def _replenishment_windows(self, slots):
        """Return at most five low-price index ranges used for grid charging.

        Constraining charge eligibility before the DP means the published SoC
        trajectory can never depend on a sixth Victron window that post-processing
        silently drops.  If many isolated troughs exist, retain the final trough
        (the last known fallback) plus the cheapest earlier opportunities.
        """
        if not slots:
            return []
        threshold = _percentile(
            [slot['buy'] for slot in slots], WINTER_LOW_PRICE_QUANTILE)
        low_indices = [
            index for index, slot in enumerate(slots)
            if slot['buy'] <= threshold + EPS or slot['buy'] < 0
        ]
        if not low_indices:
            low_indices = [min(range(len(slots)), key=lambda i: slots[i]['buy'])]

        windows = []
        start = previous = low_indices[0]
        for index in low_indices[1:]:
            if index == previous + 1:
                previous = index
                continue
            windows.append((start, previous))
            start = previous = index
        windows.append((start, previous))

        if len(windows) <= MAX_VICTRON_CHARGE_WINDOWS:
            return windows
        final = windows[-1]
        earlier = sorted(
            windows[:-1],
            key=lambda window: (
                min(slots[i]['buy'] for i in range(window[0], window[1] + 1)),
                window[0],
            ),
        )[:MAX_VICTRON_CHARGE_WINDOWS - 1]
        return sorted(earlier + [final])

    def _uncertainty_kwh(self, forecast_kwh):
        return _clamp(
            max(0.0, forecast_kwh) * self.uncertainty_model['rate'],
            WINTER_UNCERTAINTY_MIN_KWH,
            WINTER_UNCERTAINTY_MAX_KWH,
        )

    def _coverage(self, slots, slot_h, windows):
        """Build post-trough SoC checkpoints and export-protection envelopes."""
        positive_net = [max(0.0, slot['load'] - slot['pv']) for slot in slots]
        stress_house_load = [max(0.0, slot['load']) for slot in slots]
        dates = {slot['start'].date() for slot in slots}
        average_load_kw = (
            sum(slot['load'] for slot in slots) / max(EPS, len(slots) * slot_h))
        terminal_house = 0.0
        if len(dates) <= 1:
            terminal_house = average_load_kw * WINTER_UNKNOWN_HORIZON_HOURS

        checkpoints = {}
        window_details = []
        for position, (start, end) in enumerate(windows):
            next_start = windows[position + 1][0] if position + 1 < len(windows) else len(slots)
            house = sum(positive_net[end + 1:next_start])
            if position == len(windows) - 1:
                house += terminal_house
            uncertainty = self._uncertainty_kwh(house)
            protected_dc = house / self.discharge_efficiency + uncertainty
            required_soc = self.min_soc + protected_dc / self.battery_capacity * 100.0
            feasible_soc = min(self.max_grid_charge_soc, required_soc)
            warning = None
            if feasible_soc + EPS < required_soc:
                warning = 'coverage_limited_by_configured_max_soc'
            checkpoints[end] = feasible_soc
            window_details.append({
                'start': start,
                'end': end,
                'next_start': next_start if next_start < len(slots) else None,
                'house_kwh': house,
                'uncertainty_kwh': uncertainty,
                'protected_dc_kwh': protected_dc,
                'required_soc': required_soc,
                'feasible_soc': feasible_soc,
                'warning': warning,
            })

        # For every potential export slot, protect the positive net household
        # requirement until the next selected replenishment opportunity.
        export_envelope = []
        for index in range(len(slots)):
            next_start = next((start for start, _ in windows if start > index), None)
            stop = next_start if next_start is not None else len(slots)
            # Exceptional exports are accepted only under a near-zero-PV stress
            # replay. Forecast PV may reduce the normal replenishment target but
            # can never be relied upon to make stored energy safe to sell.
            # Include the current slot: the forecast trajectory may show PV
            # covering its load, but the zero-PV stress case must reserve battery
            # energy for that load before declaring any concurrent export safe.
            house = sum(stress_house_load[index:stop])
            if next_start is None:
                house += terminal_house
            uncertainty = self._uncertainty_kwh(house)
            protected_dc = house / self.discharge_efficiency + uncertainty
            export_envelope.append(min(
                100.0,
                self.min_soc + protected_dc / self.battery_capacity * 100.0,
            ))
        return checkpoints, export_envelope, window_details

    def _exceptional_economics(self, slots):
        low_buy = min(slot['buy'] for slot in slots)
        high_sell = max(self._sell_price(slot['buy']) for slot in slots)
        # Convert all DC-denominated costs to the AC-export price dimension.
        # One exported AC kWh consumes 1/discharge_efficiency DC kWh.
        recovery = (
            low_buy / self.charge_efficiency + self.cycle_cost + self.arbitrage_margin
        ) / self.discharge_efficiency
        required_sell = (
            recovery + WINTER_EXCEPTIONAL_HURDLE_EUR_PER_KWH
        )
        return {
            'low_buy': low_buy,
            'high_sell': high_sell,
            'spread': high_sell - low_buy,
            'required_sell': max(self.min_sell_price, required_sell),
            'qualifies': high_sell + EPS >= max(self.min_sell_price, required_sell),
        }

    def _run_candidate(self, slots, slot_h, windows, checkpoints,
                       export_envelope, allow_exceptional_export,
                       discharge_blocked_starts=None):
        steps = len(slots)
        allowed_charge = {
            index for start, end in windows for index in range(start, end + 1)
        }
        economics = self._exceptional_economics(slots)
        dp = [{state: float('inf') for state in self.soc_states} for _ in range(steps + 1)]
        parent = [{state: None for state in self.soc_states} for _ in range(steps + 1)]
        start_soc = self.initial_protected_soc
        dp[0][start_soc] = 0.0

        for index, slot in enumerate(slots):
            net_load = slot['load'] - slot['pv']
            buy = slot['buy']
            sell = self._sell_price(buy)
            for soc in self.soc_states:
                base = dp[index][soc]
                if base == float('inf'):
                    continue
                max_down_pct = self.max_discharge_power * slot_h / self.battery_capacity * 100.0
                max_up_pct = self.max_charge_power * slot_h / self.battery_capacity * 100.0
                first_state = bisect_left(self.soc_states, soc - max_down_pct - EPS)
                last_state = bisect_right(self.soc_states, soc + max_up_pct + EPS)
                for next_soc in self.soc_states[first_state:last_state]:
                    # If telemetry starts below reserve (or max-charge SoC is
                    # configured below it), never discharge farther but permit a
                    # monotonic recovery/hold trajectory instead of inventing an
                    # opening reserve.
                    if next_soc < self.min_soc - EPS and next_soc < soc - EPS:
                        continue
                    dc_change = (next_soc - soc) / 100.0 * self.battery_capacity
                    if (dc_change < -EPS and round(slot['start'].timestamp())
                            in (discharge_blocked_starts or set())):
                        continue
                    if dc_change >= 0:
                        if dc_change > self.max_charge_power * slot_h + EPS:
                            continue
                        ac_for_battery = dc_change / self.charge_efficiency
                    else:
                        if -dc_change > self.max_discharge_power * slot_h + EPS:
                            continue
                        ac_for_battery = dc_change * self.discharge_efficiency
                    grid_energy = net_load + ac_for_battery
                    if grid_energy > self.max_power_import * slot_h + EPS:
                        continue
                    if -grid_energy > self.max_power_export * slot_h + EPS:
                        continue

                    active_grid_charge = dc_change > EPS and grid_energy > EPS
                    if active_grid_charge:
                        if index not in allowed_charge:
                            continue
                        if next_soc > self.max_grid_charge_soc + EPS:
                            continue

                    active_export = dc_change < -EPS and grid_energy < -EPS
                    if active_export:
                        if not allow_exceptional_export or not economics['qualifies']:
                            continue
                        if sell < economics['required_sell'] - EPS:
                            continue
                        if next_soc < export_envelope[index] - EPS:
                            continue
                        if sell < self.cost_basis_sell_floor - EPS \
                                and next_soc < self.initial_protected_soc - EPS:
                            continue

                    checkpoint = checkpoints.get(index)
                    if checkpoint is not None and next_soc < checkpoint - EPS:
                        continue

                    import_kwh = max(0.0, grid_energy)
                    export_kwh = max(0.0, -grid_energy)
                    cost = import_kwh * buy - export_kwh * sell
                    if dc_change < -EPS:
                        cost += -dc_change * (self.cycle_cost + self.arbitrage_margin)
                    total = base + cost
                    if total < dp[index + 1][next_soc] - EPS:
                        dp[index + 1][next_soc] = total
                        parent[index + 1][next_soc] = (soc, grid_energy)

        viable = [state for state in self.soc_states if dp[steps][state] < float('inf')]
        if not viable:
            return None
        end_soc = min(viable, key=lambda state: dp[steps][state])
        objective = dp[steps][end_soc]
        schedule = []
        current_soc = end_soc
        for position in range(steps, 0, -1):
            previous = parent[position][current_soc]
            if previous is None:
                return None
            previous_soc, grid_energy = previous
            slot = slots[position - 1]
            action = self._classify_action(previous_soc, current_soc, grid_energy)
            schedule.insert(0, {
                'time': slot['start'],
                'action': action,
                'soc_start': previous_soc,
                'soc_end': current_soc,
                'grid_energy': round(grid_energy, 4),
                'pv': round(slot['pv'], 4),
                'load': round(slot['load'], 4),
                'price': slot['buy'],
                'sell': round(self._sell_price(slot['buy']), 4),
            })
            current_soc = previous_soc
        return {
            'schedule': schedule,
            'objective_cost': objective,
            'replenishment_windows': windows,
        }

    @staticmethod
    def _classify_action(soc_start, soc_end, grid_energy):
        if soc_end > soc_start + EPS:
            return 'buy'
        if grid_energy < -EPS:
            return 'sell'
        if soc_end < soc_start - EPS:
            return 'self_supply'
        return 'hold'

    def _reason(self, step, exceptional):
        action = step['action']
        if action == 'buy':
            if step['grid_energy'] > EPS:
                return 'WINTER_REPLENISH', 'Charging in a winter price trough to cover household demand'
            return 'PV_CHARGING', 'Forecast solar surplus is charging the battery'
        if action == 'sell' and step['soc_end'] < step['soc_start'] - EPS:
            return 'WINTER_EXCEPTIONAL_EXPORT', 'Exceptional spread export above the protected household reserve'
        if action == 'self_supply':
            return 'WINTER_SELF_SUPPLY', 'Using stored low-cost energy for household demand'
        if exceptional:
            return 'WINTER_PROTECT_HOUSE', 'Holding energy reserved for household demand before the next trough'
        return 'WINTER_RETAIN', 'Retaining the winter reserve while grid or solar covers demand'

    def _victron_slots(self, schedule, slot_seconds, replenishment_windows):
        """Build at most one executable charge schedule per selected trough.

        The DP may choose separated charge steps inside a contiguous low-price
        trough. Victron has no equivalent concept, so span those steps with one
        schedule targeting the highest planned SoC in that trough. This is
        conservative (it may charge earlier within the same trough) and can never
        omit energy assumed by the protected trajectory.
        """
        groups = []
        for window_start, window_end in replenishment_windows:
            buy_indices = [
                index for index in range(window_start, min(window_end + 1, len(schedule)))
                if schedule[index]['action'] == 'buy'
                and schedule[index]['grid_energy'] > EPS
            ]
            if not buy_indices:
                continue
            first = min(buy_indices)
            last = max(buy_indices)
            max_target = max(schedule[index]['soc_end'] for index in buy_indices)
            # Victron accepts integer targets. Floor rather than round so a user
            # ceiling such as 64.5% can never become a 65% hardware instruction.
            target = math.floor(min(max_target, self.max_grid_charge_soc) + EPS)
            groups.append({
                'start': schedule[first]['time'],
                'duration': (last - first + 1) * slot_seconds,
                'target_soc': target,
            })
        return groups

    def _guardrails(self):
        return {
            'max_grid_charge_soc': self.max_grid_charge_soc,
            'min_sell_price': self.min_sell_price,
            'cost_basis_eur_per_dc_kwh': self.cost_basis_eur_per_dc_kwh,
            'cost_basis_sell_floor': self.cost_basis_sell_floor,
            'initial_protected_soc': self.initial_protected_soc,
            'battery_cycle_cost': self.cycle_cost,
            'arbitrage_margin': self.arbitrage_margin,
        }

    def _finish(self, candidate, slot_h, winter_policy):
        schedule = candidate['schedule']
        slot_seconds = int(round(slot_h * 3600.0))
        exceptional = winter_policy['selected_candidate'] == 'exceptional_arbitrage'
        for step in schedule:
            code, reason = self._reason(step, exceptional)
            step['reason_code'] = code
            step['reason'] = reason
            step['control_action'] = control_action_for(
                step['action'], step['soc_start'], step['soc_end'], step['grid_energy'])
        first = schedule[0]
        active_sell = first['action'] == 'sell' and first['soc_end'] < first['soc_start'] - EPS
        pv_surplus = first['action'] == 'sell' and not active_sell
        export_setpoint = _safe_float('ESS_EXPORT_AC_SETPOINT', -10000.0)
        planned_watts = first['grid_energy'] / slot_h * 1000.0 if slot_h else 0.0
        return {
            'schedule': schedule,
            'victron_slots': self._victron_slots(
                schedule, slot_seconds, candidate['replenishment_windows']),
            'optimizer_guardrails': self._guardrails(),
            'winter_policy': winter_policy,
            'optimizer_mode': 'winter',
            'setpoint': float(round(max(planned_watts, export_setpoint))) if active_sell else 0.0,
            'mode': first['action'],
            'control_action': first['control_action'],
            'reason': first['reason'],
            'reason_code': first['reason_code'],
            'grid_assist': first['action'] == 'hold',
            'pv_surplus': pv_surplus,
            'current_price': first['price'],
            'limit_feed_in': first['price'] < 0,
            'slot_duration_h': slot_h,
        }

    def optimize(self, current_soc_percent, price_data, load_forecast=None,
                 pv_forecast=None, discharge_blocked_slots=None):
        slots, slot_h = self._normalise(price_data, load_forecast, pv_forecast)
        if not slots:
            logging.warning("WINTER_ESS: No future usable price data.")
            return None
        discharge_blocked_starts = set()
        for value in discharge_blocked_slots or ():
            try:
                discharge_blocked_starts.add(round(_coerce_datetime(value).timestamp()))
            except (TypeError, ValueError):
                logging.warning("WINTER_ESS: Ignoring malformed discharge-blocked slot %r.", value)
        # Represent telemetry exactly at the opening boundary. Nearest-state
        # snapping can otherwise create or discard up to half a DP step of energy.
        opening_soc = _clamp(float(current_soc_percent), 0.0, 100.0)
        self._adapt_soc_lattice(slots, opening_soc)
        self.initial_protected_soc = opening_soc
        windows = self._replenishment_windows(slots)
        checkpoints, export_envelope, details = self._coverage(slots, slot_h, windows)

        normal = self._run_candidate(
            slots, slot_h, windows, checkpoints, export_envelope, False,
            discharge_blocked_starts)
        warning = next((item['warning'] for item in details if item['warning']), None)
        if self.max_grid_charge_soc < self.min_soc - EPS:
            warning = 'max_grid_charge_soc_below_winter_reserve'
        if normal is None:
            # An unreachable checkpoint (usually a short trough plus a strict
            # power cap) must not invent energy. Retry without the hard coverage
            # checkpoint, surface the degraded guarantee, and retain all physical
            # limits and the configured reserve.
            normal = self._run_candidate(
                slots, slot_h, windows, {}, export_envelope, False,
                discharge_blocked_starts)
            warning = 'coverage_infeasible_safest_feasible_plan'
        if normal is None:
            logging.error("WINTER_ESS: No physically feasible self-sufficiency plan.")
            return None

        economics = self._exceptional_economics(slots)
        exceptional = None
        benefit = 0.0
        reason_code = 'WINTER_EXCEPTIONAL_SPREAD_REJECTED'
        reason = 'Normal winter self-sufficiency selected; exceptional spread hurdle not met'
        if economics['qualifies']:
            exceptional = self._run_candidate(
                slots, slot_h, windows, checkpoints, export_envelope, True,
                discharge_blocked_starts)
            if exceptional is not None:
                benefit = normal['objective_cost'] - exceptional['objective_cost']
            if exceptional is not None and benefit >= WINTER_EXCEPTIONAL_MIN_BENEFIT_EUR - EPS:
                selected = exceptional
                selected_name = 'exceptional_arbitrage'
                reason_code = 'WINTER_EXCEPTIONAL_ACCEPTED'
                reason = 'Exceptional export materially improves cost while preserving household coverage'
            else:
                selected = normal
                selected_name = 'self_sufficiency'
                reason_code = 'WINTER_EXCEPTIONAL_BENEFIT_REJECTED'
                reason = 'Exceptional spread did not improve the protected winter plan materially'
        else:
            selected = normal
            selected_name = 'self_sufficiency'

        first_detail = details[0] if details else {
            'house_kwh': 0.0,
            'uncertainty_kwh': 0.0,
            'protected_dc_kwh': 0.0,
            'feasible_soc': self.min_soc,
        }
        next_time = slots[windows[0][0]]['start'].isoformat() if windows else None
        policy = {
            'mode': 'winter',
            'selected_candidate': selected_name,
            'next_replenishment_time': next_time,
            'forecast_house_energy_required_kwh': round(first_detail['house_kwh'], 3),
            'uncertainty_allowance_kwh': round(first_detail['uncertainty_kwh'], 3),
            'protected_soc_percent': round(first_detail['feasible_soc'], 2),
            'protected_energy_kwh': round(first_detail['protected_dc_kwh'], 3),
            'exceptional_spread_eur_per_kwh': round(economics['spread'], 4),
            'expected_incremental_benefit_eur': round(max(0.0, benefit), 3),
            'reason_code': reason_code,
            'reason': reason,
            'warning': warning,
            'active_charge_windows': len(windows),
            'uncertainty_source': self.uncertainty_model['source'],
            'uncertainty_samples': self.uncertainty_model['samples'],
            'uncertainty_rate': round(self.uncertainty_model['rate'], 4),
            'uncertainty_quantile': self.uncertainty_model['quantile'],
            'uncertainty_rate_cap': self.uncertainty_model['rate_cap'],
            'uncertainty_min_kwh': WINTER_UNCERTAINTY_MIN_KWH,
            'uncertainty_max_kwh': WINTER_UNCERTAINTY_MAX_KWH,
            'soc_step_percent': round(self.soc_step, 4),
        }
        return self._finish(selected, slot_h, policy)


def optimize_schedule(current_soc, price_data, load_forecast=None, pv_forecast=None,
                      discharge_blocked_slots=None):
    """Public Winter Mode entry point matching the summer optimizer contract."""
    engine = OptimizationEngine()
    try:
        from lib import ess_cost_basis
        engine.set_cost_basis_floor(ess_cost_basis.current_basis())
    except Exception as exc:  # pragma: no cover - defensive best effort
        logging.warning("WINTER_ESS: cost-basis unavailable (%s).", exc)
    return engine.optimize(
        current_soc, price_data, load_forecast, pv_forecast,
        discharge_blocked_slots=discharge_blocked_slots)


def format_plan_summary(result, **_kwargs):
    """Render a compact summary without depending on the summer formatter."""
    if not result:
        return "WINTER_ESS plan: <no feasible plan>"
    policy = result.get('winter_policy', {})
    schedule = result.get('schedule', [])
    buys = sum(step.get('control_action') == 'BUY' for step in schedule)
    sells = sum(step.get('control_action') == 'SELL' for step in schedule)
    return (
        "WINTER_ESS plan: "
        f"{policy.get('selected_candidate', 'self_sufficiency')}; "
        f"{buys} buy slots, {sells} sell slots; "
        f"protected {policy.get('protected_soc_percent', 0):.0f}% SoC"
    )
