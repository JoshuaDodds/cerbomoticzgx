import time
import threading
import schedule as scheduler

from paho.mqtt import publish
from lib.config_retrieval import retrieve_setting
from lib.constants import cerboGxEndpoint, systemId0
from lib.constants import logging, PythonToVictronWeekdayNumberConversion
from lib.helpers import get_seasonally_adjusted_max_charge_slots, calculate_max_discharge_slots_needed, publish_message, round_up_to_nearest_10, remove_message, current_min_soc_reserve, clear_victron_schedules as _clear_victron_schedules_helper
from lib.tibber_api import lowest_48h_prices, lowest_24h_prices
from lib.notifications import pushover_notification
from lib.tibber_api import publish_pricing_data, get_all_price_points
from lib.global_state import GlobalStateClient
from lib.victron_integration import ac_power_setpoint, limit_grid_feed_in, set_minimum_ess_soc
from lib.ai_powered_ess import optimize_schedule
from lib import history_store as _hist

STATE = GlobalStateClient()


def _get_float_setting(setting_name: str, default: float) -> float:
    """Return a configuration value parsed as float with a safe default."""
    raw_value = retrieve_setting(setting_name)
    if raw_value in (None, "", "None"):
        return default

    try:
        return float(raw_value)
    except (TypeError, ValueError):
        logging.warning(
            "EnergyBroker: Unable to parse %s value '%s'. Falling back to default %.2f.",
            setting_name,
            raw_value,
            default,
        )
        return default


def _optimizer_guardrails_snapshot() -> dict:
    return {
        'max_grid_charge_soc': _get_float_setting('ESS_MAX_GRID_CHARGE_SOC', 100.0),
        'min_sell_price': _get_float_setting('ESS_MIN_SELL_PRICE', 0.0),
        'battery_cycle_cost': _get_float_setting('ESS_BATTERY_CYCLE_COST', 0.0),
        'arbitrage_margin': _get_float_setting('ESS_ARBITRAGE_MARGIN', 0.0),
    }


# Module configuration parsed safely so a missing/blank setting can never crash
# the import of this module (which controls critical power infrastructure).
MAX_TIBBER_BUY_PRICE = _get_float_setting('MAX_TIBBER_BUY_PRICE', 0.20)
ESS_EXPORT_AC_SETPOINT = _get_float_setting('ESS_EXPORT_AC_SETPOINT', -10000.0)
DAILY_HOME_ENERGY_CONSUMPTION = _get_float_setting('DAILY_HOME_ENERGY_CONSUMPTION', 12.0)
_LAST_WEATHER_LOG_SIGNATURE = None
_WEATHER_LOAD_LOG_THRESHOLD_KWH = 0.10
_WEATHER_PV_SHIFT_LOG_THRESHOLD_KWH = 0.25
_WEATHER_PV_TOTAL_LOG_THRESHOLD_KWH = 0.10
_AI_OPTIMIZER_LOCK = threading.Lock()


def _is_truthy(value: str | None, default: bool) -> bool:
    if value in (None, "", "None"):
        return default

    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _weather_context_log_message(summary: dict | None) -> str | None:
    if not summary:
        return None

    def _f(key, default=0.0):
        try:
            return float(summary.get(key, default) or 0.0)
        except (TypeError, ValueError):
            return default

    source = str(summary.get('source') or 'weather').replace('-', ' ').title().replace(' ', '-')
    max_temp = summary.get('max_temp_c')
    load_delta = _f('load_adj_today_kwh')
    pv_shift = _f('pv_shadow_abs_delta_kwh')
    pv_net = _f('pv_shadow_net_delta_kwh')
    hvac_apply = bool(summary.get('hvac_apply'))
    pv_apply = bool(summary.get('pv_apply'))

    def _signed(value):
        return f"{value:+.2f}"

    max_temp_txt = ""
    try:
        max_temp_txt = f" (max {float(max_temp):.1f}C)"
    except (TypeError, ValueError):
        pass

    parts = []
    if abs(load_delta) >= _WEATHER_LOAD_LOG_THRESHOLD_KWH:
        parts.append(f"load {_signed(load_delta)} kWh today{max_temp_txt}")
    if abs(pv_shift) >= _WEATHER_PV_SHIFT_LOG_THRESHOLD_KWH or abs(pv_net) >= _WEATHER_PV_TOTAL_LOG_THRESHOLD_KWH:
        if abs(pv_net) >= _WEATHER_PV_TOTAL_LOG_THRESHOLD_KWH:
            parts.append(f"PV total {_signed(pv_net)} kWh, timing shifted {abs(pv_shift):.2f} kWh")
        else:
            parts.append(f"PV timing shifted {abs(pv_shift):.2f} kWh, total unchanged")

    if not parts:
        return None

    action = "applied" if (hvac_apply or pv_apply) else "observed (not applied)"
    detail = "; ".join(parts)
    return f"Weather forecast {action}: {source} {detail}."


