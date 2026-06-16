import time
import schedule as scheduler

from paho.mqtt import publish
from lib.config_retrieval import retrieve_setting
from lib.constants import cerboGxEndpoint, systemId0
from lib.constants import logging, PythonToVictronWeekdayNumberConversion
from lib.helpers import get_seasonally_adjusted_max_charge_slots, calculate_max_discharge_slots_needed, publish_message, round_up_to_nearest_10, remove_message, current_min_soc_reserve
from lib.tibber_api import lowest_48h_prices, lowest_24h_prices
from lib.notifications import pushover_notification
from lib.tibber_api import publish_pricing_data, get_all_price_points
from lib.global_state import GlobalStateClient
from lib.victron_integration import ac_power_setpoint, limit_grid_feed_in, set_minimum_ess_soc
from lib.ai_powered_ess import optimize_schedule

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


# Module configuration parsed safely so a missing/blank setting can never crash
# the import of this module (which controls critical power infrastructure).
MAX_TIBBER_BUY_PRICE = _get_float_setting('MAX_TIBBER_BUY_PRICE', 0.20)
ESS_EXPORT_AC_SETPOINT = _get_float_setting('ESS_EXPORT_AC_SETPOINT', -10000.0)
DAILY_HOME_ENERGY_CONSUMPTION = _get_float_setting('DAILY_HOME_ENERGY_CONSUMPTION', 12.0)


def _is_truthy(value: str | None, default: bool) -> bool:
    if value in (None, "", "None"):
        return default

    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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
            if ac_setpoint < 0.0:
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
        if STATE.get('ai_grid_assist') == 'on':
            # Retain mode: import only what PV can't cover; stay at 0 when PV
            # covers the load so surplus PV charges the battery / exports.
            _apply_grid_assist_setpoint(power)
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
    for i in range(0, 5):
        day = -1
        topic_stub = f"W/{systemId0}/settings/0/Settings/CGwacs/BatteryLife/Schedule/Charge/{i}/"
        publish_message(f"{topic_stub}Day", payload=f"{{\"value\": {day}}}", retain=False)

def push_notification(hour, day, price):
    topic = f"Energy Broker Alert"
    msg = f"ESS Charge scheduled for {hour}:00 {'Today' if day == 0 else 'Tomorrow'} @ {price}"
    pushover_notification(topic, msg)

def run_daily_price_update_and_optimize():
    """Refresh Tibber pricing (so the just-published next-day prices are
    available) and immediately re-run the optimizer over the full 48h horizon.

    Scheduled for 13:05 local time, shortly after Tibber publishes day-ahead
    prices for tomorrow."""
    retrieve_latest_tibber_pricing()
    logging.info("EnergyBroker: 13:05 next-day pricing refresh complete; running optimizer over 48h horizon.")
    run_ai_optimizer()


def _build_pv_forecast_by_slot(price_slots: list, slot_duration_h: float) -> dict:
    """Build a per-slot PV generation forecast (kWh) keyed by slot start time.

    Distributes the VRM solar-yield forecast across the daylight slots of each
    day so day-1 AND day-2 buy/charge decisions account for expected solar:
      * today    -> STATE['pv_projected_remaining'] (Wh remaining today)
      * tomorrow -> STATE['pv_projected_tomorrow']  (Wh forecast full day)
    Spread evenly across a daylight window (a reasonable first-order shape; a
    finer hourly curve can replace this later).
    """
    from datetime import date as _date, timedelta as _td

    def _kwh(key):
        try:
            return max(0.0, float(STATE.get(key)) / 1000.0)
        except (TypeError, ValueError):
            return 0.0

    today_kwh = _kwh('pv_projected_remaining')
    tomorrow_kwh = _kwh('pv_projected_tomorrow')

    today = _date.today()
    tomorrow = today + _td(days=1)
    daylight_start_h, daylight_end_h = 6, 22

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

    out = {}
    if today_kwh > 0 and today_slots:
        per = today_kwh / len(today_slots)
        for s in today_slots:
            out[s] = per
    if tomorrow_kwh > 0 and tomorrow_slots:
        per = tomorrow_kwh / len(tomorrow_slots)
        for s in tomorrow_slots:
            out[s] = per
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


