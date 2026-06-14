"""
AI-powered ESS optimization engine.

This module computes a cost-optimal battery charge/discharge plan over the
available Tibber price horizon (today, and tomorrow once published ~13:00) using
a dynamic-programming search over a discretized battery state-of-charge (SoC).

Design notes
------------
* The DP enumerates transitions between discretized SoC levels for every price
  slot. For each transition it derives the AC grid energy required (taking
  charge/discharge efficiency, PV and load forecasts into account), enforces
  power and SoC-reserve limits, and accumulates the monetary cost using the
  per-slot *buy* price for imports and *sell* price for exports.
* Stored energy at the end of the horizon is given a terminal value so the
  optimizer does not simply dump the battery to the grid at the end of the
  window. This is what lets a 48h (today + tomorrow) plan defer cheap charging
  or expensive discharging into the next day when that is more profitable over
  the monthly settlement period.
* Negative prices are handled naturally by the cost function (importing at a
  negative price is revenue; exporting at a negative price is a loss, so the
  optimizer avoids it). The caller additionally hard-limits grid feed-in to 0W
  while the current price is negative (see energy_broker.run_ai_optimizer).

All energy is in kWh, power in kW, prices in currency/kWh, durations in hours
unless otherwise noted.
"""
import logging
from datetime import datetime, timedelta

from dateutil import parser as date_parser

from lib.config_retrieval import retrieve_setting

# Defaults for tunables that can be overridden via .env (see OptimizationEngine).
# Seasonal SoC reserve (percentage) kept in the battery at all times.
MIN_SOC_RESERVE_WINTER = 20.0
MIN_SOC_RESERVE_SUMMER = 5.0

# DP SoC discretization step (percentage points). Smaller = finer control but
# more compute (states scale as 100/step). Must divide 100 sensibly.
SOC_STEP = 5.0

# Numerical tolerance used for float comparisons.
EPS = 1e-6

# Internal mode code -> user-facing label.
MODE_LABELS = {
    'buy': 'BUY',
    'sell': 'SELL',
    'hold': 'HOLD',
    'self_supply': 'SELF-SUPPLY',
}

# Internal mode code -> what the battery is physically doing.
MODE_BATTERY = {
    'buy': 'charging',
    'sell': 'discharging to grid',
    'hold': 'held (idle)',
    'self_supply': 'powering house loads',
}


def _safe_float(setting_name: str, default: float) -> float:
    """Return a setting parsed as float, falling back to a safe default."""
    raw = retrieve_setting(setting_name)
    if raw in (None, "", "None"):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.warning(
            "AI_ESS: Unable to parse %s value '%s'; using default %s.",
            setting_name,
            raw,
            default,
        )
        return default


def _coerce_datetime(value) -> datetime:
    """Coerce a price-point 'start' (datetime or ISO-8601 string) to datetime.

    Tibber returns ISO-8601 strings (e.g. ``2026-06-13T13:00:00+02:00``) while
    unit tests pass real ``datetime`` objects. Both must be supported.
    """
    if isinstance(value, datetime):
        return value
    return date_parser.parse(value)


