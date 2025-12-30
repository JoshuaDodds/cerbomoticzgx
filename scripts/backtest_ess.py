import sys
import os
import json
from datetime import datetime, timedelta
import random

# Add repo root to path
sys.path.append(os.getcwd())

from lib.ai_powered_ess import OptimizationEngine

def run_backtest():
    print("Running backtest for AI Powered ESS Algorithm...")

    engine = OptimizationEngine()
    engine.battery_capacity = 45.0

    # Generate 48 hours of dummy price data
    prices = []
    base_time = datetime.now()
    print("Generating price data...")
    for i in range(48 * 4): # 48 hours, 15 min slots
        t = base_time + timedelta(minutes=15 * i)
        hour = t.hour
        # Create a typical price curve
        price = 0.20
        if 2 <= hour < 5: price = 0.05 # Super cheap
        if 17 <= hour < 20: price = 0.50 # Expensive

        # Add some noise
        price += random.uniform(-0.02, 0.02)

        prices.append({'start': t, 'total': price, 'level': 'NORMAL'})

    start_soc = 50.0
    print(f"Starting optimization with SoC: {start_soc}%")

    start_time = datetime.now()
    result = engine.optimize(start_soc, prices)
    end_time = datetime.now()

    if not result:
        print("Optimization failed.")
        return

    print(f"Optimization completed in {(end_time - start_time).total_seconds():.3f}s")

    schedule = result['schedule']
    victron_slots = result['victron_slots']

    print(f"Generated {len(victron_slots)} Victron charge slots.")
    for s in victron_slots:
        print(f"  Slot: {s['start'].strftime('%H:%M')} Duration: {s['duration']/60} mins")

    # Calculate simple profit/loss simulation
    total_cost = 0.0
    current_soc_kwh = start_soc / 100.0 * engine.battery_capacity

    print("\nSimulation Step-by-Step:")
    for step in schedule[:10]: # Print first 10 steps
        print(f"{step['time'].strftime('%H:%M')} | Action: {step['action']:<10} | Price: {step['price']:.3f} | SoC: {step['soc']:.1f}%")

    # Simple cost analysis
    cost_with_ess = 0.0
    cost_without_ess = 0.0

    # Assume constant load of 0.5 kW
    load_kw = 0.5

    for step in schedule:
        price = step['price']
        # Without ESS, we just pay for load
        cost_without_ess += (load_kw * 0.25) * price

        # With ESS
        action = step['action']
        grid_import = 0.0
        grid_export = 0.0

        # This is a simplification. The schedule tells us what the battery did.
        # We need to know what the grid did.
        # Grid = Load + Battery Charge - Battery Discharge - PV
        # Assuming PV = 0 for this test.

        # The schedule step['soc'] is the SoC at the END of the step (or start? Let's assume result of action)
        # Wait, my DP implementation: dp[t+1] is cost to reach state at t+1.
        # Schedule: action at time t results in soc at t+1?
        # My schedule reconstruction:
        # insert(0, {'time': future_prices[t-1]['start'], 'action': action, 'soc': curr_soc, ...})
        # curr_soc comes from best_end_soc backtrack.
        # So `step['soc']` is the SoC at the START of the step (curr_soc).
        # And `action` is what we do during that step.

        # Let's verify logic in `ai_powered_ess.py`:
        # `curr_soc = prev_soc` happens at end of loop iteration.
        # So `curr_soc` in `schedule` list is the state BEFORE action.

        # Wait:
        # for t in range(steps, 0, -1):
        #    prev = parent[t][curr_soc] ... prev_soc, action = prev
        #    schedule.insert(0, { ... 'soc': curr_soc ... })
        #    curr_soc = prev_soc
        #
        # Here `curr_soc` is the state at time `t`. `prev_soc` is state at `t-1`.
        # So `step['soc']` is the state AFTER the action?
        # `t` goes from `steps` down to 1.
        # `future_prices[t-1]` is price at `t-1`.
        # `action` transforms `prev_soc` (at `t-1`) to `curr_soc` (at `t`).
        # So `step['soc']` is the Target SoC at end of interval.

        pass

    print("\nBacktest complete.")

if __name__ == "__main__":
    run_backtest()