def _grid_assist_setpoint_watts(load_watts=None) -> int:
    """Grid setpoint (W) for retain mode: import only the load the PV cannot cover.

    Returns max(0, house_load - PV). A value of 0 means "do not import" — PV is
    already covering the load, so we leave the grid setpoint at 0 and let excess
    PV charge the battery (and export when the battery is full) rather than pulling
    from the grid while solar is available.
    """
    if load_watts is None:
        load_watts = STATE.get('ac_out_power')
    pv_watts = STATE.get('pv_power')
    try:
        net = float(load_watts or 0) - float(pv_watts or 0)
    except (TypeError, ValueError):
        net = 0.0
    return max(0, int(round(net)))


def _apply_grid_assist_setpoint(load_watts=None, deadband_w: int = 50) -> None:
    """Apply the retain-mode grid setpoint (PV-aware), avoiding redundant writes."""
    target = _grid_assist_setpoint_watts(load_watts)
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
        # charges the battery / exports when full. Only write if not already 0.
        if current_sp != 0:
            ac_power_setpoint(watts="0.0", override_ess_net_mettering=False)


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


def _build_load_forecast_by_slot(price_slots: list, slot_duration_h: float) -> dict:
    """Build a per-slot house-load forecast (kWh) keyed by slot start time.

    Distributes the estimated daily consumption across slots using a diurnal
    profile, so the optimizer accounts for self-usage (notably the evening peak)
    and does not over-estimate how much stored energy is available to sell.
    """
    daily_kwh = _estimate_daily_consumption_kwh()
    profile = _hourly_load_profile()
    out = {}
    for slot in price_slots:
        start = slot['start']
        # profile[hour] is the fraction of the daily load in that clock hour;
        # scale by the native slot length so sub-slots sum back to the hour.
        out[start] = daily_kwh * profile[start.hour] * slot_duration_h
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

    try:
        path = retrieve_setting('AI_PLAN_EXPORT_PATH') or '/dev/shm/cerbo_ai_plan.json'

        schedule = [{
            'time': _iso(s['time']),
            'mode': s['action'],
            'price': s['price'],
            'sell': s.get('sell'),
            'soc_start': s['soc_start'],
            'soc_end': s['soc_end'],
            'grid_energy': s['grid_energy'],
            'reason': s.get('reason'),
            'reason_code': s.get('reason_code'),
        } for s in result.get('schedule', [])]

        victron_slots = [{
            'start': _iso(s['start']),
            'duration': s['duration'],
            'target_soc': s['target_soc'],
        } for s in result.get('victron_slots', [])]

        payload = {
            'generated_at': _dt.now().astimezone().isoformat(),
            'battery_soc': batt_soc,
            'price_points': price_points,
            'pv_remaining_wh': pv_remaining,
            'pv_tomorrow_wh': STATE.get('pv_projected_tomorrow'),
            'slot_duration_h': result.get('slot_duration_h'),
            'current': {
                'mode': result.get('mode'),
                'reason': result.get('reason'),
                'reason_code': result.get('reason_code'),
                'price': result.get('current_price'),
                'setpoint': result.get('setpoint'),
                'applied_setpoint': applied_setpoint,
                'limit_feed_in': result.get('limit_feed_in'),
            },
            'today_actuals': today_actuals,
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

        record = {
            "ts": now.isoformat(),
            "soc": batt_soc,
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
            "load_actual_today_wh": _num(STATE.get('consumption_total_cumulative')),
            "plan_horizon_net_eur": plan_horizon_net_eur,
            "realized_net_eur": realized_net_eur,
            # Running daily actuals (reset by Tibber at midnight).
            "day_import_kwh": act.get('imp_kwh'),
            "day_import_cost": act.get('imp_cost'),
            "day_export_kwh": act.get('exp_kwh'),
            "day_export_reward": act.get('exp_rev'),
        }

        path = os.path.join(history_dir, f"ess-{now.strftime('%Y-%m-%d')}.ndjson")
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as e:
        logging.warning(f"AI_ESS: Failed to append history record: {e}")


def run_ai_optimizer():
    """
    Runs the AI optimizer if enabled and applies the resulting plan to the
    Victron system (charge schedule, AC setpoint, grid-assist/retain, and
    negative-price feed-in protection).
    """
    if not _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        return

    try:
        # 1. Retrieve data
        batt_soc = STATE.get('batt_soc')
        # NOTE: STATE.get() returns 0 for BOTH a missing key and a real 0%. With a
        # 0% summer reserve the battery can legitimately sit at 0%, so use battery
        # voltage as the "is the battery actually reporting" signal — only skip when
        # there's genuinely no battery data, not when SoC is a valid 0%.
        battery_reporting = bool(STATE.get('batt_voltage'))
        if batt_soc is None or (batt_soc == 0 and not battery_reporting):
            logging.warning("AI_ESS: Battery data not available (no SoC/voltage). Skipping optimization.")
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

        pv_forecast = _build_pv_forecast_by_slot(normalised_slots, slot_duration_h)
        # Self-consumption: forecast house load per slot from VRM consumption
        # data shaped by a diurnal profile, so SoC predictions reflect real usage.
        load_forecast = _build_load_forecast_by_slot(normalised_slots, slot_duration_h)

        # 3. Optimize
        result = optimize_schedule(batt_soc, prices, load_forecast, pv_forecast)
        if not result:
            logging.warning("AI_ESS: Optimization failed or returned nothing.")
            return

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

        # Publish the current mode/reason for dashboards and automation.
        STATE.set('ai_mode', result.get('mode'))
        STATE.set('ai_reason', result.get('reason'))
        STATE.set('ai_reason_code', result.get('reason_code'))

        # 5. Apply immediate control for the current slot.
        setpoint = result.get('setpoint', 0.0)
        if result.get('grid_assist'):  # HOLD (retain)
            # Cover the PV-deficit portion of the house load from the grid so the
            # battery is held; when PV covers the load, stay at 0 so surplus PV
            # charges the battery / exports. Applied immediately here and
            # maintained on ac_out_power events via manage_grid_usage_based_on_current_price.
            _set_grid_assist(True)
            _apply_grid_assist_setpoint()
            applied_setpoint = _grid_assist_setpoint_watts()
        else:
            # Ensure HOLD is off, then apply the planned setpoint
            # (export for SELL, 0W for BUY/SELF-SUPPLY).
            _set_grid_assist(False)
            ac_power_setpoint(watts=str(setpoint), override_ess_net_mettering=False)
            applied_setpoint = setpoint

        # 6. Program the Victron grid-charge schedule slots.
        victron_slots = result.get('victron_slots', [])
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

            logging.info(
                f"AI_ESS: Scheduled charge slot {i}: weekday={weekday} at {start_dt.strftime('%H:%M')} "
                f"for {slot['duration']}s to {target_soc}% SoC"
            )

        # Snapshot today's actuals once, reused by history + plan publish.
        pv_remaining = STATE.get('pv_projected_remaining')
        today_actuals = get_today_energy_actuals()

        # Append an analytics-ready history record for this cycle (best-effort).
        _append_history(result, batt_soc=batt_soc, applied_setpoint=applied_setpoint,
                        today_actuals=today_actuals, realized_power=realized_power)

        # Publish the plan as JSON for the frontend dashboard (best-effort).
        _publish_plan_json(
            result,
            batt_soc=batt_soc,
            price_points=len(prices),
            pv_remaining=pv_remaining,
            applied_setpoint=applied_setpoint,
            today_actuals=today_actuals,
        )

        # Log the full plan (same view as scripts/ai_ess_dryrun.py) so the
        # service log shows the active plan and how it changes over time.
        from lib.ai_powered_ess import format_plan_summary
        logging.info(
            "AI_ESS: Optimization complete.\n%s",
            format_plan_summary(
                result,
                batt_soc=batt_soc,
                source="live STATE",
                price_points=len(prices),
                pv_remaining=pv_remaining,
                max_hours=12,
                today_actuals=today_actuals,
                applied_setpoint=applied_setpoint,
            ),
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