class OptimizationEngine:
    def __init__(self):
        self.battery_capacity = _safe_float('BATTERY_CAPACITY_KWH', 45.0)
        self.charge_efficiency = _safe_float('AC_DC_CHARGE_EFFICIENCY', 0.90)
        self.discharge_efficiency = _safe_float('AC_DC_DISCHARGE_EFFICIENCY', 0.90)

        # Grid and battery power limits (kW).
        self.max_power_import = _safe_float('ESS_MAX_GRID_IMPORT_KW', 10.0)
        self.max_power_export = _safe_float('ESS_MAX_GRID_EXPORT_KW', 10.0)
        self.max_charge_power = _safe_float('ESS_MAX_CHARGE_KW', self.max_power_import)
        self.max_discharge_power = _safe_float('ESS_MAX_DISCHARGE_KW', self.max_power_export)

        # Export economics. Tibber typically pays the spot component for exports
        # while the buy price additionally carries grid fees and taxes. These
        # default to 1.0 / 0.0 to preserve the previous symmetric behaviour and
        # can be tuned via config.
        self.export_price_factor = _safe_float('ESS_EXPORT_PRICE_FACTOR', 1.0)
        self.export_fee = _safe_float('ESS_EXPORT_FEE', 0.0)

        # Terminal valuation multiplier for energy left in the battery at the
        # end of the horizon (1.0 = value it at the horizon mean buy price).
        self.terminal_value_factor = _safe_float('ESS_TERMINAL_VALUE_FACTOR', 1.0)

        # Expected peak buy price (currency/kWh). When set (> 0) end-of-horizon
        # stored energy is valued at the higher of the horizon mean and this
        # expected peak, so the optimizer holds charge for the typical
        # morning/evening peaks (which maximises revenue over the monthly
        # settlement period) instead of selling into a low intra-day "high".
        self.expected_peak_price = _safe_float('ESS_EXPECTED_PEAK_PRICE', 0.0)

        # Hard floor below which the battery is never actively discharged to the
        # grid (PV surplus feed-in is still allowed). 0.0 disables the floor.
        self.min_sell_price = _safe_float('ESS_MIN_SELL_PRICE', 0.0)

        # Planning resolution in minutes. When the native price data is coarser
        # than this (e.g. hourly Tibber prices with a 15-minute target) each
        # native price slot is sub-divided so the engine is ready for true
        # quarter-hourly prices once Tibber publishes them. Auto-uses the native
        # resolution when it is already finer.
        self.slot_minutes = _safe_float('OPTIMIZER_SLOT_MINUTES', 15.0)

        # Default daily home consumption (kWh) used to synthesise a flat load
        # forecast when no per-slot forecast is supplied.
        self.daily_load_kwh = _safe_float('DAILY_HOME_ENERGY_CONSUMPTION', 16.0)

        # Seasonal SoC reserve (percentage) kept in the battery at all times.
        winter_reserve = _safe_float('MIN_SOC_RESERVE_WINTER', MIN_SOC_RESERVE_WINTER)
        summer_reserve = _safe_float('MIN_SOC_RESERVE_SUMMER', MIN_SOC_RESERVE_SUMMER)

        current_month = datetime.now().month
        self.is_winter = current_month in (11, 12, 1, 2, 3)
        self.min_soc = winter_reserve if self.is_winter else summer_reserve
        self.max_soc = 100.0

        # DP SoC discretization step (percentage points).
        self.soc_step = _safe_float('OPTIMIZER_SOC_STEP_PCT', SOC_STEP)
        if self.soc_step <= 0:
            self.soc_step = SOC_STEP
        self.soc_states = [i * self.soc_step for i in range(int(100 / self.soc_step) + 1)]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _detect_slot_duration_h(self, future_prices) -> float:
        """Infer slot duration (hours) from the spacing of price timestamps.

        Tibber publishes hourly prices, but the engine must also work with
        15-minute data. We take the smallest positive gap between consecutive
        starts as the canonical slot length, defaulting to 1.0h.
        """
        deltas = []
        for i in range(1, len(future_prices)):
            gap = (future_prices[i]['start'] - future_prices[i - 1]['start']).total_seconds()
            if gap > 0:
                deltas.append(gap)
        if not deltas:
            return 1.0
        return min(deltas) / 3600.0

    @staticmethod
    def _lookup_forecast(forecast, index, slot_start, default):
        """Look up a per-slot forecast value.

        ``forecast`` may be None (use default), a list indexed by slot position,
        or a dict keyed by the slot's start ``datetime`` (robust to the engine's
        internal future-only filtering, so callers don't need to know which
        slots survive the filter).
        """
        if forecast is None:
            return default
        if isinstance(forecast, dict):
            return forecast.get(slot_start, default)
        if index < len(forecast):
            return forecast[index]
        return default

    def _sell_price(self, buy_price: float) -> float:
        return buy_price * self.export_price_factor - self.export_fee

    def _snap_soc(self, soc: float) -> float:
        snapped = round(soc / self.soc_step) * self.soc_step
        return max(0.0, min(snapped, 100.0))

    # ------------------------------------------------------------------ #
    # Optimization
    # ------------------------------------------------------------------ #
    def optimize(self, current_soc_percent, price_data, load_forecast=None, pv_forecast=None):
        """Compute the optimal plan.

        :param current_soc_percent: current battery SoC (0-100)
        :param price_data: list of {'start': datetime|str, 'total': float, ...}
        :param load_forecast: optional list of per-slot load (kWh)
        :param pv_forecast: optional list of per-slot PV generation (kWh)
        :return: dict with schedule, victron_slots, setpoint, limit_feed_in,
                 current_price - or None when no feasible plan exists.
        """
        if not price_data:
            logging.warning("AI_ESS: No price data available for optimization.")
            return None

        # Normalise timestamps and sort chronologically.
        normalised = []
        for p in price_data:
            try:
                normalised.append({
                    'start': _coerce_datetime(p['start']),
                    'total': float(p['total']),
                    'level': p.get('level'),
                })
            except (KeyError, TypeError, ValueError) as e:
                logging.warning("AI_ESS: Skipping malformed price point %s (%s).", p, e)
        if not normalised:
            logging.warning("AI_ESS: No usable price points after normalisation.")
            return None

        normalised.sort(key=lambda x: x['start'])

        tzinfo = normalised[0]['start'].tzinfo
        now = datetime.now(tzinfo)

        native_slot_h = self._detect_slot_duration_h(normalised) if len(normalised) > 1 else 1.0

        # Planning resolution: sub-divide each native price slot when a finer
        # target resolution is configured (e.g. 15-min planning over hourly
        # prices). When native data is already finer, k == 1.
        target_h = max(self.slot_minutes, 1.0) / 60.0
        k = max(1, int(round(native_slot_h / target_h))) if target_h > 0 else 1
        slot_duration_h = native_slot_h / k
        slot_seconds = int(round(slot_duration_h * 3600))

        # Expand native price slots into (optionally finer) planning slots,
        # distributing per-slot forecasts evenly across the sub-slots.
        avg_native_load = self.daily_load_kwh * (native_slot_h / 24.0)
        expanded = []
        for idx, p in enumerate(normalised):
            native_load = self._lookup_forecast(load_forecast, idx, p['start'], avg_native_load)
            native_pv = self._lookup_forecast(pv_forecast, idx, p['start'], 0.0)
            for j in range(k):
                sub_start = p['start'] + timedelta(hours=slot_duration_h * j)
                expanded.append({
                    'start': sub_start,
                    'buy': p['total'],
                    'load': native_load / k,
                    'pv': native_pv / k,
                })

        # Keep the slot whose window still contains "now" plus all future slots.
        keep_after = now - timedelta(hours=slot_duration_h)
        future_prices = [p for p in expanded if p['start'] > keep_after]

        if not future_prices:
            logging.warning("AI_ESS: No future price data.")
            return None

        steps = len(future_prices)

        # Precompute buy/sell prices and net AC load per slot.
        buy_prices = [p['buy'] for p in future_prices]
        sell_prices = [self._sell_price(b) for b in buy_prices]
        net_loads = [p['load'] - p['pv'] for p in future_prices]

        # DP tables. dp[t][soc] = minimum cost to reach soc at slot boundary t.
        dp = [{s: float('inf') for s in self.soc_states} for _ in range(steps + 1)]
        parent = [{s: None for s in self.soc_states} for _ in range(steps + 1)]

        start_soc = self._snap_soc(current_soc_percent)
        dp[0][start_soc] = 0.0

        cap = self.battery_capacity

        for t in range(steps):
            buy = buy_prices[t]
            sell = sell_prices[t]
            net_load = net_loads[t]

            for soc in self.soc_states:
                base_cost = dp[t][soc]
                if base_cost == float('inf'):
                    continue

                for nsoc in self.soc_states:
                    # Never discharge below the seasonal reserve.
                    if nsoc < self.min_soc - EPS:
                        continue

                    dc_change_kwh = (nsoc - soc) / 100.0 * cap

                    # Battery power limit.
                    batt_kw = abs(dc_change_kwh) / slot_duration_h
                    if dc_change_kwh >= 0:
                        if batt_kw > self.max_charge_power + EPS:
                            continue
                        ac_for_batt = dc_change_kwh / self.charge_efficiency  # AC consumed
                    else:
                        if batt_kw > self.max_discharge_power + EPS:
                            continue
                        ac_for_batt = dc_change_kwh * self.discharge_efficiency  # AC produced (neg)

                    # AC energy balance: grid must cover load minus PV plus the
                    # net battery AC demand. >0 = import, <0 = export.
                    grid_energy = net_load + ac_for_batt

                    if grid_energy > self.max_power_import * slot_duration_h + EPS:
                        continue
                    if -grid_energy > self.max_power_export * slot_duration_h + EPS:
                        continue

                    import_kwh = grid_energy if grid_energy > 0 else 0.0
                    export_kwh = -grid_energy if grid_energy < 0 else 0.0

                    # Never actively sell battery energy below the sell-price
                    # floor. PV-surplus feed-in (battery not discharging) is
                    # still allowed so we don't curtail free solar export.
                    if export_kwh > EPS and dc_change_kwh < -EPS and sell < self.min_sell_price - EPS:
                        continue

                    step_cost = import_kwh * buy - export_kwh * sell

                    total = base_cost + step_cost
                    if total < dp[t + 1][nsoc] - EPS:
                        dp[t + 1][nsoc] = total
                        parent[t + 1][nsoc] = (soc, grid_energy)

        # Terminal valuation: value usable stored energy so the plan keeps
        # charge for future peaks rather than dumping it at the end of the
        # horizon. Use the higher of the horizon mean (scaled) and the expected
        # peak price, so charge is preserved for the typical morning/evening
        # peaks even before the next day's prices are published.
        mean_buy = sum(buy_prices) / len(buy_prices)
        terminal_price = mean_buy * self.terminal_value_factor
        if self.expected_peak_price > 0:
            terminal_price = max(terminal_price, self.expected_peak_price)

        best_end_soc = None
        best_objective = float('inf')
        for s in self.soc_states:
            if dp[steps][s] == float('inf'):
                continue
            usable_kwh = max(0.0, (s - self.min_soc) / 100.0 * cap) * self.discharge_efficiency
            objective = dp[steps][s] - usable_kwh * terminal_price
            if objective < best_objective:
                best_objective = objective
                best_end_soc = s

        if best_end_soc is None:
            logging.error("AI_ESS: No feasible schedule found.")
            return None

        # Backtrack to build the per-slot schedule.
        schedule = []
        curr_soc = best_end_soc
        for t in range(steps, 0, -1):
            prev = parent[t][curr_soc]
            if not prev:
                break
            prev_soc, grid_energy = prev
            buy = future_prices[t - 1]['buy']
            action = self._classify_action(prev_soc, curr_soc, grid_energy)
            schedule.insert(0, {
                'time': future_prices[t - 1]['start'],
                'action': action,
                'soc_start': prev_soc,
                'soc_end': curr_soc,
                'grid_energy': round(grid_energy, 4),
                'price': buy,
                'sell': round(self._sell_price(buy), 4),
            })
            curr_soc = prev_soc

        if not schedule:
            logging.warning("AI_ESS: Backtrack produced an empty schedule.")
            return None

        return self._post_process(schedule, slot_seconds)

    @staticmethod
    def _classify_action(soc_start, soc_end, grid_energy) -> str:
        """Classify a slot into one of four user-facing modes:

        * ``buy``         — storing energy into the battery (SoC rising; from grid
                            and/or PV). May import from the grid.
        * ``sell``        — exporting energy to the grid (battery discharge and/or
                            PV surplus while the battery is full).
        * ``self_supply`` — battery powering the house loads (SoC falling, no grid
                            export).
        * ``hold``        — battery preserved (SoC ~flat); loads covered by the grid
                            and/or PV ("retain").
        """
        charging = soc_end > soc_start + EPS
        discharging = soc_end < soc_start - EPS
        exporting = grid_energy < -EPS

        if charging:
            return 'buy'
        if exporting:
            return 'sell'
        if discharging:
            return 'self_supply'
        return 'hold'

    def _explain_action(self, schedule):
        """Return ``(reason_code, reason_text)`` explaining the current-slot mode."""
        cur = schedule[0]
        mode = cur['action']
        price = cur['price']
        soc = cur['soc_start']

        def _hm(s):
            try:
                return s['time'].strftime('%H:%M')
            except Exception:
                return str(s['time'])

        next_sell = next((s for s in schedule[1:] if s['action'] == 'sell'), None)
        horizon_max = max((s['price'] for s in schedule), default=price)

        if mode == 'buy':
            if next_sell:
                return ('PRECHARGE_FOR_PEAK',
                        f"Charging at €{price:.3f}/kWh to sell later at "
                        f"{_hm(next_sell)} (€{next_sell['price']:.3f}/kWh)")
            return ('PRICE_LOW', f"Charging while the price is low (€{price:.3f}/kWh)")

        if mode == 'sell':
            if soc >= 100.0 - self.soc_step:
                return ('PV_SURPLUS_BATTERY_FULL',
                        f"Battery full — exporting surplus solar at €{price:.3f}/kWh")
            if price >= horizon_max - EPS:
                return ('PRICE_PEAK',
                        f"Selling at €{price:.3f}/kWh — the highest price in the horizon")
            return ('PRICE_HIGH',
                    f"Selling stored energy at €{price:.3f}/kWh (a profitable high price)")

        if mode == 'hold':
            if soc <= self.min_soc + self.soc_step + EPS:
                return ('RESERVE_POLICY',
                        f"At minimum reserve ({self.min_soc:.0f}%); holding — loads covered by grid/PV")
            if next_sell:
                return ('BUY_CHEAPER_THAN_STORED_VALUE',
                        f"Holding the battery; grid (€{price:.3f}/kWh) is cheaper than the stored "
                        f"energy's value at {_hm(next_sell)} (€{next_sell['price']:.3f}/kWh) — "
                        f"covering loads from grid/PV")
            return ('HOLD_PRESERVE',
                    f"Holding the battery; covering loads from grid/PV (€{price:.3f}/kWh)")

        # self_supply
        if self.min_sell_price > 0 and price < self.min_sell_price:
            return ('BELOW_SELL_FLOOR',
                    f"Price €{price:.3f}/kWh is below the sell floor (€{self.min_sell_price:.3f}); "
                    f"using stored energy rather than exporting")
        if next_sell and next_sell['price'] > price:
            return ('AVOID_PRICE_AWAIT_DIP',
                    f"Running off the battery at €{price:.3f}/kWh; cheaper than the grid now, "
                    f"recharging before the {_hm(next_sell)} peak (€{next_sell['price']:.3f}/kWh)")
        return ('STORED_CHEAPER_THAN_GRID',
                f"Using stored energy — cheaper than buying from the grid at €{price:.3f}/kWh")

    def _post_process(self, schedule, slot_seconds):
        # Group consecutive grid-charge (buy) slots into Victron charge windows.
        victron_slots = []
        current_slot = None
        for i, step in enumerate(schedule):
            if step['action'] == 'buy':
                if current_slot is not None and i == current_slot['_end_index'] + 1:
                    current_slot['duration'] += slot_seconds
                    current_slot['_end_index'] = i
                    current_slot['target_soc'] = min(100, int(round(step['soc_end'])))
                    current_slot['_prices'].append(step['price'])
                else:
                    current_slot = {
                        'start': step['time'],
                        'duration': slot_seconds,
                        'target_soc': min(100, int(round(step['soc_end']))),
                        '_end_index': i,
                        '_prices': [step['price']],
                    }
                    victron_slots.append(current_slot)

        # Victron exposes only five charge schedule slots; keep the cheapest.
        for s in victron_slots:
            s['avg_price'] = sum(s['_prices']) / len(s['_prices'])
        if len(victron_slots) > 5:
            victron_slots.sort(key=lambda x: x['avg_price'])
            victron_slots = victron_slots[:5]
        victron_slots.sort(key=lambda x: x['start'])

        formatted_slots = [{
            'start': s['start'],
            'duration': s['duration'],
            'target_soc': s['target_soc'],
        } for s in victron_slots]

        # Immediate control for the current slot.
        first = schedule[0]
        mode = first['action']
        export_setpoint = _safe_float('ESS_EXPORT_AC_SETPOINT', -10000.0)
        slot_h = slot_seconds / 3600.0
        if mode == 'sell':
            # Apply the export power the plan actually calls for this slot (grid
            # energy / slot length), not a blanket max-export, so the real SoC
            # trajectory tracks the forecast. Clamp to the configured export limit
            # (both values are negative; never export more than the limit).
            planned_w = (first['grid_energy'] / slot_h * 1000.0) if slot_h else export_setpoint
            setpoint = float(round(max(planned_w, export_setpoint)))
        else:
            # buy -> Victron charge schedule; self_supply -> 0W; hold is applied
            # by the caller via the PV-aware grid-assist control loop.
            setpoint = 0.0

        reason_code, reason_text = self._explain_action(schedule)

        return {
            'schedule': schedule,
            'victron_slots': formatted_slots,
            'setpoint': setpoint,
            'mode': mode,
            'reason': reason_text,
            'reason_code': reason_code,
            'grid_assist': mode == 'hold',
            'current_price': first['price'],
            'limit_feed_in': first['price'] < 0,
            'slot_duration_h': slot_seconds / 3600.0,
        }


