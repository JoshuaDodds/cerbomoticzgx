"""Declarative schema for the configuration view.

Each entry describes a setting so the UI can render it with a human label,
description and type. This same schema will drive editable "knobs" in a future
iteration, so keep labels/descriptions user-facing and accurate.
"""

CONFIG_SCHEMA = [
    {
        "group": "AI ESS Optimizer",
        "settings": [
            {"key": "AI_POWERED_ESS_ALGORITHM", "label": "AI optimizer enabled", "type": "bool",
             "desc": "Master switch for the AI-powered ESS optimizer."},
            {"key": "OPTIMIZER_SLOT_MINUTES", "label": "Planning resolution (min)", "type": "int",
             "desc": "Sub-divides hourly prices into slots of this size; 15 is future-ready for quarter-hourly prices."},
            {"key": "OPTIMIZER_SOC_STEP_PCT", "label": "SoC step (%)", "type": "float",
             "desc": "DP discretization step. Smaller = finer control, more compute."},
            {"key": "ESS_TERMINAL_VALUE_FACTOR", "label": "Terminal value factor", "type": "float",
             "desc": "Value of end-of-horizon stored energy as a multiple of the horizon mean buy price."},
            {"key": "ESS_EXPECTED_PEAK_PRICE", "label": "Expected peak price", "type": "float",
             "desc": "Holds charge for the typical peak; 0 disables."},
            {"key": "ESS_MIN_SELL_PRICE", "label": "Min sell price floor", "type": "float",
             "desc": "Battery is never actively exported below this price (PV surplus still exports)."},
            {"key": "ESS_BATTERY_CYCLE_COST", "label": "Battery cycle cost", "type": "float",
             "desc": "Wear cost per kWh discharged; discourages cycling for marginal arbitrage. 0 disables."},
        ],
    },
    {
        "group": "Battery & Power Limits",
        "settings": [
            {"key": "BATTERY_CAPACITY_KWH", "label": "Battery capacity (kWh)", "type": "float", "desc": ""},
            {"key": "AC_DC_CHARGE_EFFICIENCY", "label": "Charge efficiency", "type": "float", "desc": ""},
            {"key": "AC_DC_DISCHARGE_EFFICIENCY", "label": "Discharge efficiency", "type": "float", "desc": ""},
            {"key": "ESS_MAX_GRID_IMPORT_KW", "label": "Max grid import (kW)", "type": "float", "desc": ""},
            {"key": "ESS_MAX_GRID_EXPORT_KW", "label": "Max grid export (kW)", "type": "float", "desc": ""},
            {"key": "MIN_SOC_RESERVE_WINTER", "label": "Winter SoC reserve (%)", "type": "float", "desc": ""},
            {"key": "MIN_SOC_RESERVE_SUMMER", "label": "Summer SoC reserve (%)", "type": "float", "desc": ""},
            {"key": "ESS_EXPORT_AC_SETPOINT", "label": "Max export setpoint (W)", "type": "float", "desc": ""},
        ],
    },
    {
        "group": "Pricing & Forecasts",
        "settings": [
            {"key": "TIBBER_PRICE_RESOLUTION", "label": "Tibber price resolution", "type": "str",
             "options": ["QUARTER_HOURLY", "HOURLY"],
             "desc": "QUARTER_HOURLY (15-min) or HOURLY."},
            {"key": "ESS_EXPORT_PRICE_FACTOR", "label": "Export price factor", "type": "float",
             "desc": "sell = buy * factor - fee."},
            {"key": "ESS_EXPORT_FEE", "label": "Export fee", "type": "float", "desc": ""},
            {"key": "DAILY_HOME_ENERGY_CONSUMPTION", "label": "Daily consumption (kWh)", "type": "float",
             "desc": "Fallback when the VRM consumption forecast is unavailable."},
            {"key": "NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED", "label": "Negative-price feed-in limit", "type": "bool",
             "desc": "Limit grid feed-in to 0W while the price is negative."},
        ],
    },
]