def _weather_context_log_signature(summary: dict | None) -> tuple | None:
    if not summary:
        return None

    def _float_value(key):
        try:
            return float(summary.get(key) or 0.0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _nearest_bucket(value, step):
        return round(value / step) if step else value

    def _floor_bucket(value, step):
        return int(abs(value) / step) if step else value

    return (
        summary.get('source'),
        bool(summary.get('hvac_apply')),
        bool(summary.get('pv_apply')),
        _nearest_bucket(_float_value('max_temp_c'), 0.5),
        _nearest_bucket(_float_value('load_adj_today_kwh'), 0.25),
        _floor_bucket(_float_value('pv_shadow_abs_delta_kwh'), 1.0),
        _nearest_bucket(_float_value('pv_shadow_net_delta_kwh'), 0.10),
    )


def _log_weather_context_once(summary: dict | None) -> None:
    global _LAST_WEATHER_LOG_SIGNATURE

    msg = _weather_context_log_message(summary)
    if not msg:
        return

    signature = _weather_context_log_signature(summary)
    if signature == _LAST_WEATHER_LOG_SIGNATURE:
        return
    _LAST_WEATHER_LOG_SIGNATURE = signature
    logging.info(msg)


def _should_skip_night_charge(
    batt_soc: float | None,
    charge_context: str | None,
) -> tuple[bool, str | None]:
    """Determine whether the nightly charge schedule should be skipped."""
    if charge_context != "nightly":
        return False, None

    if batt_soc is None:
        return False, None

    if not _is_truthy(retrieve_setting("NIGHT_CHARGE_SKIP_ENABLED"), True):
        return False, None

    min_soc = _get_float_setting("NIGHT_CHARGE_SKIP_MIN_SOC", 70.0)
    max_soc = _get_float_setting("NIGHT_CHARGE_SKIP_MAX_SOC", 100.0)

    if max_soc < min_soc:
        max_soc = min_soc

    if min_soc <= batt_soc <= max_soc:
        message = (
            "EnergyBroker: Skipping nightly charge schedule because battery "
            f"SoC is {round(batt_soc, 2)}% which falls within the configured "
            f"skip range of {min_soc}-{max_soc}%."
        )
        return True, message

    if batt_soc > max_soc:
        message = (
            "EnergyBroker: Skipping nightly charge schedule because battery "
            f"SoC is {round(batt_soc, 2)}% which is above the configured skip "
            f"maximum of {max_soc}%."
        )
        return True, message

    return False, None

# Tracks the last-logged AI optimizer health state so we log only on transitions
# (active<->fallback) instead of on every event-driven invocation.
_ai_health_last_logged = {"state": None}


def _ai_optimizer_active_and_healthy() -> bool:
    """True when the AI optimizer is enabled and has succeeded within the last hour."""
    if not _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        return False
    last_ai_success = STATE.get('ai_success_timestamp')
    try:
        return bool(last_ai_success) and (time.time() - float(last_ai_success) < 3600)
    except (TypeError, ValueError):
        return False


def _log_ai_health_transition(healthy: bool) -> None:
    """Log the AI health state only when it changes, to avoid log spam."""
    if _ai_health_last_logged["state"] == healthy:
        return
    _ai_health_last_logged["state"] = healthy
    if healthy:
        logging.info("EnergyBroker: AI Optimizer active and healthy; legacy buy/sell logic standing down.")
    else:
        logging.warning("EnergyBroker: AI Optimizer enabled but stale/unhealthy; legacy logic active as fallback.")


def main():
    logging.info("EnergyBroker: Initializing...")
    schedule_tasks()
    logging.info("EnergyBroker: Initialization complete.")


def schedule_tasks():
    # ESS Scheduled Tasks
    scheduler.every().hour.at(":00").do(manage_sale_of_stored_energy_to_the_grid)

    # AI Optimization Loop — aligned to the clock quarter-hours (:00/:15/:30/:45)
    # so each 15-minute price slot's decision is applied right at its boundary,
    # not offset by however many minutes after start the loop happens to fire.
    for _qh in (":00", ":15", ":30", ":45"):
        scheduler.every().hour.at(_qh).do(run_ai_optimizer)

    # Poll Domoticz for EV charging power + gas usage for the dashboard (best-effort).
    scheduler.every(1).minutes.do(_publish_domoticz_aux)

    # Daily next-day pricing refresh + (re)optimization.
    # Tibber publishes the next day's day-ahead prices around 13:00 local time.
    # At 13:05 we refresh pricing so the optimizer can plan over the full
    # today+tomorrow (48h) horizon and place charge/discharge across the day
    # boundary when that maximises revenue over the monthly settlement period.
    scheduler.every().day.at("13:05").do(run_daily_price_update_and_optimize)

    # Grid Charging Scheduled Tasks
    scheduler.every().day.at("09:30").do(set_charging_schedule, caller="TaskScheduler()", silent=True)
    scheduler.every().day.at(
        "21:30"
    ).do(
        set_charging_schedule,
        caller="TaskScheduler()",
        silent=True,
        schedule_type='48h',
        charge_context='nightly',
    )

    # History maintenance: roll complete past months of NDJSON into immutable Parquet.
    # Runs in the small hours; idempotent and self-healing, so most days it's a no-op
    # (only the just-completed month, on the 1st, has anything to do).
    scheduler.every().day.at("03:20").do(_run_history_compaction)


def _run_history_compaction():
    """Compact complete past months of history NDJSON into per-month Parquet.

    Only past months are touched (the current month stays hot NDJSON), the write is
    atomic, and source NDJSON is removed only after a verified rename — so a pod restart
    mid-run can't lose data. Gated by ``HISTORY_COMPACTION_ENABLED`` (default on) and a
    working DuckDB; any failure is logged and never affects control.
    """
    import os
    try:
        if not _is_truthy(retrieve_setting('HISTORY_COMPACTION_ENABLED'), True):
            return
        if not _hist.duckdb_available():
            logging.info("EnergyBroker: history compaction skipped — duckdb not installed.")
            return
        history_dir = retrieve_setting('HISTORY_DIR') or 'data/history'
        done = _hist.backfill_cold_months(history_dir, remove_ndjson=True)
        if done:
            logging.info("EnergyBroker: history compaction wrote %d Parquet file(s): %s",
                         len(done), ", ".join(os.path.basename(p) for p in done))
    except Exception as e:
        logging.warning("EnergyBroker: history compaction failed: %s", e)


def _publish_domoticz_aux():
    """Read EV charging power + gas usage from Domoticz and publish them to STATE
    (mirrored to the MQTT GlobalState topics) so the dashboard can show an EV node
    and gas usage. Best-effort and read-only — any failure is ignored and never
    affects ESS control. IDXs are configurable (DOMOTICZ_EV_IDX / DOMOTICZ_GAS_IDX).
    """
    try:
        from lib.domoticz_updater import domoticz_device_number, domoticz_sun_times
        ev_idx = retrieve_setting('DOMOTICZ_EV_IDX') or '627'
        gas_idx = retrieve_setting('DOMOTICZ_GAS_IDX') or '291'
        ev_w = domoticz_device_number(int(ev_idx), fields=("Usage", "Data"))     # live W
        if ev_w is not None:
            STATE.set('ev_power', round(ev_w, 1))
        ev_kwh = domoticz_device_number(int(ev_idx), fields=("CounterToday",))   # today kWh
        if ev_kwh is not None:
            STATE.set('ev_today_kwh', round(ev_kwh, 3))
        gas_m3 = domoticz_device_number(int(gas_idx), fields=("CounterToday", "Data"))
        if gas_m3 is not None:
            STATE.set('gas_today_m3', round(gas_m3, 3))
        sunrise, sunset = domoticz_sun_times()
        if sunrise:
            STATE.set('sun_rise', sunrise)
        if sunset:
            STATE.set('sun_set', sunset)
        if ev_w is None and gas_m3 is None:
            logging.warning("AI_ESS: Domoticz aux read returned nothing — check DZ_URL_PREFIX "
                            "and IDXs (ev=%s, gas=%s).", ev_idx, gas_idx)
        else:
            logging.debug("AI_ESS: Domoticz aux — ev=%sW gas=%sm3 sun=%s/%s",
                          ev_w, gas_m3, sunrise, sunset)
    except Exception as e:
        logging.warning(f"AI_ESS: Domoticz aux publish failed: {e}")


def retrieve_latest_tibber_pricing():
    if retrieve_setting('TIBBER_UPDATES_ENABLED') != '1':
        return None
    else:
        publish_pricing_data(__name__)
        logging.debug(f"EnergyBroker: Running task: retrieve_latest_tibber_pricing()")


def publish_export_schedule(price_list: list) -> None:
    if price_list:
        if len(price_list) == 0:
            message = "No export today."
        elif len(price_list) == 1:
            item = "{:.4f}".format(price_list[0])
            message = f"Export at: {item}"
        else:
            items = " and ".join("{:.4f}".format(item) for item in price_list)
            message = f"Export at: {items}"

        publish_message("Tibber/home/price_info/today/tibber_export_schedule_status", message=message, retain=True)

    else:
        publish_message("Tibber/home/price_info/today/tibber_export_schedule_status", message="No export today.", retain=True)


def get_todays_n_highest_prices(batt_soc: float, ess_net_metering_batt_min_soc: float = 0.0) -> list:
    ess_net_metering_enabled = STATE.get('ess_net_metering_enabled') or False

    if batt_soc > ess_net_metering_batt_min_soc and ess_net_metering_enabled:
        n = calculate_max_discharge_slots_needed(batt_soc - ess_net_metering_batt_min_soc)
        prices = [
            STATE.get('tibber_cost_highest_today'),
            STATE.get('tibber_cost_highest2_today'),
            STATE.get('tibber_cost_highest3_today'),
        ]

        sorted_items = sorted(prices, reverse=True)
        price_list = sorted_items[:n] if len(sorted_items[:n]) != 0 else None
        if price_list:
            publish_export_schedule(price_list)

        return price_list

    else:
        message = "No export scheduled."
        publish_message("Tibber/home/price_info/today/tibber_export_schedule_status", message=message, retain=True)

        return None


def should_start_selling(price_now: float, batt_soc: float, ess_net_metering_batt_min_soc: float):
    prices = get_todays_n_highest_prices(batt_soc=batt_soc, ess_net_metering_batt_min_soc=ess_net_metering_batt_min_soc)
    if not prices:
        return False
    else:
        return any(price_now >= price for price in prices)


def manage_sale_of_stored_energy_to_the_grid() -> None:
    # Defer to the AI optimizer when it is enabled and healthy (log only on change).
    if _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        healthy = _ai_optimizer_active_and_healthy()
        _log_ai_health_transition(healthy)
        if healthy:
            return

    batt_soc = STATE.get('batt_soc')
    tibber_price_now = STATE.get('tibber_price_now') or 0
    ac_setpoint = STATE.get('ac_power_setpoint')
    ess_net_metering = STATE.get('ess_net_metering_enabled') or False
    ess_net_metering_overridden = STATE.get('ess_net_metering_overridden') or False
    ess_net_metering_batt_min_soc = STATE.get('ess_net_metering_batt_min_soc')

    get_todays_n_highest_prices(batt_soc, ess_net_metering_batt_min_soc)

    if ess_net_metering_overridden:
        if batt_soc <= ess_net_metering_batt_min_soc:
            if ac_setpoint < 0.0:
                ac_power_setpoint(watts="0.0", override_ess_net_mettering=False)
                logging.info(f"AC Power Setpoint changed to 0.0")
                logging.info(f"Stopped energy export at {batt_soc}% SOC because of DynEss min batt SoC configuration setting.")
                pushover_notification("Energy Sale Alert",
                                      f"Stopped energy export at {batt_soc}% SOC because of DynEss min batt SoC setting.")

    if not ess_net_metering_overridden:
        if batt_soc > ess_net_metering_batt_min_soc \
                and should_start_selling(tibber_price_now, batt_soc, ess_net_metering_batt_min_soc) \
                and tibber_price_now > 0 \
                and ess_net_metering:

            if ac_setpoint != ESS_EXPORT_AC_SETPOINT:
                ac_power_setpoint(watts=str(ESS_EXPORT_AC_SETPOINT), override_ess_net_mettering=False)

                logging.info(f"Beginning to sell energy at {batt_soc}% SOC and a price of {round(tibber_price_now, 3)}")
                pushover_notification("Energy Sale Alert",
                                      f"Beginning to sell energy at a cost of {round(tibber_price_now, 3)}")
        else:
            if ess_net_metering and ac_setpoint < 0.0:
                ac_power_setpoint(watts="0.0", override_ess_net_mettering=False)

                logging.info(f"AC Power Setpoint changed to 0.0")
                logging.info(f"Stopped energy export at {batt_soc}% SOC and a current price of {round(tibber_price_now, 3)}")
                pushover_notification("Energy Sale Alert",
                                      f"Stopped energy export at {batt_soc}% and a current price of {round(tibber_price_now, 3)}")


def adjust_grid_setpoint(watts, override_ess_net_mettering):
    target_watts = int(round_up_to_nearest_10(watts))
    ac_power_setpoint(watts=target_watts, override_ess_net_mettering=override_ess_net_mettering, silent=True)
    return target_watts


def manage_grid_usage_based_on_current_price(price: float = None, power: any = None) -> None:
    """
    Manages and allows automatic or manual toggle of a "passthrough" mode control loop which matches power consumption
    to a grid setpoint to allow consumption from grid while having a fallback to battery in case of grid instability.
    """
    # When the AI optimizer is in control, the legacy auto/manual grid logic must
    # stand down so it does not clobber the AI's setpoint. The one exception is
    # AI grid-assist ("retain") mode, where we DO match the grid setpoint to the
    # live house load so the battery is held.
    if _ai_optimizer_active_and_healthy():
        manual_grid_assist = _manual_grid_charge_on()
        if STATE.get('ai_grid_assist') == 'on' or manual_grid_assist:
            # Retain mode (AI plan OR manual grid-charge override): import only what
            # PV can't cover; stay at 0 when PV covers the load so surplus PV charges
            # the battery / exports. The manual override keeps the setpoint tracking
            # live load between 15-min cycles so a full-power EV charge is covered by
            # the grid the instant it ramps, without draining the battery.
            _apply_grid_assist_setpoint(power, cover_all_load=manual_grid_assist)
        return

    ess_net_metering_overridden = STATE.get('ess_net_metering_overridden') or False
    price = price if price is not None else STATE.get('tibber_price_now')
    grid_charging_enabled = STATE.get('grid_charging_enabled') or False
    grid_charging_enabled_by_price = STATE.get('grid_charging_enabled_by_price') or False
    SWITCH_TO_GRID_PRICE_THRESHOLD = float(retrieve_setting('SWITCH_TO_GRID_PRICE_THRESHOLD'))

    # Manual Mode Setpoint Management: used when grid assist has been manually toggled on
    if ess_net_metering_overridden and grid_charging_enabled and not grid_charging_enabled_by_price and power:
        setpoint = adjust_grid_setpoint(power, override_ess_net_mettering=True)
        logging.debug(f"Setpoint adjusted to: {setpoint}")
        return

    # Auto Mode State Change: Toggle grid charging based on price and send a single notification on state change
    if not ess_net_metering_overridden or grid_charging_enabled_by_price:
        if price <= SWITCH_TO_GRID_PRICE_THRESHOLD and not grid_charging_enabled_by_price:
            logging.info(f"Energy cost is {round(price, 3)} cents per kWh. Switching to grid energy.")
            pushover_notification(
                "Auto Grid Assist On",
                f"Energy cost is {round(price, 3)} cents per kWh. Switching to grid energy."
            )
            STATE.set('grid_charging_enabled_by_price', True)

        elif price > SWITCH_TO_GRID_PRICE_THRESHOLD and grid_charging_enabled_by_price:
            logging.info(f"Energy cost is {round(price, 3)} cents per kWh. Switching back to battery.")
            pushover_notification(
                "Auto Grid Assist Off",
                f"Energy cost is {round(price, 3)} cents per kWh. Switching back to battery."
            )
            STATE.set('grid_charging_enabled_by_price', False)
            ac_power_setpoint(watts="0.0", override_ess_net_mettering=False, silent=False)

    # Auto Mode Setpoint Management: Manage setpoints if grid charging has been enabled by price threshold targets
    if grid_charging_enabled_by_price and power:
        setpoint = adjust_grid_setpoint(power, ess_net_metering_overridden)
        logging.debug(f"Setpoint adjusted to: {setpoint}")


def publish_mqtt_trigger():
    """ Triggers the event_handler to call set_charging_schedule() function"""
    publish_message("Cerbomoticzgx/EnergyBroker/RunTrigger", payload=f"{{\"value\": {time.localtime().tm_hour}}}", retain=False)


def set_charging_schedule(
    caller=None,
    price_cap=MAX_TIBBER_BUY_PRICE,
    silent=False,
    schedule_type=None,
    slots=None,
    charge_context=None,
):
    # Defer to the AI optimizer when it is enabled and healthy (log only on change).
    if _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        healthy = _ai_optimizer_active_and_healthy()
        _log_ai_health_transition(healthy)
        if healthy:
            return True

    batt_soc = STATE.get('batt_soc')

    # Determine schedule type if not explicitly provided
    if schedule_type is None:
        if 50 <= batt_soc <= 100:
            schedule_type = '48h'
        else:
            schedule_type = '24h'

    # Convert forecast from Wh to kWh and subtract expected day usage
    pv_precalc = round((STATE.get('pv_projected_remaining') / 1000 - DAILY_HOME_ENERGY_CONSUMPTION), 2) or 0.0
    pv_forecast_min_consumption_forecast = pv_precalc if pv_precalc > 0 else 0.0

    if slots is not None:
        max_items = slots
    else:
        # Get maximum items to charge based on current battery SOC and solar forecast
        max_items = get_seasonally_adjusted_max_charge_slots(batt_soc, pv_forecast_min_consumption_forecast)

    # Log the schedule request details
    logging.info(
        "EnergyBroker: set up %s charging schedule request received by %s using batt_soc=%s%% and expected solar surplus of %s kWh",
        schedule_type,
        caller,
        batt_soc,
        pv_forecast_min_consumption_forecast,
    )

    should_skip, skip_message = _should_skip_night_charge(batt_soc, charge_context)
    if should_skip:
        logging.info(skip_message)
        return False

    # If no charging slots are needed, return early
    if max_items < 1:
        return False

    # Clear the existing Victron schedules
    clear_victron_schedules()

    # Determine new schedule based on schedule type
    if schedule_type == '24h':
        new_schedule = lowest_24h_prices(price_cap=price_cap, max_items=max_items)
    elif schedule_type == '48h':
        new_schedule = lowest_48h_prices(price_cap=price_cap, max_items=max_items)
    else:
        raise ValueError("Invalid schedule type. Use '24h' or '48h'.")

    # Schedule the Victron ESS charging based on the new schedule
    if len(new_schedule) > 0:
        schedule = 0
        for item in new_schedule:
            hour = int(item[1])
            day = item[0]
            price = item[3]
            schedule_victron_ess_charging(int(hour), schedule=schedule, day=day)
            remove_message("Cerbomoticzgx/EnergyBroker/RunTrigger")  # Remove any retained messages on the topic which might retrigger scheduling again
            if not silent:
                push_notification(hour, day, price)
            schedule += 1

    return True


def schedule_victron_ess_charging(hour, schedule=0, duration=3600, day=0):
    """
    :param schedule: integer between 0 and 4 for the five available scheduling slots
    :param hour: integer between 0 and 23
    :param duration: duration of charging in seconds (defaults to one hour)
    :param day: 0 or 1 which maps relatively to today or tomorrow
    :return: None
    """
    weekday = PythonToVictronWeekdayNumberConversion[time.localtime().tm_wday] if day == 0 else \
        PythonToVictronWeekdayNumberConversion[time.localtime(time.time() + 86400).tm_wday]

    if hour > 23:
        raise Exception("OoBError: hour must be an integer between 0 and 23")

    topic_stub = f"W/{systemId0}/settings/0/Settings/CGwacs/BatteryLife/Schedule/Charge/{schedule}/"
    soc = 100
    start = hour * 3600

    publish_message(f"{topic_stub}Duration", payload=f"{{\"value\": {duration}}}", retain=True)
    publish_message(f"{topic_stub}Soc", payload=f"{{\"value\": {soc}}}", retain=True)
    publish_message(f"{topic_stub}Start", payload=f"{{\"value\": {start}}}", retain=True)
    publish_message(f"{topic_stub}Day", payload=f"{{\"value\": {weekday}}}", retain=True)

    logging.info(f"EnergyBroker: Adding schedule entry for day:{weekday}, duration:{duration}, start: {start}")

def clear_victron_schedules():
    _clear_victron_schedules_helper()

def push_notification(hour, day, price):
    topic = f"Energy Broker Alert"
    msg = f"ESS Charge scheduled for {hour}:00 {'Today' if day == 0 else 'Tomorrow'} @ {price}"
    pushover_notification(topic, msg)

def run_daily_price_update_and_optimize():
    """Refresh Tibber pricing (so the just-published next-day prices are
    available) and immediately re-run the optimizer over the latest available
    horizon.

    Scheduled for 13:05 local time, shortly after Tibber publishes day-ahead
    prices for tomorrow."""
    retrieve_latest_tibber_pricing()
    logging.info("EnergyBroker: 13:05 pricing refresh requested; running optimizer with latest available horizon.")
    run_ai_optimizer()


PV_DAYLIGHT_START_H, PV_DAYLIGHT_END_H = 5, 22


def _pv_shape_by_slot(days: int = 3) -> dict:
    """Learned PV *shape*: mean realised PV power (kW) per quarter-hour-of-day
    over the last ``days`` days (DAYLIGHT slots only), keyed by ``'HH:MM'``.

    Used as relative weights to redistribute the day's PV total, and as the basis
    for the intraday "elapsed fraction" used to self-correct the magnitude
    (``_pv_intraday_remaining_kwh``). Only sun-up hours
    (``PV_DAYLIGHT_START_H..PV_DAYLIGHT_END_H``) are included so night/evening
    zeros don't dilute the curve or skew the cumulative-fraction maths. This is
    weather-robust precisely because it's a normalised shape, not an average of
    absolute generation. Returns {} when no usable history (caller falls back to
    an even spread).
    """
    import os
    import json
    from datetime import datetime as _dt, timedelta as _td

    try:
        history_dir = retrieve_setting('HISTORY_DIR') or 'data/history'
        buckets = {}
        today = _dt.now().date()
        for i in range(max(1, days)):
            d = today - _td(days=i)
            for r in _hist.read_day(d, history_dir):   # NDJSON hot + Parquet cold
                if r.get('kind') == 'settlement':
                    continue
                pv, ts = r.get('pv_w'), r.get('ts')
                if pv is None or ts is None:
                    continue
                try:
                    when = _dt.fromisoformat(ts)
                    if not (PV_DAYLIGHT_START_H <= when.hour < PV_DAYLIGHT_END_H):
                        continue   # daylight only — exclude night/evening
                    key = f"{when.hour:02d}:{(when.minute // 15) * 15:02d}"
                    buckets.setdefault(key, []).append(max(0.0, float(pv)) / 1000.0)
                except (TypeError, ValueError):
                    continue
        return {k: sum(v) / len(v) for k, v in buckets.items() if v}
    except Exception as e:
        logging.debug(f"EnergyBroker: PV shape read failed: {e}")
        return {}


def _pv_intraday_remaining_kwh(shape: dict, remaining_vrm_kwh: float, now=None) -> float:
    """Self-correct the remaining-PV magnitude from today's realised production.

    VRM anchors the forecast to a fixed daily total set overnight, so it drifts from
    reality in BOTH directions:
      * on a better-than-forecast day ``pv_projected_remaining`` (= VRM_total - actual)
        collapses toward 0 while the panels still produce strongly — starving the
        rest-of-day forecast; and
      * on a cloudier-than-forecast day VRM's total stays high, so the remaining
        forecast stays optimistic all day and the optimizer plans (and projects P/L)
        on PV that never arrives.
    Here we project today's total from the PV produced SO FAR and the share of the
    day's solar curve that should be complete by now (from the learned DAYLIGHT
    shape), then scale the remaining TOWARD that projection — damped and capped. On a
    normal day the projection ≈ VRM so nothing changes. Upward correction starts as
    soon as ``ESS_PV_INTRADAY_MIN_ELAPSED`` of the curve has passed; the downward
    correction waits until ``ESS_PV_INTRADAY_DOWN_MIN_ELAPSED`` so a brief morning
    cloud that later clears can't collapse the whole day. Returns the (adjusted)
    remaining-PV kWh for today.

    Tunables: ``ESS_PV_INTRADAY_CORRECTION`` (damp 0..1, 0 disables),
    ``ESS_PV_INTRADAY_MAX_RATIO`` (cap projected day total vs VRM),
    ``ESS_PV_INTRADAY_MIN_RATIO`` (floor projected day total vs VRM),
    ``ESS_PV_INTRADAY_MIN_ELAPSED`` (min daylight fraction before scaling up),
    ``ESS_PV_INTRADAY_DOWN_MIN_ELAPSED`` (min daylight fraction before scaling down).
    """
    try:
        damp = _get_float_setting('ESS_PV_INTRADAY_CORRECTION', 0.6)
        if damp <= 0 or not shape:
            return remaining_vrm_kwh
        max_ratio = _get_float_setting('ESS_PV_INTRADAY_MAX_RATIO', 1.6)
        min_ratio = _get_float_setting('ESS_PV_INTRADAY_MIN_RATIO', 0.25)
        min_elapsed = _get_float_setting('ESS_PV_INTRADAY_MIN_ELAPSED', 0.10)
        down_min_elapsed = _get_float_setting('ESS_PV_INTRADAY_DOWN_MIN_ELAPSED', 0.30)

        from datetime import datetime as _dt
        now = now or _dt.now().astimezone()
        if not (PV_DAYLIGHT_START_H <= now.hour < PV_DAYLIGHT_END_H):
            return remaining_vrm_kwh   # outside daylight — nothing to extrapolate

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        actual = _f(STATE.get('c1_daily_yield')) + _f(STATE.get('c2_daily_yield'))
        if actual <= 0:
            return remaining_vrm_kwh

        def _slot_min(k):
            try:
                h, m = k.split(':')
                return int(h) * 60 + int(m)
            except (ValueError, AttributeError):
                return 0

        now_min = now.hour * 60 + now.minute
        total_w = sum(shape.values())
        if total_w <= 0:
            return remaining_vrm_kwh
        elapsed_w = sum(v for k, v in shape.items() if _slot_min(k) <= now_min)
        frac = elapsed_w / total_w
        if frac < max(1e-3, min_elapsed):
            return remaining_vrm_kwh   # too early in the day to extrapolate reliably

        vrm_remaining = max(0.0, remaining_vrm_kwh)
        vrm_total = actual + vrm_remaining
        projected_total = actual / frac
        if vrm_total > 0:
            projected_total = min(projected_total, max_ratio * vrm_total)  # cap over-projection
            projected_total = max(projected_total, min_ratio * vrm_total)  # floor under-projection
        projected_remaining = max(0.0, projected_total - actual)

        if projected_remaining >= vrm_remaining:
            # Out-producing VRM (clearer than forecast): scale the remaining UP, damped.
            return vrm_remaining + damp * (projected_remaining - vrm_remaining)

        # Under-producing VRM (cloudier than forecast): scale the remaining DOWN, but only
        # once enough of the solar day has elapsed to trust the extrapolation — a brief
        # morning cloud that later clears must not collapse the whole day. Damped.
        if frac < down_min_elapsed:
            return vrm_remaining
        return vrm_remaining + damp * (projected_remaining - vrm_remaining)
    except Exception as e:
        logging.debug(f"EnergyBroker: PV intraday correction failed: {e}")
        return remaining_vrm_kwh


def _build_pv_forecast_by_slot(price_slots: list, slot_duration_h: float) -> dict:
    """Build a per-slot PV generation forecast (kWh) keyed by slot start time.

    Keeps the VRM daily magnitude (today's remaining + tomorrow's full day, which
    reflect today's weather), self-corrects today's remaining UP when we're
    out-producing the VRM forecast (`_pv_intraday_remaining_kwh`), then distributes
    it across the day using the LEARNED per-slot shape from history
    (`_pv_shape_by_slot`) so e.g. shaded early-morning slots get ~0 instead of an
    even share. Falls back to an even daylight spread when there's no usable history.
    """
    from datetime import date as _date, timedelta as _td

    def _kwh(key):
        try:
            return max(0.0, float(STATE.get(key)) / 1000.0)
        except (TypeError, ValueError):
            return 0.0

    today_kwh = _kwh('pv_projected_remaining')
    tomorrow_kwh = _kwh('pv_projected_tomorrow')

    # Learned daylight shape (trailing N days), used both to redistribute the day's
    # total AND to gauge how far through the solar curve we are for the intraday
    # magnitude correction below.
    shape = _pv_shape_by_slot(int(_get_float_setting('ESS_PV_SHAPE_DAYS', 3)))

    # Self-correct today's remaining UP when we're out-producing the VRM forecast
    # (tomorrow stays on VRM — no actuals yet).
    today_kwh = _pv_intraday_remaining_kwh(shape, today_kwh)

    today = _date.today()
    tomorrow = today + _td(days=1)
    daylight_start_h, daylight_end_h = PV_DAYLIGHT_START_H, PV_DAYLIGHT_END_H

    today_slots, tomorrow_slots = [], []
    for slot in price_slots:
        start = slot['start']
        if not (daylight_start_h <= start.hour < daylight_end_h):
            continue
        d = start.date()
        if d == today:
            today_slots.append(start)
        elif d == tomorrow:
            tomorrow_slots.append(start)

    def _distribute(total, slots):
        if total <= 0 or not slots:
            return {}
        if shape:
            weights = [shape.get(f"{s.hour:02d}:{(s.minute // 15) * 15:02d}", 0.0) for s in slots]
            wsum = sum(weights)
            if wsum > 0:
                return {s: total * w / wsum for s, w in zip(slots, weights)}
        per = total / len(slots)          # fallback: even spread
        return {s: per for s in slots}

    return {**_distribute(today_kwh, today_slots), **_distribute(tomorrow_kwh, tomorrow_slots)}


def _forecast_slots_for_optimizer(price_slots: list, slot_duration_h: float, now=None) -> list:
    """Return the same current/future horizon the optimizer will keep.

    The optimizer keeps the slot whose window contains ``now`` plus future slots.
    Forecast builders must use that same horizon, otherwise "remaining today" PV
    can be spread into already-elapsed daylight slots and then disappear when the
    optimizer drops those past slots.
    """
    if not price_slots:
        return []
    try:
        starts = [slot['start'] for slot in price_slots if slot.get('start') is not None]
    except AttributeError:
        return []
    if not starts:
        return []
    tzinfo = starts[0].tzinfo
    if now is None:
        from datetime import datetime as _dt
        now = _dt.now(tzinfo)
    keep_after = now - _td_hours(slot_duration_h)
    return [slot for slot in price_slots if slot.get('start') and slot['start'] > keep_after]


def _td_hours(hours: float):
    from datetime import timedelta as _td
    return _td(hours=max(0.0, float(hours or 0.0)))


def _latest_settled_pv_slot_kwh(slot_duration_h: float, now=None) -> float | None:
    import os
    import json
    from datetime import datetime as _dt

    try:
        now = now or _dt.now().astimezone()
        history_dir = retrieve_setting('HISTORY_DIR') or 'data/history'
        path = os.path.join(history_dir, f"ess-{now.strftime('%Y-%m-%d')}.ndjson")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            lines = fh.readlines()
        target_s = max(60.0, float(slot_duration_h or 0.25) * 3600.0)
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('kind') != 'settlement' or row.get('incomplete'):
                continue
            try:
                pv = float(row.get('actual_pv_kwh'))
                start = _dt.fromisoformat(row.get('slot_start'))
                end = _dt.fromisoformat(row.get('slot_end'))
            except (TypeError, ValueError):
                continue
            if pv < 0:
                continue
            duration_s = max(1.0, (end - start).total_seconds())
            if not (0.75 * target_s <= duration_s <= 1.30 * target_s):
                continue
            if (now - end).total_seconds() > max(2.0 * target_s, 1800.0):
                continue
            return pv * target_s / duration_s
    except Exception as e:
        logging.debug("EnergyBroker: PV nowcast recent settlement read failed: %s", e)
    return None


def _pv_nowcast_anchor_kwh(slot_duration_h: float, now=None) -> dict | None:
    """Choose a per-slot PV anchor from the latest actual slot and live PV.

    The latest settled slot captures what the system just did; live PV captures
    sudden drop-offs. When live output is materially below the previous slot, use
    live PV and reduce confidence so the forecast follows the tree-line/sundown
    drop instead of projecting stale high output forward.
    """
    try:
        slot_h = max(0.01, float(slot_duration_h or 0.25))
    except (TypeError, ValueError):
        slot_h = 0.25

    recent = _latest_settled_pv_slot_kwh(slot_h, now=now)
    live = None
    try:
        pv_w = float(STATE.get('pv_power') or 0.0)
        if pv_w > 0:
            live = pv_w / 1000.0 * slot_h
    except (TypeError, ValueError):
        live = None

    if recent is None and live is None:
        return None

    drop_ratio = 1.0
    source = "live" if live is not None else "recent"
    if recent is not None and live is not None and recent > 0.05:
        drop_ratio = max(0.0, min(1.5, live / recent))
        if drop_ratio < 0.75:
            anchor = live
            source = "live_drop"
        else:
            anchor = max(live, recent)
            source = "live_recent"
    else:
        anchor = live if live is not None else recent

    if anchor is None or anchor <= 0.03:
        return None
    return {
        "slot_kwh": float(anchor),
        "source": source,
        "drop_ratio": float(drop_ratio),
        "live_slot_kwh": live,
        "recent_slot_kwh": recent,
    }


def _apply_pv_nowcast(pv_forecast: dict, forecast_slots: list, weather_context: dict | None,
                      slot_duration_h: float, now=None) -> dict:
    """Blend live/recent PV evidence into the near-term PV forecast.

    The baseline forecast still owns the full-day shape. This overlay nudges current-day
    near-term slots toward live evidence — raising them when live production and GTI imply
    the baseline is too low, and lowering them when that evidence implies it's too high —
    then fades out over a few hours. Lowering requires real evidence (a GTI ratio derived
    from data, or a confirmed live production drop) so a missing-GTI gap never zeroes a slot.
    """
    if not pv_forecast or not forecast_slots:
        return pv_forecast

    starts = [slot.get('start') for slot in forecast_slots if slot.get('start') is not None]
    if not starts:
        return pv_forecast

    from datetime import datetime as _dt
    now = now or _dt.now(starts[0].tzinfo)
    anchor = _pv_nowcast_anchor_kwh(slot_duration_h, now=now)
    if not anchor:
        return pv_forecast

    weather_context = weather_context if isinstance(weather_context, dict) else {}
    slot_context = weather_context.setdefault("slots", {})
    summary = weather_context.setdefault("summary", {})

    def _gti(start):
        try:
            row = slot_context.get(start.isoformat()) or {}
            return float(row.get("gti_forecast_wm2") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    anchor_gti = next((_gti(s) for s in starts if _gti(s) > 0), 0.0)
    today = now.date()
    out = dict(pv_forecast)
    delta = 0.0
    adjusted_slots = 0
    drop_ratio = float(anchor.get("drop_ratio", 1.0) or 1.0)

    for start in starts:
        if start.date() != today:
            continue
        hours_ahead = max(0.0, (start - now).total_seconds() / 3600.0)
        if hours_ahead > 4.0:
            continue
        if hours_ahead <= 2.0:
            weight = 0.90 - 0.20 * (hours_ahead / 2.0)
        else:
            weight = 0.70 * max(0.0, 1.0 - (hours_ahead - 2.0) / 2.0)
        if drop_ratio < 0.75:
            weight *= max(0.25, drop_ratio)
        if weight <= 0:
            continue

        gti = _gti(start)
        have_gti = anchor_gti > 0 and gti > 0
        if have_gti:
            gti_ratio = max(0.0, min(1.15, gti / anchor_gti))
        elif hours_ahead <= 1.0:
            gti_ratio = 1.0
        else:
            gti_ratio = 0.0

        nowcast = float(anchor["slot_kwh"]) * gti_ratio
        base = max(0.0, float(pv_forecast.get(start, 0.0) or 0.0))
        raising = nowcast > base + 0.01
        # Lower the baseline only on real evidence it's too high: a GTI ratio derived from
        # data, or a confirmed live production drop. Never lower on the missing-GTI fallback
        # (gti_ratio=0), which would zero out slots whenever GTI data is simply absent.
        lowering = nowcast < base - 0.01 and (have_gti or drop_ratio < 0.75)
        if not (raising or lowering):
            continue
        adjusted = base * (1.0 - weight) + nowcast * weight
        out[start] = adjusted
        delta += adjusted - base
        adjusted_slots += 1
        row = slot_context.setdefault(start.isoformat(), {})
        row["pv_nowcast_kwh"] = round(adjusted, 4)
        row["pv_nowcast_weight"] = round(weight, 3)

    summary.update({
        "pv_nowcast_applied": adjusted_slots > 0,
        "pv_nowcast_source": anchor.get("source"),
        "pv_nowcast_anchor_kwh": round(float(anchor["slot_kwh"]), 3),
        "pv_nowcast_live_slot_kwh": (
            round(float(anchor["live_slot_kwh"]), 3)
            if anchor.get("live_slot_kwh") is not None else None
        ),
        "pv_nowcast_recent_slot_kwh": (
            round(float(anchor["recent_slot_kwh"]), 3)
            if anchor.get("recent_slot_kwh") is not None else None
        ),
        "pv_nowcast_drop_ratio": round(drop_ratio, 3),
        "pv_nowcast_delta_kwh": round(delta, 3),
        "pv_nowcast_slots": adjusted_slots,
    })
    if adjusted_slots:
        weather_context["pv_nowcast_forecast"] = out
        weather_context["pv_forecast"] = out
    return out


def _set_grid_assist(enabled: bool) -> None:
    """Track AI grid-assist ("retain") mode in global state.

    We deliberately do NOT publish the legacy ``grid_charging_enabled`` topic
    here: that topic's event handler zeroes the AC setpoint (clobbering the AI's
    export/charge setpoint) and is also tied to Tesla grid-charging logic. The
    setpoint matching for retain mode is instead handled directly via
    ``manage_grid_usage_based_on_current_price`` / ``adjust_grid_setpoint`` while
    ``ai_grid_assist`` is on. Idempotent — logs only on change."""
    desired = "on" if enabled else "off"
    if STATE.get('ai_grid_assist') == desired:
        return

    STATE.set('ai_grid_assist', desired)
    logging.info(f"AI_ESS: Grid-assist (retain) mode {'ENABLED' if enabled else 'disabled'}.")


_SELL_STATE_PATH = '/dev/shm/cerbo_ai_sell_state.json'


def _manual_grid_charge_on() -> bool:
    """Manual override toggle (legacy ``grid_charging_enabled`` topic).

    When on, force RETAIN: hold the battery and let the grid cover ALL house loads
    — including a full-power EV charge — instead of draining the pack. This
    overrides the AI plan; toggling it off hands control back to the optimizer.
    Dual-purpose by design: it is both a "charge the car from the grid now"
    button and a "retain the battery" hold.
    """
    return _is_truthy(STATE.get('grid_charging_enabled'), False)


def _ai_ess_override_on() -> bool:
    """Runtime dashboard override: AI ESS stands down completely while true."""
    return _is_truthy(STATE.get('ai_ess_override_enabled'), False)


def _apply_sell_hysteresis(result):
    """Damp SELL flapping between 15-minute re-plans (minimum dwell + price band).

    The optimizer rebuilds from scratch each cycle, so on a flat price curve it can
    flip SELL<->HOLD on sub-cent noise. This guard only ever *suppresses entering*
    SELL — it never forces or prolongs a discharge — so it is safe for the battery:
    if a SELL is planned but we stopped (or weren't) selling within
    ``ESS_SELL_MIN_DWELL_MIN`` and the price hasn't risen by ``ESS_SELL_HYSTERESIS_EUR``
    since, we hold (RETAIN) instead. Exiting SELL is always allowed immediately.
    """
    import os
    import json
    try:
        want_sell = (result.get('control_action') or '') == 'SELL'
        dwell_min = _get_float_setting('ESS_SELL_MIN_DWELL_MIN', 20.0)
        hyst = _get_float_setting('ESS_SELL_HYSTERESIS_EUR', 0.03)
        price = float(result.get('current_price') or 0.0)
        now = time.time()

        state = {}
        try:
            with open(_SELL_STATE_PATH) as fh:
                state = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        was_sell = bool(state.get('sell_on'))
        last_ts = float(state.get('ts') or 0.0)
        last_price = state.get('price')

        if want_sell and not was_sell:
            mins = (now - last_ts) / 60.0 if last_ts else 1e9
            moved = abs(price - float(last_price)) if last_price is not None else 1e9
            if mins < dwell_min and moved < hyst:
                # Suppress: turn the planned SELL into a hold. Leave the persisted
                # state untouched (we remain "not selling").
                result['control_action'] = 'RETAIN'
                result['grid_assist'] = True
                result['mode'] = 'hold'
                result['setpoint'] = 0.0
                result['reason_code'] = 'SELL_DAMPED_HYSTERESIS'
                result['reason'] = (
                    f"Holding — sell suppressed by hysteresis (price moved "
                    f"€{moved:.3f} < €{hyst:.3f} and only {mins:.0f} < {dwell_min:.0f} min "
                    f"since last sell)"
                )
                logging.info(
                    "AI_ESS: SELL suppressed by hysteresis (Δ€%.3f<%.3f, %.0f<%.0f min).",
                    moved, hyst, mins, dwell_min,
                )
                return result

        # Record a genuine sell<->not-sell transition (with the price/time it
        # happened) so the next cycle can measure dwell + price movement.
        new_sell = (result.get('control_action') or '') == 'SELL'
        if new_sell != was_sell:
            tmp = _SELL_STATE_PATH + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump({'sell_on': new_sell, 'ts': now, 'price': price}, fh)
            os.replace(tmp, _SELL_STATE_PATH)
    except Exception as e:
        logging.warning(f"AI_ESS: sell-hysteresis check failed: {e}")
    return result


def _apply_low_soc_retain_before_cheaper_buy(result, batt_soc):
    """At the configured reserve floor, hold instead of IDLE when a cheaper BUY is planned.

    Victron/BMS may protect an empty pack by importing even though the optimizer's
    IDLE model is neutral. If the next scheduled grid-charge window is cheaper
    than the current slot, make the current command explicit RETAIN so grid-assist
    covers only the PV deficit and defers battery charging to the cheaper BUY.
    """
    if (result.get('control_action') or '') != 'IDLE':
        return result

    try:
        reserve = float(current_min_soc_reserve())
        soc = float(batt_soc)
    except (TypeError, ValueError):
        return result
    if soc > reserve + 1e-6:
        return result

    schedule = result.get('schedule') or []
    next_buy = next(
        (s for s in schedule[1:] if (s.get('control_action') or '') == 'BUY'),
        None,
    )
    if not next_buy:
        return result

    def _price(row, fallback=None):
        try:
            return float(row.get('price'))
        except (AttributeError, TypeError, ValueError):
            return fallback

    current_price = _price(result, _price(schedule[0], None) if schedule else None)
    next_buy_price = _price(next_buy)
    if current_price is None or next_buy_price is None or current_price <= next_buy_price + 1e-6:
        return result

    result['control_action'] = 'RETAIN'
    result['grid_assist'] = True
    result['mode'] = 'hold'
    result['setpoint'] = 0.0
    result['reason_code'] = 'LOW_SOC_DEFER_CHEAPER_BUY'
    result['reason'] = (
        f"At reserve floor ({reserve:.0f}%); holding battery now at €{current_price:.3f}/kWh "
        f"and deferring grid charge to cheaper planned BUY (€{next_buy_price:.3f}/kWh)"
    )
    logging.info(
        "AI_ESS: Low-SoC IDLE converted to RETAIN; current €%.3f > next BUY €%.3f at reserve %.1f%%.",
        current_price, next_buy_price, reserve,
    )
    return result


def _filter_victron_slots_for_grid_charge_cap(victron_slots, batt_soc, now=None):
    """Apply ESS_MAX_GRID_CHARGE_SOC to Victron forced charge windows.

    The optimizer already avoids planning grid-sourced charging above this cap.
    This publication-side guard is defensive and handles live drift: if the pack
    has reached the cap while a charge window is active, the active window is not
    re-published after the normal clear-all-schedules step. Future windows remain,
    since an intervening SELL/self-supply period may lower SoC before they start.
    """
    cap = max(0.0, min(100.0, _get_float_setting('ESS_MAX_GRID_CHARGE_SOC', 100.0)))
    if not victron_slots:
        return []

    try:
        soc = float(batt_soc)
    except (TypeError, ValueError):
        soc = None

    if now is None:
        from datetime import datetime as _dt
        first_start = next((s.get('start') for s in victron_slots if s.get('start') is not None), None)
        tzinfo = getattr(first_start, 'tzinfo', None)
        now = _dt.now(tzinfo)

    def _active(slot):
        from datetime import timedelta as _td
        start = slot.get('start')
        if start is None:
            return False
        try:
            end = start + _td(seconds=float(slot.get('duration') or 0))
        except Exception:
            return False
        try:
            return start <= now < end
        except TypeError:
            try:
                start_naive = start.replace(tzinfo=None)
                now_naive = now.replace(tzinfo=None)
                end_naive = end.replace(tzinfo=None)
                return start_naive <= now_naive < end_naive
            except Exception:
                return False

    filtered = []
    removed_active = 0
    for slot in victron_slots:
        if cap < 100.0 and soc is not None and soc >= cap - 1e-6 and _active(slot):
            removed_active += 1
            continue
        capped = dict(slot)
        if cap < 100.0:
            try:
                capped['target_soc'] = min(int(capped.get('target_soc', 100)), int(round(cap)))
            except (TypeError, ValueError):
                capped['target_soc'] = int(round(cap))
        filtered.append(capped)

    if removed_active:
        logging.info(
            "AI_ESS: Battery SoC %.1f%% reached grid-charge cap %.1f%%; cleared %d active charge slot(s).",
            soc, cap, removed_active,
        )
    return filtered


def _grid_assist_setpoint_watts(load_watts=None, cover_all_load: bool = False) -> int:
    """Grid setpoint (W) for retain mode.

    AI retain imports only the load PV cannot cover. Manual Grid assist imports the
    full AC load so sudden loads (for example EV charging) are covered by grid
    immediately instead of PV/battery.
    """
    if load_watts is None:
        load_watts = STATE.get('ac_out_power')
    if cover_all_load:
        try:
            return max(0, int(round(float(load_watts or 0))))
        except (TypeError, ValueError):
            return 0
    pv_watts = STATE.get('pv_power')
    try:
        net = float(load_watts or 0) - float(pv_watts or 0)
    except (TypeError, ValueError):
        net = 0.0
    return max(0, int(round(net)))


def _apply_grid_assist_setpoint(load_watts=None, deadband_w: int = 50, cover_all_load: bool = False) -> None:
    """Apply the retain-mode grid setpoint (PV-aware), avoiding redundant writes."""
    target = _grid_assist_setpoint_watts(load_watts, cover_all_load=cover_all_load)
    try:
        current_sp = float(STATE.get('ac_power_setpoint') or 0)
    except (TypeError, ValueError):
        current_sp = 0.0

    if target > 0:
        # Import only the PV deficit; skip tiny changes to avoid MQTT churn.
        if abs(target - max(current_sp, 0.0)) >= deadband_w:
            adjust_grid_setpoint(target, override_ess_net_mettering=True)
    else:
        # PV covers the load: don't import. Leave the setpoint at 0 so surplus PV
        # charges the battery / exports when full. Apply the same deadband as the
        # import branch so we don't thrash 0 <-> import as PV flickers around the
        # load level (e.g. at sunrise). silent=True to match the import write
        # above — otherwise only the zero-writes log, spamming the service log.
        if abs(current_sp) >= deadband_w:
            ac_power_setpoint(watts="0.0", override_ess_net_mettering=False, silent=True)


def _grid_assist_control_action(applied_setpoint, manual_grid_assist: bool = False) -> str:
    if manual_grid_assist:
        return 'RETAIN'
    return 'RETAIN' if (applied_setpoint or 0) > 0 else 'IDLE'


# Default relative residential load shape (per clock hour, 0-23). Normalised at
# use; only the relative magnitudes matter. Low overnight, morning + evening peaks.
DEFAULT_LOAD_PROFILE_RELATIVE = [
    0.6, 0.5, 0.5, 0.5, 0.5, 0.7,      # 00-05
    1.1, 1.4, 1.3, 1.0, 0.9, 0.9,      # 06-11
    1.0, 1.0, 0.9, 0.9, 1.1, 1.6,      # 12-17
    1.8, 1.8, 1.6, 1.4, 1.0, 0.7,      # 18-23
]


def _hourly_load_profile() -> list:
    """Return a 24-element load profile normalised to sum to 1.0.

    Override the default shape with LOAD_PROFILE_HOURLY (24 comma-separated
    relative weights) if your home has a known consumption pattern.
    """
    raw = retrieve_setting('LOAD_PROFILE_HOURLY')
    weights = DEFAULT_LOAD_PROFILE_RELATIVE
    if raw:
        try:
            parsed = [float(x) for x in str(raw).split(',')]
            if len(parsed) == 24 and sum(parsed) > 0:
                weights = parsed
        except (TypeError, ValueError):
            logging.warning("EnergyBroker: Invalid LOAD_PROFILE_HOURLY; using default profile.")

    total = sum(weights)
    return [w / total for w in weights]


def _estimate_daily_consumption_kwh() -> float:
    """Estimate today's total house consumption (kWh).

    Prefers the VRM consumption forecast (consumption_total_projected, Wh).
    Falls back to extrapolating today's measured consumption so far, then to the
    configured DAILY_HOME_ENERGY_CONSUMPTION default.
    """
    try:
        projected_kwh = float(STATE.get('consumption_total_projected')) / 1000.0
    except (TypeError, ValueError):
        projected_kwh = 0.0
    if projected_kwh > 0:
        return projected_kwh

    try:
        cum_kwh = float(STATE.get('consumption_total_cumulative')) / 1000.0
    except (TypeError, ValueError):
        cum_kwh = 0.0

    lt = time.localtime()
    elapsed_h = lt.tm_hour + lt.tm_min / 60.0
    if cum_kwh > 0 and elapsed_h >= 1.0:
        return cum_kwh * 24.0 / elapsed_h  # extrapolate today's rate to a full day

    return DAILY_HOME_ENERGY_CONSUMPTION


def _historical_load_by_slot(days: int = 3) -> dict:
    """Average realised house load (kW) per quarter-hour-of-day over the last
    ``days`` days of history, keyed by ``'HH:MM'`` (15-min buckets).

    This is the empirical, per-slot consumption model: e.g. the 06:00 bucket
    reflects what the house actually drew at 06:00 across recent days, so the
    optimizer stops assuming free morning solar covers the load. Samples taken
    during heavy battery activity (|batt_w| > 4 kW) are excluded because the
    AC-out reading is unreliable then. Returns {} when there's no usable history.
    """
    import os
    import json
    from datetime import datetime as _dt, timedelta as _td

    try:
        history_dir = retrieve_setting('HISTORY_DIR') or 'data/history'
        buckets = {}  # "HH:MM" -> [load_kw, ...]
        today = _dt.now().date()
        for i in range(max(1, days)):
            d = today - _td(days=i)
            for r in _hist.read_day(d, history_dir):   # NDJSON hot + Parquet cold
                if r.get('kind') == 'settlement':
                    continue
                load, batt, ts = r.get('load_w'), r.get('batt_w'), r.get('ts')
                if load is None or ts is None:
                    continue
                try:
                    if batt is not None and abs(float(batt)) > 4000:
                        continue
                    when = _dt.fromisoformat(ts)
                    key = f"{when.hour:02d}:{(when.minute // 15) * 15:02d}"
                    buckets.setdefault(key, []).append(float(load) / 1000.0)
                except (TypeError, ValueError):
                    continue
        return {k: sum(v) / len(v) for k, v in buckets.items() if v}
    except Exception as e:
        logging.debug(f"EnergyBroker: historical load read failed: {e}")
        return {}


def _build_load_forecast_by_slot(price_slots: list, slot_duration_h: float) -> dict:
    """Build a per-slot house-load forecast (kWh) keyed by slot start time.

    Primary model: the trailing 3-day average of the *realised* load for that
    slot-of-day (so 06:00 reflects the real morning draw rather than assuming
    solar covers it). Falls back to the diurnal-profile distribution of the
    estimated daily consumption for any slot without recent history.
    """
    from datetime import timedelta as _td
    hist = _historical_load_by_slot(3)
    daily_kwh = _estimate_daily_consumption_kwh()
    profile = _hourly_load_profile()
    out = {}
    for slot in price_slots:
        start = slot['start']
        load_kwh = None
        if hist:
            # Average the 15-min buckets this slot spans (1 for a 15-min slot, 4
            # for an hourly slot), then scale to the slot length.
            n_buckets = max(1, int(round(slot_duration_h * 4)))
            kws = []
            for b in range(n_buckets):
                t = start + _td(minutes=15 * b)
                key = f"{t.hour:02d}:{(t.minute // 15) * 15:02d}"
                if key in hist:
                    kws.append(hist[key])
            if kws:
                load_kwh = (sum(kws) / len(kws)) * slot_duration_h
        if load_kwh is None:
            # Fallback: profile[hour] is the fraction of daily load in that clock
            # hour; scale by the native slot length so sub-slots sum to the hour.
            load_kwh = daily_kwh * profile[start.hour] * slot_duration_h
        out[start] = load_kwh
    return out


def get_today_energy_actuals() -> dict:
    """Return today's accumulated energy actuals from the MQTT bus (retained).

    Used to combine today's measured import cost / export reward with the
    optimizer's remaining-day forecast for a full-day cost summary.
    """
    from lib.helpers import retrieve_message

    def _f(topic):
        try:
            return float(retrieve_message(topic))
        except (TypeError, ValueError):
            return 0.0

    return {
        'imp_kwh': _f("Tibber/home/energy/day/imported"),
        'imp_cost': _f("Tibber/home/energy/day/cost"),
        'exp_kwh': _f("Tibber/home/energy/day/exported"),
        'exp_rev': _f("Tibber/home/energy/day/reward"),
    }


def _publish_plan_json(result, *, batt_soc, price_points, pv_remaining,
                       applied_setpoint, today_actuals) -> None:
    """Write the current plan to a JSON file for the frontend dashboard to read.

    Best-effort and read-only-friendly: any failure is logged and ignored so it
    can never affect ESS control. The path is configurable via AI_PLAN_EXPORT_PATH
    (default /dev/shm/cerbo_ai_plan.json — shared, in-memory, same-host sidecar).
    """
    import json
    from datetime import datetime as _dt

    def _iso(v):
        try:
            return v.isoformat()
        except AttributeError:
            return v

    def _json_safe(v):
        """Convert internal optimizer objects into JSON-safe plan payload values."""
        if isinstance(v, dict):
            out = {}
            for key, val in v.items():
                if isinstance(key, (str, int, float, bool)) or key is None:
                    safe_key = key
                else:
                    safe_key = _iso(key)
                    if safe_key is key:
                        safe_key = str(key)
                out[safe_key] = _json_safe(val)
            return out
        if isinstance(v, (list, tuple)):
            return [_json_safe(x) for x in v]
        if isinstance(v, set):
            return [_json_safe(x) for x in sorted(v, key=str)]
        converted = _iso(v)
        return converted

    try:
        path = retrieve_setting('AI_PLAN_EXPORT_PATH') or '/dev/shm/cerbo_ai_plan.json'

        schedule = [{
            'time': _iso(s['time']),
            'mode': s['action'],
            'control_action': s.get('control_action'),
            'price': s['price'],
            'sell': s.get('sell'),
            'soc_start': s['soc_start'],
            'soc_end': s['soc_end'],
            'grid_energy': s['grid_energy'],
            'pv': s.get('pv'),
            'load': s.get('load'),
            'reason': s.get('reason'),
            'reason_code': s.get('reason_code'),
        } for s in result.get('schedule', [])]

        victron_slots = [{
            'start': _iso(s['start']),
            'duration': s['duration'],
            'target_soc': s['target_soc'],
        } for s in result.get('victron_slots', [])]

        # Daily totals + HA-style derived metrics for the dashboard (computed here
        # where STATE is available; refreshed each optimizer cycle).
        _act = today_actuals or {}

        def _fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        solar_kwh = (_fnum(STATE.get('c1_daily_yield')) or 0.0) + (_fnum(STATE.get('c2_daily_yield')) or 0.0)
        _cons_wh = _fnum(STATE.get('consumption_total_cumulative'))
        cons_kwh = (_cons_wh / 1000.0) if _cons_wh is not None else None
        g_imp = _fnum(_act.get('imp_kwh')) or 0.0
        g_exp = _fnum(_act.get('exp_kwh')) or 0.0
        self_cons_solar = (max(0.0, min(100.0, (solar_kwh - g_exp) / solar_kwh * 100.0))
                           if solar_kwh > 0.01 else None)
        self_suff = (max(0.0, min(100.0, (cons_kwh - g_imp) / cons_kwh * 100.0))
                     if (cons_kwh and cons_kwh > 0.01) else None)
        today_block = {
            'solar_kwh': round(solar_kwh, 2),
            'consumption_kwh': round(cons_kwh, 2) if cons_kwh is not None else None,
            'grid_import_kwh': round(g_imp, 2),
            'grid_export_kwh': round(g_exp, 2),
            'grid_import_cost': _fnum(_act.get('imp_cost')),
            'grid_export_reward': _fnum(_act.get('exp_rev')),
            'net_imported_kwh': round(g_imp - g_exp, 2),
            'self_consumed_solar_pct': round(self_cons_solar) if self_cons_solar is not None else None,
            'self_sufficiency_pct': round(self_suff) if self_suff is not None else None,
            'gas_m3': _fnum(STATE.get('gas_today_m3')),   # from Domoticz (best-effort)
            'ev_kwh': _fnum(STATE.get('ev_today_kwh')),
            'sun_rise': STATE.get('sun_rise') or None,
            'sun_set': STATE.get('sun_set') or None,
        }

        def _adjusted_pv_remaining_wh():
            if not schedule:
                return None
            first_day = str(schedule[0].get('time') or '')[:10]
            if not first_day:
                return None
            total_kwh = 0.0
            seen = False
            for slot in schedule:
                if not str(slot.get('time') or '').startswith(first_day):
                    continue
                pv_kwh = _fnum(slot.get('pv'))
                if pv_kwh is None:
                    continue
                total_kwh += max(0.0, pv_kwh)
                seen = True
            return round(total_kwh * 1000.0, 3) if seen else None

        weather_context = result.get('weather_context') or {}
        weather_summary = weather_context.get('summary') or {}
        pv_remaining_raw_wh = _fnum(pv_remaining)
        pv_adjusted_remaining_wh = _adjusted_pv_remaining_wh()
        pv_adjustment_kwh = None
        if pv_adjusted_remaining_wh is not None and pv_remaining_raw_wh is not None:
            pv_adjustment_kwh = round((pv_adjusted_remaining_wh - pv_remaining_raw_wh) / 1000.0, 3)
        pv_adjusted_source = (
            'optimizer nowcast'
            if weather_summary.get('pv_nowcast_applied')
            else 'optimizer schedule'
            if pv_adjusted_remaining_wh is not None
            else None
        )

        payload = {
            'generated_at': _dt.now().astimezone().isoformat(),
            'battery_soc': batt_soc,
            'price_points': price_points,
            'pv_remaining_wh': pv_remaining,
            'pv_remaining_raw_wh': pv_remaining,
            'pv_remaining_raw_source': 'VRM forecast',
            'pv_adjusted_remaining_wh': pv_adjusted_remaining_wh,
            'pv_adjusted_remaining_source': pv_adjusted_source,
            'pv_adjustment_kwh': pv_adjustment_kwh,
            'pv_today_total_kwh': STATE.get('pv_projected_today'),
            'pv_tomorrow_wh': STATE.get('pv_projected_tomorrow'),
            'slot_duration_h': result.get('slot_duration_h'),
            'current': {
                'mode': result.get('mode'),
                'control_action': result.get('control_action'),
                'reason': result.get('reason'),
                'reason_code': result.get('reason_code'),
                'price': result.get('current_price'),
                'setpoint': result.get('setpoint'),
                'applied_setpoint': applied_setpoint,
                'limit_feed_in': result.get('limit_feed_in'),
            },
            'today_actuals': today_actuals,
            'today': today_block,
            'weather': _json_safe(weather_context),
            'optimizer_guardrails': _json_safe(
                result.get('optimizer_guardrails') or _optimizer_guardrails_snapshot()
            ),
            'planning_policy': _json_safe(result.get('planning_policy')),
            'victron_slots': victron_slots,
            'schedule': schedule,
        }

        tmp = f"{path}.tmp"
        with open(tmp, 'w') as fh:
            json.dump(payload, fh)
        import os
        os.replace(tmp, path)  # atomic publish so the reader never sees a partial file
    except Exception as e:
        logging.warning(f"AI_ESS: Failed to publish plan JSON for frontend: {e}")


def _realized_action(grid_w, batt_w, deadband_w: int = 200) -> str:
    """Canonical action describing what the system is ACTUALLY doing right now, from
    the live power flow (W; + = import/charge, − = export/discharge).

    The history record pairs THIS cycle's decision (``control_action``) with the
    power measured at cycle start — i.e. the steady-state outcome of the PRIOR
    decision. At a transition those legitimately differ (e.g. a new RETAIN decision
    logged while the previous SELL's export is still ramping down, or an IDLE
    re-evaluation while the Victron charge schedule is still topping up). Recording
    the realized action alongside the decision makes that explicit instead of
    looking like a mislabel.
    """
    try:
        g = float(grid_w) if grid_w is not None else 0.0
        b = float(batt_w) if batt_w is not None else 0.0
    except (TypeError, ValueError):
        return "IDLE"
    charging, discharging = b > deadband_w, b < -deadband_w
    importing, exporting = g > deadband_w, g < -deadband_w
    if discharging and exporting:
        return "SELL"
    if charging and importing:
        return "BUY"
    if importing:
        return "RETAIN"          # grid covering load; battery held
    return "IDLE"                # PV-driven / neutral


def _append_history(result, *, batt_soc, applied_setpoint, today_actuals, realized_power=None) -> None:
    """Append one analytics-ready record per optimizer cycle to a per-day NDJSON
    file (one JSON object per line) under HISTORY_DIR.

    The ``mode``/``setpoint`` fields are the decision being applied this cycle;
    the ``*_w`` power fields are the *realized* steady-state power measured at the
    start of the cycle (i.e. the outcome of the previous decision), so plan-vs-
    actual analysis is clean. Best-effort — any failure is logged and ignored so
    it can never affect ESS control.
    """
    import os
    import json
    from datetime import datetime as _dt

    try:
        history_dir = retrieve_setting('HISTORY_DIR') or 'data/history'
        os.makedirs(history_dir, exist_ok=True)

        now = _dt.now().astimezone()
        sched0 = (result.get('schedule') or [{}])[0]
        act = today_actuals or {}
        rp = realized_power or {}

        def _num(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        # Forecast net € over the planned horizon (profit positive), so we can
        # later compare it against the realised net and learn the optimizer's
        # bias. Excludes stored PV surplus (it isn't sold, so it isn't realised
        # revenue) to stay comparable with grid-measured actuals.
        f_imp_cost = f_exp_rev = 0.0
        for s in (result.get('schedule') or []):
            try:
                g = float(s.get('grid_energy') or 0.0)
                b = float(s.get('price') or 0.0)
                sl = float(s.get('sell', b) or b)
            except (TypeError, ValueError):
                continue
            stored = (str(s.get('reason_code', '')).startswith('PV_SURPLUS')
                      and float(s.get('soc_start') or 0.0) < 99.0)
            if stored:
                continue
            if g > 0:
                f_imp_cost += g * b
            elif g < 0:
                f_exp_rev += -g * sl
        plan_horizon_net_eur = round(f_exp_rev - f_imp_cost, 4)

        # Realised net so far today (profit positive) = export reward - import cost.
        _exp_rev = _num(act.get('exp_rev')) or 0.0
        _imp_cost = _num(act.get('imp_cost')) or 0.0
        realized_net_eur = round(_exp_rev - _imp_cost, 4)

        # Actual PV produced so far today (kWh) from the two MPPT daily yields.
        pv_actual_today_kwh = round((_num(STATE.get('c1_daily_yield')) or 0.0)
                                    + (_num(STATE.get('c2_daily_yield')) or 0.0), 3)
        load_actual_today_wh = _num(STATE.get('consumption_total_cumulative'))

        # Midnight rollover guard. The Tibber daily counters reset promptly at 00:00,
        # but the Victron MPPT daily-yield and consumption counters can lag a cycle,
        # carrying yesterday's full-day totals into the first record of the new day.
        # Two deterministic corrections (no effect on control — logging only):
        #   * PV cannot be produced before dawn, so any pre-dawn yield is a stale
        #     counter -> 0.
        #   * If the day's grid import has just reset (~0) while the consumption
        #     counter still shows a full day's accumulation, it's stale -> 0.
        fresh_day = (_num(act.get('imp_kwh')) or 0.0) < 0.1
        if now.hour < PV_DAYLIGHT_START_H and pv_actual_today_kwh > 0.5:
            pv_actual_today_kwh = 0.0
        if fresh_day and (load_actual_today_wh or 0.0) > 2000.0:
            load_actual_today_wh = 0.0

        record = {
            "ts": now.isoformat(),
            "kind": "cycle",
            "soc": batt_soc,
            "control_action": result.get('control_action'),
            # What the system was actually doing (from live flow) when this record
            # was written — equals control_action in steady state, differs across a
            # transition while the prior decision's power is still settling.
            "realized_action": _realized_action(rp.get('grid_w'), rp.get('batt_w')),
            "mode": result.get('mode'),
            "reason_code": result.get('reason_code'),
            "price_buy": result.get('current_price'),
            "price_sell": sched0.get('sell'),
            "applied_setpoint_w": applied_setpoint,
            "limit_feed_in": result.get('limit_feed_in'),
            "min_soc_reserve": current_min_soc_reserve(),
            # Realized power (W) measured at cycle start = outcome of prior decision.
            "grid_w": _num(rp.get('grid_w')),
            "pv_w": _num(rp.get('pv_w')),
            "load_w": _num(rp.get('load_w')),
            "batt_w": _num(rp.get('batt_w')),
            # Forecast context.
            "pv_remaining_wh": STATE.get('pv_projected_remaining'),
            "pv_tomorrow_wh": STATE.get('pv_projected_tomorrow'),
            # Forecast vs actual (for learning VRM/optimizer bias over time).
            "pv_forecast_today_kwh": _num(STATE.get('pv_projected_today')),
            "pv_actual_today_kwh": pv_actual_today_kwh,
            "load_forecast_today_wh": _num(STATE.get('consumption_total_projected')),
            "load_actual_today_wh": load_actual_today_wh,
            "plan_horizon_net_eur": plan_horizon_net_eur,
            "realized_net_eur": realized_net_eur,
            # Running daily actuals (reset by Tibber at midnight).
            "day_import_kwh": act.get('imp_kwh'),
            "day_import_cost": act.get('imp_cost'),
            "day_export_kwh": act.get('exp_kwh'),
            "day_export_reward": act.get('exp_rev'),
        }
        weather_summary = (result.get('weather_context') or {}).get('summary') or {}
        if weather_summary:
            record.update({
                "weather_source": weather_summary.get("source"),
                "weather_fetched_at": weather_summary.get("fetched_at"),
                "weather_hvac_apply": weather_summary.get("hvac_apply"),
                "weather_pv_apply": weather_summary.get("pv_apply"),
                "weather_load_adj_today_kwh": weather_summary.get("load_adj_today_kwh"),
                "weather_max_temp_c": weather_summary.get("max_temp_c"),
                "weather_pv_shadow_abs_delta_kwh": weather_summary.get("pv_shadow_abs_delta_kwh"),
            })

        path = os.path.join(history_dir, f"ess-{now.strftime('%Y-%m-%d')}.ndjson")
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as e:
        logging.warning(f"AI_ESS: Failed to append history record: {e}")


# Snapshot of the previous cycle (prediction + counters) used to settle each slot.
_LAST_SLOT_PATH = '/dev/shm/cerbo_ai_last_slot.json'


def _settle_prior_slot(result, *, batt_soc, today_actuals, now=None) -> None:
    """At each quarter-hour boundary, write one ``kind: "settlement"`` record to
    the same daily NDJSON pairing the prediction we made for the slot that just
    closed with what ACTUALLY happened (derived by diffing the cumulative daily
    counters). Handles the midnight counter reset and service gaps.

    Exactly ONE settlement is emitted per closed 15-min slot: if a second optimizer
    cycle fires within the same slot (a manual replan or the daily price re-optimize),
    it does not write another settlement or advance the snapshot. This keeps the
    summed per-slot ledger reconciled to the cumulative counters — otherwise a
    mid-slot counter jump gets split across sub-intervals or dropped entirely.
    Best-effort — any failure is logged and never affects ESS control.
    """
    import os
    import json
    from datetime import datetime as _dt

    try:
        history_dir = retrieve_setting('HISTORY_DIR') or 'data/history'
        os.makedirs(history_dir, exist_ok=True)
        now = now or _dt.now().astimezone()
        cur_slot = f"{now.strftime('%Y-%m-%d %H')}:{(now.minute // 15) * 15:02d}"
        act = today_actuals or {}

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        cur = {
            'ts': now.isoformat(),
            'day_import_kwh': _f(act.get('imp_kwh')),
            'day_import_cost': _f(act.get('imp_cost')),
            'day_export_kwh': _f(act.get('exp_kwh')),
            'day_export_reward': _f(act.get('exp_rev')),
            'soc': _f(batt_soc),
            'pv_kwh': (_f(STATE.get('c1_daily_yield')) or 0.0) + (_f(STATE.get('c2_daily_yield')) or 0.0),
            # Cumulative actual house consumption (Wh) so the settlement can diff it
            # into a clean per-slot actual load (forecast-vs-actual analysis / charts).
            # Read from STATE here (this function has no cycle-record locals); _diff
            # turns a midnight counter reset into None, same as the other counters.
            'load_actual_wh': _f(STATE.get('consumption_total_cumulative')),
            'slot_key': cur_slot,
        }
        sched0 = (result.get('schedule') or [{}])[0]
        weather_slots = (result.get('weather_context') or {}).get('slots') or {}

        def _slot_weather(slot):
            try:
                key = slot.get('time').isoformat()
            except AttributeError:
                key = slot.get('time')
            return weather_slots.get(key) or {}

        w0 = _slot_weather(sched0)
        cur['prediction'] = {
            'control_action': result.get('control_action'),
            'predicted_grid_kwh': _f(sched0.get('grid_energy')),
            'price_buy': _f(sched0.get('price')),
            'price_sell': _f(sched0.get('sell')),
            # Per-slot PV and load the optimizer forecast for this slot, so each
            # settlement pairs prediction vs actual for PV and consumption too.
            'predicted_pv_kwh': _f(sched0.get('pv')),
            'predicted_load_kwh': _f(sched0.get('load')),
            'temp_forecast_c': _f(w0.get('temp_forecast_c')),
            'gti_forecast_wm2': _f(w0.get('gti_forecast_wm2')),
            'cloud_forecast_pct': _f(w0.get('cloud_forecast_pct')),
            'weather_load_adj_kwh': _f(w0.get('weather_load_adj_kwh')),
            'weather_pv_shadow_kwh': _f(w0.get('weather_pv_shadow_kwh')),
        }

        prev = None
        try:
            with open(_LAST_SLOT_PATH) as fh:
                prev = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            prev = None

        # A second cycle within the SAME slot: leave the slot-start snapshot in place so
        # the eventual settlement diffs the FULL slot, and emit nothing now.
        if prev is not None and prev.get('slot_key') == cur_slot:
            return

        if prev:
            def _diff(key):
                a, b = cur.get(key), prev.get(key)
                if a is None or b is None:
                    return None
                d = a - b
                return d if d >= -1e-6 else None   # negative => midnight reset, unknown

            slot_h = float(result.get('slot_duration_h') or 0.25)
            try:
                gap_s = (now - _dt.fromisoformat(prev['ts'])).total_seconds()
            except Exception:
                gap_s = None
            incomplete = (gap_s is None) or (gap_s > slot_h * 3600 * 1.6)

            imp_kwh, exp_kwh = _diff('day_import_kwh'), _diff('day_export_kwh')
            imp_cost, exp_rev = _diff('day_import_cost'), _diff('day_export_reward')
            pv_kwh = _diff('pv_kwh')
            load_act_wh = _diff('load_actual_wh')   # cumulative Wh delta -> per-slot load
            soc_start, soc_end = prev.get('soc'), cur.get('soc')
            soc_delta = (soc_end - soc_start) if (soc_start is not None and soc_end is not None) else None
            actual_net = (exp_rev - imp_cost) if (exp_rev is not None and imp_cost is not None) else None

            pred = prev.get('prediction') or {}
            pg = pred.get('predicted_grid_kwh')
            pbuy = pred.get('price_buy') or 0.0
            psell = pred.get('price_sell') if pred.get('price_sell') is not None else pbuy
            predicted_net = None
            if pg is not None:
                # +export revenue (pg<0) / -import cost (pg>0)
                predicted_net = (-pg * psell) if pg < 0 else (-pg * pbuy)

            # Update the persisted battery cost-basis from this slot's measured
            # outcome (grid-charge raises it, PV-charge dilutes it). The optimizer
            # reads it next cycle so it won't sell stored energy below cost.
            # Best-effort: never let an accounting error affect ESS control.
            cost_basis_now = None
            try:
                from lib import ess_cost_basis
                if soc_start is not None and soc_end is not None:
                    cb = ess_cost_basis.update_from_slot(
                        soc_start=soc_start,
                        soc_end=soc_end,
                        capacity_kwh=_get_float_setting('BATTERY_CAPACITY_KWH', 45.0),
                        import_kwh=imp_kwh or 0.0,
                        pv_kwh=pv_kwh or 0.0,
                        price_buy=pbuy,
                        charge_efficiency=_get_float_setting('AC_DC_CHARGE_EFFICIENCY', 0.90),
                    )
                    cost_basis_now = cb.get('basis')
            except Exception as e:
                logging.warning(f"AI_ESS: cost-basis update failed: {e}")

            settlement = {
                'ts': now.isoformat(),
                'kind': 'settlement',
                'slot_start': prev.get('ts'),
                'slot_end': now.isoformat(),
                'incomplete': incomplete,
                'predicted_control_action': pred.get('control_action'),
                'predicted_grid_kwh': pg,
                'predicted_net_eur': round(predicted_net, 4) if predicted_net is not None else None,
                'actual_import_kwh': round(imp_kwh, 3) if imp_kwh is not None else None,
                'actual_export_kwh': round(exp_kwh, 3) if exp_kwh is not None else None,
                'actual_cost': round(imp_cost, 4) if imp_cost is not None else None,
                'actual_reward': round(exp_rev, 4) if exp_rev is not None else None,
                'actual_net_eur': round(actual_net, 4) if actual_net is not None else None,
                'actual_pv_kwh': round(pv_kwh, 3) if pv_kwh is not None else None,
                'actual_load_kwh': round(load_act_wh / 1000.0, 3) if load_act_wh is not None else None,
                'predicted_pv_kwh': pred.get('predicted_pv_kwh'),
                'predicted_load_kwh': pred.get('predicted_load_kwh'),
                'temp_forecast_c': pred.get('temp_forecast_c'),
                'gti_forecast_wm2': pred.get('gti_forecast_wm2'),
                'cloud_forecast_pct': pred.get('cloud_forecast_pct'),
                'weather_load_adj_kwh': pred.get('weather_load_adj_kwh'),
                'weather_pv_shadow_kwh': pred.get('weather_pv_shadow_kwh'),
                'soc_start': soc_start,
                'soc_end': soc_end,
                'soc_delta': round(soc_delta, 2) if soc_delta is not None else None,
                'price_buy': pred.get('price_buy'),
                'price_sell': pred.get('price_sell'),
                'cost_basis_eur_per_kwh': round(cost_basis_now, 4) if cost_basis_now is not None else None,
            }
            path = os.path.join(history_dir, f"ess-{now.strftime('%Y-%m-%d')}.ndjson")
            with open(path, "a") as fh:
                fh.write(json.dumps(settlement) + "\n")

        # Persist this cycle's snapshot for the next settlement (atomic).
        tmp = _LAST_SLOT_PATH + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(cur, fh)
        os.replace(tmp, _LAST_SLOT_PATH)
    except Exception as e:
        logging.warning(f"AI_ESS: Failed to settle prior slot: {e}")


def run_ai_optimizer():
    """Run the optimizer as a single writer; skip overlapping scheduler/UI calls."""
    if not _AI_OPTIMIZER_LOCK.acquire(blocking=False):
        logging.info("AI_ESS: Optimization already running; skipping overlapping request.")
        return False
    try:
        _run_ai_optimizer_once()
        return True
    finally:
        _AI_OPTIMIZER_LOCK.release()


def _run_ai_optimizer_once():
    """
    Runs the AI optimizer if enabled and applies the resulting plan to the
    Victron system (charge schedule, AC setpoint, grid-assist/retain, and
    negative-price feed-in protection).
    """
    if not _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        return
    if _ai_ess_override_on():
        if STATE.get('ai_grid_assist') == 'on':
            STATE.set('ai_grid_assist', 'off')
        logging.info("AI_ESS: Override active; optimizer standing down.")
        return

    try:
        # 1. Retrieve data
        batt_soc = STATE.get('batt_soc')
        # STATE.get() returns 0 for both a missing key and a real 0%, so require
        # the SoC key itself to exist before treating 0 as a valid battery reading.
        soc_reporting = STATE.has('batt_soc') if hasattr(STATE, 'has') else batt_soc is not None
        if not soc_reporting or batt_soc is None:
            logging.warning("AI_ESS: Battery SoC not available yet. Skipping optimization.")
            return

        # Snapshot the realized power NOW, before we apply this cycle's setpoint,
        # so the history record reflects the steady-state outcome of the prior
        # decision (not a just-applied/transient value).
        realized_power = {
            'grid_w': STATE.get('ac_in_power'),
            'pv_w': STATE.get('pv_power'),
            'load_w': STATE.get('ac_out_power'),
            'batt_w': STATE.get('batt_power'),
        }

        prices = get_all_price_points()
        if not prices:
            logging.warning("AI_ESS: No prices available.")
            return

        # 2. Build forecasts from available system data.
        # Normalise price slot starts for the PV forecast distribution.
        from lib.ai_powered_ess import _coerce_datetime
        normalised_slots = []
        for p in prices:
            try:
                normalised_slots.append({'start': _coerce_datetime(p['start'])})
            except (KeyError, TypeError, ValueError):
                continue
        slot_duration_h = 1.0
        if len(normalised_slots) > 1:
            normalised_slots.sort(key=lambda x: x['start'])
            gaps = [
                (normalised_slots[i]['start'] - normalised_slots[i - 1]['start']).total_seconds()
                for i in range(1, len(normalised_slots))
            ]
            positive_gaps = [g for g in gaps if g > 0]
            if positive_gaps:
                slot_duration_h = min(positive_gaps) / 3600.0

        forecast_slots = _forecast_slots_for_optimizer(normalised_slots, slot_duration_h)
        pv_forecast = _build_pv_forecast_by_slot(forecast_slots, slot_duration_h)
        # Self-consumption: forecast house load per slot from VRM consumption
        # data shaped by a diurnal profile, so SoC predictions reflect real usage.
        load_forecast = _build_load_forecast_by_slot(forecast_slots, slot_duration_h)
        weather_context = {"available": False, "summary": {}, "slots": {}}
        try:
            from lib.weather import weather_context_for_slots
            weather_context = weather_context_for_slots(
                forecast_slots,
                slot_duration_h,
                load_forecast,
                pv_forecast,
            )
            if weather_context.get('available'):
                load_forecast = weather_context.get('load_forecast') or load_forecast
                pv_forecast = weather_context.get('pv_forecast') or pv_forecast
                _log_weather_context_once(weather_context.get('summary') or {})
        except Exception as e:
            logging.warning("Weather: shadow forecast skipped: %s", e)

        pv_forecast = _apply_pv_nowcast(
            pv_forecast,
            forecast_slots,
            weather_context,
            slot_duration_h,
        )

        # 3. Optimize
        result = optimize_schedule(batt_soc, prices, load_forecast, pv_forecast)
        if not result:
            logging.warning("AI_ESS: Optimization failed or returned nothing.")
            return
        if weather_context:
            result['weather_context'] = weather_context

        # 4. Negative-price grid feed-in protection.
        # When the current price is negative, exporting costs money, so limit
        # system feed-in to 0W. Auto-revert to unlimited otherwise.
        if _is_truthy(retrieve_setting('NEGATIVE_PRICE_FEED_IN_LIMIT_ENABLED'), True):
            if result.get('limit_feed_in'):
                limit_grid_feed_in(enabled=True, watts=0)
            else:
                limit_grid_feed_in(enabled=False)

        # Keep the Victron hardware MinimumSocLimit in sync with the seasonal
        # reserve (single source of truth). Idempotent — only writes on change.
        set_minimum_ess_soc()

        # 4b. Manual override wins over the plan; otherwise damp SELL flapping.
        # The manual grid-charge toggle forces a retain hold (grid covers all
        # loads incl. a full-power EV charge). When it's off, suppress jittery
        # SELL<->HOLD flips that the stateless re-plan would otherwise produce.
        if _manual_grid_charge_on():
            result['control_action'] = 'RETAIN'
            result['grid_assist'] = True
            result['mode'] = 'hold'
            result['setpoint'] = 0.0
            result['reason_code'] = 'MANUAL_GRID_CHARGE'
            result['reason'] = (
                "Manual grid-charge override active: holding the battery; grid "
                "covers all loads (including full-power EV charging)"
            )
        else:
            result = _apply_sell_hysteresis(result)
            result = _apply_low_soc_retain_before_cheaper_buy(result, batt_soc)

        # 5. Apply immediate control for the current slot.
        setpoint = result.get('setpoint', 0.0)
        if result.get('grid_assist'):  # HOLD (retain)
            # Cover the PV-deficit portion of the house load from the grid so the
            # battery is held; when PV covers the load, stay at 0 so surplus PV
            # charges the battery / exports. Applied immediately here and
            # maintained on ac_out_power events via manage_grid_usage_based_on_current_price.
            _set_grid_assist(True)
            manual_grid_assist = _manual_grid_charge_on()
            _apply_grid_assist_setpoint(cover_all_load=manual_grid_assist)
            applied_setpoint = _grid_assist_setpoint_watts(cover_all_load=manual_grid_assist)
            # Manual Grid assist is an explicit RETAIN lock even if the current
            # load is momentarily near zero. AI retain only labels RETAIN when it
            # actually has to import a PV deficit.
            applied_control_action = _grid_assist_control_action(applied_setpoint, manual_grid_assist)
        else:
            # Ensure HOLD is off, then apply the planned setpoint
            # (export for SELL, 0W for BUY/IDLE).
            _set_grid_assist(False)
            ac_power_setpoint(watts=str(setpoint), override_ess_net_mettering=False, silent=True)
            applied_setpoint = setpoint
            # BUY / SELL / IDLE is unaffected by live PV, so the planned label holds.
            applied_control_action = result.get('control_action') or 'IDLE'

        # Make the published result reflect what we ACTUALLY applied this cycle.
        result['control_action'] = applied_control_action

        # Keep the published schedule's CURRENT slot consistent with what we actually
        # applied. When a planned SELL is suppressed by hysteresis, or a manual
        # override forces RETAIN, the top-level action changes but schedule[0] still
        # held the original DP action — so the Schedule tab showed an action (and a
        # phantom profit) we never took. Sync slot 0 to reality (future slots are
        # projections that re-plan next cycle).
        sched = result.get('schedule') or []
        if sched and sched[0].get('control_action') != applied_control_action:
            s0 = sched[0]
            slot_h = result.get('slot_duration_h') or 0.25
            s0['control_action'] = applied_control_action
            s0['reason_code'] = result.get('reason_code')
            s0['reason'] = result.get('reason')
            s0['mode'] = result.get('mode')
            if applied_control_action in ('RETAIN', 'IDLE'):
                # Battery held — no discharge. Reflect the applied grid setpoint
                # (import to cover load, or ~0 when PV covers it) and a flat SoC.
                s0['soc_end'] = s0.get('soc_start')
                s0['grid_energy'] = round((applied_setpoint or 0) / 1000.0 * slot_h, 4)

        # Publish the current action/reason for dashboards and automation.
        STATE.set('ai_control_action', applied_control_action)
        STATE.set('ai_mode', result.get('mode'))
        STATE.set('ai_reason', result.get('reason'))
        STATE.set('ai_reason_code', result.get('reason_code'))

        # 6. Program the Victron grid-charge schedule slots.
        victron_slots = _filter_victron_slots_for_grid_charge_cap(
            result.get('victron_slots', []),
            batt_soc,
        )
        result['victron_slots'] = victron_slots
        # Clear-then-program leaves a brief empty-slot window; missing a grid charge
        # is the safe failure mode, and the next optimizer cycle self-heals it.
        clear_victron_schedules()

        for i, slot in enumerate(victron_slots):
            if i >= 5:
                break
            start_dt = slot['start']
            seconds_from_midnight = start_dt.hour * 3600 + start_dt.minute * 60
            weekday = PythonToVictronWeekdayNumberConversion[start_dt.weekday()]
            target_soc = int(slot.get('target_soc', 100))
            topic_stub = f"W/{systemId0}/settings/0/Settings/CGwacs/BatteryLife/Schedule/Charge/{i}/"

            publish_message(f"{topic_stub}Duration", payload=f"{{\"value\": {slot['duration']}}}", retain=True)
            publish_message(f"{topic_stub}Soc", payload=f"{{\"value\": {target_soc}}}", retain=True)
            publish_message(f"{topic_stub}Start", payload=f"{{\"value\": {seconds_from_midnight}}}", retain=True)
            publish_message(f"{topic_stub}Day", payload=f"{{\"value\": {weekday}}}", retain=True)

            logging.debug(
                f"AI_ESS: Scheduled charge slot {i}: weekday={weekday} at {start_dt.strftime('%H:%M')} "
                f"for {slot['duration']}s to {target_soc}% SoC"
            )

        # Snapshot today's actuals once, reused by history + plan publish.
        pv_remaining = STATE.get('pv_projected_remaining')
        today_actuals = get_today_energy_actuals()

        # Append an analytics-ready history record for this cycle (best-effort).
        _append_history(result, batt_soc=batt_soc, applied_setpoint=applied_setpoint,
                        today_actuals=today_actuals, realized_power=realized_power)

        # Settle the slot that just closed (predicted vs actual) for accuracy
        # learning + the future timeline view. Best-effort.
        _settle_prior_slot(result, batt_soc=batt_soc, today_actuals=today_actuals)

        # Publish the plan as JSON for the frontend dashboard (best-effort).
        _publish_plan_json(
            result,
            batt_soc=batt_soc,
            price_points=len(prices),
            pv_remaining=pv_remaining,
            applied_setpoint=applied_setpoint,
            today_actuals=today_actuals,
        )

        # The full plan view is available via the web UI and scripts/ai_ess_dryrun.py,
        # so we keep the service log clean with a one-line summary instead of the
        # multi-line plan table.
        charge_slot_note = ". Victron charge slots scheduled." if victron_slots else ""
        logging.info(
            "AI_ESS: Optimization complete — action=%s setpoint=%sW SoC=%.0f%% price=%.3f%s",
            result.get('control_action'), applied_setpoint,
            (batt_soc if batt_soc is not None else float('nan')),
            (result.get('current_price') or 0.0),
            charge_slot_note,
        )

        # Record success for the legacy-fallback health check.
        STATE.set('ai_success_timestamp', time.time())

    except Exception as e:
        logging.error(f"AI_ESS: Error in run_ai_optimizer: {e}", exc_info=True)

class Utils:
    @staticmethod
    def set_inverter_mode(mode: int):
        """
        :param mode: 3 = normal mode. inverter on, batteries will be discharged if PV is not sufficient
                     1 = charger only mode - inverter will not switch on, batteries will not be discharged
        """
        mode_name = {1: "Charging Only Mode", 3: "Normal Inverter Mode"}
        topic = f"W/{systemId0}/vebus/276/Mode"  # TODO: move to constants.py

        if mode and mode == 1 or mode == 3:
            publish.single(topic, payload=f"{{\"value\": {mode}}}", qos=1, retain=False, hostname=cerboGxEndpoint, port=1883)
            logging.info(f"EnergyBroker.Utils.set_inverter_mode: {__name__} has set Multiplus-II's mode to {mode_name.get(mode)}")
        else:
            logging.info(f"EnergyBroker.Utils.set_inverter_mode: {__name__} Error setting mode to {mode_name.get(mode)}. This is not a valid mode.")
