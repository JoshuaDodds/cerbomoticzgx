"""Golden characterization of the completed Summer Mode optimizer.

Winter Mode development must not change these direct summer-engine decisions.
The selector has separate tests; this file deliberately imports the completed
engine itself so accidental edits cannot hide behind routing behavior.
"""
from datetime import datetime, timedelta, timezone

from lib.ai_powered_ess import OptimizationEngine


BASE_TIME = datetime(2099, 7, 1, tzinfo=timezone.utc)
PRICES = [0.12, 0.10, 0.11, 0.24, 0.30, 0.28, 0.20, 0.18]


def _summer_engine():
    engine = OptimizationEngine()
    engine.battery_capacity = 40.0
    engine.charge_efficiency = 0.95
    engine.discharge_efficiency = 0.95
    engine.max_power_import = 10.0
    engine.max_power_export = 10.0
    engine.max_charge_power = 10.0
    engine.max_discharge_power = 10.0
    engine.export_price_factor = 1.0
    engine.export_fee = 0.0
    engine.terminal_value_factor = 0.0
    engine.expected_peak_price = 0.0
    engine.min_sell_price = 0.18
    engine.cost_basis_eur_per_dc_kwh = 0.0
    engine.cost_basis_sell_floor = 0.0
    engine.cycle_cost = 0.02
    engine.arbitrage_margin = 0.02
    engine.slot_minutes = 60.0
    engine.daily_load_kwh = 24.0
    engine.min_soc = 5.0
    engine.max_grid_charge_soc = 90.0
    engine.soc_step = 5.0
    engine.soc_states = [float(i) for i in range(0, 101, 5)]
    return engine


def _prices():
    return [
        {
            "start": BASE_TIME + timedelta(hours=index),
            "total": price,
            "level": "NORMAL",
        }
        for index, price in enumerate(PRICES)
    ]


def _signature(result):
    return [
        (
            step["time"].hour,
            step["control_action"],
            round(step["soc_start"], 4),
            round(step["soc_end"], 4),
            round(step["grid_energy"], 4),
            step["reason_code"],
        )
        for step in result["schedule"]
    ]


def test_completed_summer_optimizer_golden_arbitrage_plan():
    result = _summer_engine().optimize(
        20.0,
        _prices(),
        load_forecast=[0.5] * 8,
        pv_forecast=[0.0] * 8,
    )

    assert _signature(result) == [
        (0, "BUY", 20.0, 42.5625, 10.0, "PRECHARGE_FOR_PEAK"),
        (1, "BUY", 42.5625, 65.125, 10.0, "PRECHARGE_FOR_PEAK"),
        (2, "BUY", 65.125, 80.0, 6.7632, "PRECHARGE_FOR_PEAK"),
        (3, "SELL", 80.0, 55.0, -9.0, "PRICE_HIGH"),
        (4, "SELL", 55.0, 30.0, -9.0, "PRICE_PEAK"),
        (5, "SELL", 30.0, 5.0, -9.0, "PRICE_PEAK"),
        (6, "RETAIN", 5.0, 5.0, 0.5, "RESERVE_POLICY"),
        (7, "RETAIN", 5.0, 5.0, 0.5, "RESERVE_POLICY"),
    ]
    assert result["victron_slots"] == [
        {"start": BASE_TIME, "duration": 10800, "target_soc": 80}
    ]


def test_completed_summer_optimizer_result_contract_is_frozen():
    result = _summer_engine().optimize(
        20.0,
        _prices(),
        load_forecast=[0.5] * 8,
        pv_forecast=[0.0] * 8,
    )

    assert {
        "schedule",
        "victron_slots",
        "optimizer_guardrails",
        "setpoint",
        "mode",
        "control_action",
        "reason",
        "reason_code",
        "grid_assist",
        "pv_surplus",
        "current_price",
        "limit_feed_in",
        "slot_duration_h",
    } <= result.keys()
    assert {
        "time",
        "action",
        "control_action",
        "soc_start",
        "soc_end",
        "grid_energy",
        "pv",
        "load",
        "price",
        "sell",
        "reason",
        "reason_code",
    } <= result["schedule"][0].keys()
