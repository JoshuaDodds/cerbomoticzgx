import json
import logging
from datetime import datetime, timedelta
from math import floor, ceil

from lib.config_retrieval import retrieve_setting

# Constants
MIN_SOC_RESERVE_WINTER = 20.0  # Percentage
MIN_SOC_RESERVE_SUMMER = 5.0   # Percentage

class OptimizationEngine:
    def __init__(self):
        self.battery_capacity = float(retrieve_setting('BATTERY_CAPACITY_KWH') or 45.0)
        self.charge_efficiency = float(retrieve_setting('AC_DC_CHARGE_EFFICIENCY') or 0.90)
        self.discharge_efficiency = float(retrieve_setting('AC_DC_DISCHARGE_EFFICIENCY') or 0.90)
        self.max_power_import = 10.0 # kW, assumption based on task description
        self.max_power_export = 10.0 # kW, assumption

        # Determine season (simple month check)
        current_month = datetime.now().month
        self.is_winter = current_month in [11, 12, 1, 2, 3] # Nov-Mar

        self.min_soc = MIN_SOC_RESERVE_WINTER if self.is_winter else MIN_SOC_RESERVE_SUMMER
        self.max_soc = 100.0

    def optimize(self, current_soc_percent, price_data, load_forecast=None, pv_forecast=None):
        """
        :param current_soc_percent: Current battery SoC (0-100)
        :param price_data: List of dicts {'start': datetime, 'total': float} (price per kWh)
        :param load_forecast: List of forecast consumption (kWh) per slot (optional)
        :param pv_forecast: List of forecast PV generation (kWh) per slot (optional)
        :return: dict with 'schedule': list of actions, 'victron_slots': list of slots
        """

        if not price_data:
            logging.warning("AI_ESS: No price data available for optimization.")
            return None

        # Sort price data by time
        price_data.sort(key=lambda x: x['start'])

        # Filter for future only (approx)
        now = datetime.now(price_data[0]['start'].tzinfo)
        future_prices = [p for p in price_data if p['start'] > now - timedelta(minutes=15)]

        if not future_prices:
             logging.warning("AI_ESS: No future price data.")
             return None

        # Discretize time into steps
        steps = len(future_prices)

        # DP State: SoC (discretized to 5% steps)
        soc_step = 5.0
        soc_states = [i * soc_step for i in range(int(100/soc_step) + 1)]

        # Initialize DP table: Cost to reach state s at time t
        dp = [{s: float('inf') for s in soc_states} for _ in range(steps + 1)]
        parent = [{s: None for s in soc_states} for _ in range(steps + 1)]

        # Initial state
        start_soc = round(current_soc_percent / soc_step) * soc_step
        start_soc = max(min(start_soc, 100.0), 0.0)
        dp[0][start_soc] = 0.0

        slot_duration_h = 0.25 # 15 mins

        # Default load if not provided (assume constant)
        daily_load = float(retrieve_setting('DAILY_HOME_ENERGY_CONSUMPTION') or 16.0)
        avg_load_kwh_per_slot = daily_load / (24 * 4)

        for t in range(steps):
            price = future_prices[t]['total']

            # Forecasts for this slot
            load_kwh = load_forecast[t] if load_forecast and t < len(load_forecast) else avg_load_kwh_per_slot
            pv_kwh = pv_forecast[t] if pv_forecast and t < len(pv_forecast) else 0.0

            # Net Load (positive = deficit, negative = surplus)
            net_load_kwh = load_kwh - pv_kwh

            # For each current SoC state
            for soc in soc_states:
                if dp[t][soc] == float('inf'):
                    continue

                current_cost = dp[t][soc]

                # Actions:
                # 1. Charge (Grid Import + PV surplus handling)
                # 2. Discharge (Grid Export + Load deficit handling)
                # 3. Idle (Grid Power = 0, Battery handles Net Load)

                actions = ['charge', 'discharge', 'idle']

                for action in actions:
                    grid_power = 0.0 # kW
                    if action == 'charge':
                        grid_power = self.max_power_import
                    elif action == 'discharge':
                        grid_power = -self.max_power_export
                    elif action == 'idle':
                        grid_power = 0.0

                    grid_energy_kwh = grid_power * slot_duration_h

                    # Energy Balance:
                    # Grid_Energy = Load - PV + Battery_Change
                    # Battery_Change = Grid_Energy - Net_Load

                    batt_energy_change_kwh = grid_energy_kwh - net_load_kwh

                    # Apply efficiencies
                    if batt_energy_change_kwh > 0: # Charging
                        delta_soc = (batt_energy_change_kwh * self.charge_efficiency / self.battery_capacity) * 100
                    else: # Discharging
                        delta_soc = (batt_energy_change_kwh / self.discharge_efficiency / self.battery_capacity) * 100

                    next_soc = soc + delta_soc

                    # Constraints
                    if next_soc < self.min_soc or next_soc > 100.0 + 1e-3: # Allow slight tolerance
                        # Invalidate this transition
                        # However, for 'idle', if battery is full/empty, maybe we MUST import/export?
                        # Or if we want to force 'idle' (0 grid power) but battery can't support it,
                        # then 'idle' is impossible. We skip.
                        continue

                    # Cost Calculation: Cost = Grid_Import * Price - Grid_Export * Price
                    # Cost = Grid_Energy * Price
                    step_cost = grid_energy_kwh * price

                    # Snap to grid
                    next_soc_d = round(next_soc / soc_step) * soc_step
                    next_soc_d = max(min(next_soc_d, 100.0), 0.0) # Clamp

                    if dp[t+1][next_soc_d] > current_cost + step_cost:
                        dp[t+1][next_soc_d] = current_cost + step_cost
                        parent[t+1][next_soc_d] = (soc, action)

        # Backtrack to find optimal path
        # Find best end state (lowest cost)
        best_end_soc = min(dp[steps], key=dp[steps].get)
        if dp[steps][best_end_soc] == float('inf'):
            logging.error("AI_ESS: No feasible schedule found.")
            return None

        schedule = []
        curr_soc = best_end_soc
        for t in range(steps, 0, -1):
            prev = parent[t][curr_soc]
            if not prev:
                break
            prev_soc, action = prev
            schedule.insert(0, {
                'time': future_prices[t-1]['start'],
                'action': action,
                'soc': curr_soc,
                'price': future_prices[t-1]['total']
            })
            curr_soc = prev_soc

        return self._post_process(schedule)

    def _post_process(self, schedule):
        # Extract Victron slots (Charge actions)
        victron_slots = []
        current_slot = None

        for i, step in enumerate(schedule):
            if step['action'] == 'charge':
                if current_slot and i == current_slot['end_index'] + 1:
                    # Extend slot
                    current_slot['duration'] += 900 # 15 mins
                    current_slot['end_index'] = i
                else:
                    # New slot
                    current_slot = {
                        'start': step['time'],
                        'duration': 900,
                        'end_index': i,
                        'avg_price': step['price'] # simple approx
                    }
                    victron_slots.append(current_slot)

        # Select top 5 slots if more
        # Here we should probably select the longest/most important ones, or just the ones with lowest price.
        # But since the optimizer already decided *when* to charge based on cost, any charge action is "good".
        # We just need to fit them into 5 slots.
        # If we have fragmented charging, we might lose some optimality.

        if len(victron_slots) > 5:
             # Sort by price (cheapest first)
             victron_slots.sort(key=lambda x: x['avg_price'])
             victron_slots = victron_slots[:5]

        # Format for EnergyBroker
        formatted_slots = []
        for s in victron_slots:
            formatted_slots.append({
                'start': s['start'],
                'duration': s['duration']
            })

        # Determine immediate setpoint
        first_action = schedule[0]['action']
        setpoint = 0.0
        if first_action == 'discharge':
            setpoint = -10000.0 # Export
        elif first_action == 'charge':
            setpoint = 10000.0 # Import (handled by Victron schedule usually, but setpoint helps)

        return {
            'schedule': schedule,
            'victron_slots': formatted_slots,
            'setpoint': setpoint
        }

def optimize_schedule(current_soc, price_data, load_forecast=None, pv_forecast=None):
    engine = OptimizationEngine()
    return engine.optimize(current_soc, price_data, load_forecast, pv_forecast)