def optimize_schedule(current_soc, price_data, load_forecast=None, pv_forecast=None):
    engine = OptimizationEngine()
    return engine.optimize(current_soc, price_data, load_forecast, pv_forecast)


def format_plan_summary(result, *, batt_soc=None, source="", price_points=None,
                        pv_remaining=None, max_hours=None, today_actuals=None,
                        applied_setpoint=None) -> str:
    """Render an optimizer result as a human-readable, multi-line plan summary.

    Shared by the dry-run script and the live service log so the same plan view
    appears in both places (and the service log shows how plans change over time).

    ``max_hours`` (optional) limits the per-slot table to the next N hours to keep
    the service log compact; the cost summary is still computed over the full
    horizon.
    """
    if not result:
        return "AI_ESS plan: <no feasible plan>"

    schedule = result.get('schedule', [])
    line = "=" * 78

    # --- Build the per-slot table (and accumulate cost totals) --------------
    cutoff = None
    if max_hours is not None and schedule:
        try:
            cutoff = schedule[0]['time'] + timedelta(hours=max_hours)
        except Exception:
            cutoff = None

    shown = 0
    rows = []
    total_import_cost = total_export_rev = total_import_kwh = total_export_kwh = 0.0
    day_totals = {}  # date -> {imp_kwh, imp_cost, exp_kwh, exp_rev, slots}
    for step in schedule:
        g = step['grid_energy']
        buy = step['price']
        sell = step.get('sell', buy)

        try:
            day_key = step['time'].date()
        except Exception:
            day_key = None
        dt = day_totals.setdefault(day_key, {'imp_kwh': 0.0, 'imp_cost': 0.0,
                                             'exp_kwh': 0.0, 'exp_rev': 0.0, 'slots': 0})
        dt['slots'] += 1

        if g > 0:
            total_import_kwh += g
            total_import_cost += g * buy
            dt['imp_kwh'] += g
            dt['imp_cost'] += g * buy
        elif g < 0:
            total_export_kwh += -g
            total_export_rev += -g * sell
            dt['exp_kwh'] += -g
            dt['exp_rev'] += -g * sell

        if cutoff is not None and step['time'] > cutoff:
            continue
        shown += 1
        try:
            when = step['time'].strftime('%a %H:%M')
        except Exception:
            when = str(step['time'])
        mode_label = MODE_LABELS.get(step['action'], step['action'])
        rows.append(f"  {when:<12} {mode_label:<11} {buy:>8.4f} {sell:>8.4f} "
                    f"{step['soc_start']:>5.0f}->{step['soc_end']:<5.0f} {g:>9.2f}")

    net_cost = total_import_cost - total_export_rev

    # --- Assemble output: per-slot breakdown FIRST, key summary LAST so the
    #     headline sections sit at the bottom of the log (visible first when
    #     scrolling up) -----------------------------------------------------
    out = [line]
    plan_header = "PER-SLOT PLAN"
    if cutoff is not None and shown < len(schedule):
        plan_header += f"  (next {max_hours}h: {shown} of {len(schedule)} slots)"
    out.append(plan_header)
    out.append(f"  {'time':<12} {'mode':<11} {'buy':>8} {'sell':>8} {'soc':>13} {'grid kWh':>9}")
    out.extend(rows)

    out.append(line)
    out.append("AI ESS OPTIMIZER PLAN")
    if batt_soc is not None:
        out.append(f"Starting SoC          : {batt_soc:.1f}%" + (f"  ({source})" if source else ""))
    if price_points is not None:
        out.append(f"Price points loaded   : {price_points} "
                   f"(plan slot ~ {result.get('slot_duration_h', 0):.2f}h)")
    if pv_remaining is not None:
        out.append(f"PV remaining (STATE)  : {pv_remaining} Wh")

    out.append(line)
    vslots = result.get('victron_slots', [])
    out.append(f"VICTRON GRID-CHARGE SLOTS ({len(vslots)} / 5 max)")
    if vslots:
        for i, s in enumerate(vslots):
            try:
                when = s['start'].strftime('%a %H:%M')
            except Exception:
                when = str(s['start'])
            out.append(f"  [{i}] {when}  duration {s['duration'] // 60:>4} min  "
                       f"-> target {s['target_soc']}% SoC")
    else:
        out.append("  (none - no grid charging planned this horizon)")

    out.append(line)
    out.append("IMMEDIATE DECISION (current slot, live)")
    mode = result.get('mode', '')
    out.append(f"  Mode        : {MODE_LABELS.get(mode, mode)}")
    if result.get('reason'):
        out.append(f"  Reason      : {result.get('reason')}")
    out.append(f"  Price       : {result.get('current_price', 0):.4f} /kWh")
    out.append(f"  Battery     : {MODE_BATTERY.get(mode, '-')}")

    # Live grid flow: use the actually-applied setpoint when provided, else the
    # planned setpoint. Positive = importing, negative = exporting, 0 = idle.
    sp = applied_setpoint if applied_setpoint is not None else result.get('setpoint', 0.0)
    try:
        sp = float(sp)
    except (TypeError, ValueError):
        sp = 0.0
    if sp > 0:
        grid_note = f"import {sp:.0f} W"
    elif sp < 0:
        grid_note = f"export {abs(sp):.0f} W"
    elif mode == 'hold':
        grid_note = "idle (PV covering load)"
    else:
        grid_note = "idle"
    out.append(f"  Grid now    : {grid_note}")
    out.append(f"  Setpoint    : {sp:.0f} W")
    out.append(f"  Feed-in cap : {'ON (0 W — negative price)' if result.get('limit_feed_in') else 'off'}")

    out.append(line)
    today = datetime.now().date()

    has_actuals = today_actuals is not None
    act = today_actuals or {}
    a_imp_kwh = float(act.get('imp_kwh', 0.0) or 0.0)
    a_imp_cost = float(act.get('imp_cost', 0.0) or 0.0)
    a_exp_kwh = float(act.get('exp_kwh', 0.0) or 0.0)
    a_exp_rev = float(act.get('exp_rev', 0.0) or 0.0)

    def _row(label, imp_kwh, imp_cost, exp_kwh, exp_rev):
        net = imp_cost - exp_rev
        net_str = f"€{abs(net):.2f} {'profit' if net < 0 else 'cost'}"
        return (f"  {label:<24}"
                f"import {imp_kwh:6.2f} kWh €{imp_cost:6.2f}   "
                f"export {exp_kwh:6.2f} kWh €{exp_rev:6.2f}   "
                f"net {net_str}")

    out.append("DAY COST SUMMARY  (" + ("actuals so far + forecast)" if has_actuals else "forecast)"))

    for day_key in sorted(k for k in day_totals if k is not None):
        d = day_totals[day_key]
        f_imp_kwh, f_imp_cost = d['imp_kwh'], d['imp_cost']
        f_exp_kwh, f_exp_rev = d['exp_kwh'], d['exp_rev']

        if day_key == today and has_actuals:
            label = day_key.strftime('%a %d %b') + " (today)"
            out.append(_row(label,
                            f_imp_kwh + a_imp_kwh, f_imp_cost + a_imp_cost,
                            f_exp_kwh + a_exp_kwh, f_exp_rev + a_exp_rev))
            out.append(_row("    └ actual so far", a_imp_kwh, a_imp_cost, a_exp_kwh, a_exp_rev))
            out.append(_row("    └ forecast rest", f_imp_kwh, f_imp_cost, f_exp_kwh, f_exp_rev))
        else:
            label = day_key.strftime('%a %d %b')
            if day_key == today:
                label += " (today, remaining)"
            out.append(_row(label, f_imp_kwh, f_imp_cost, f_exp_kwh, f_exp_rev))

    g_imp_kwh = total_import_kwh + (a_imp_kwh if has_actuals else 0.0)
    g_imp_cost = total_import_cost + (a_imp_cost if has_actuals else 0.0)
    g_exp_kwh = total_export_kwh + (a_exp_kwh if has_actuals else 0.0)
    g_exp_rev = total_export_rev + (a_exp_rev if has_actuals else 0.0)

    out.append("  " + "─" * 74)
    out.append(_row("TOTAL", g_imp_kwh, g_imp_cost, g_exp_kwh, g_exp_rev))
    out.append(f"  ({len(schedule)} forecast slots, ~{len(schedule) * result.get('slot_duration_h', 0):.1f}h"
               + (" + today's actuals)" if has_actuals else ")"))
    out.append(line)
    return "\n".join(out)
