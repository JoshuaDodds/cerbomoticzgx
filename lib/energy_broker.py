import time
import schedule as scheduler

from paho.mqtt import publish
from lib.config_retrieval import retrieve_setting
from lib.constants import cerboGxEndpoint, systemId0
from lib.constants import logging, PythonToVictronWeekdayNumberConversion
from lib.helpers import get_seasonally_adjusted_max_charge_slots, calculate_max_discharge_slots_needed, publish_message, round_up_to_nearest_10, remove_message
from lib.tibber_api import lowest_48h_prices, lowest_24h_prices
from lib.notifications import pushover_notification
from lib.tibber_api import publish_pricing_data, get_all_price_points
from lib.global_state import GlobalStateClient
from lib.victron_integration import ac_power_setpoint
from lib.ai_powered_ess import optimize_schedule

MAX_TIBBER_BUY_PRICE = float(retrieve_setting('MAX_TIBBER_BUY_PRICE')) or 0.20
ESS_EXPORT_AC_SETPOINT = float(retrieve_setting('ESS_EXPORT_AC_SETPOINT')) or -10000.0
DAILY_HOME_ENERGY_CONSUMPTION = float(retrieve_setting('DAILY_HOME_ENERGY_CONSUMPTION')) or 12.0

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

def main():
    logging.info("EnergyBroker: Initializing...")
    schedule_tasks()
    logging.info("EnergyBroker: Initialization complete.")


def schedule_tasks():
    # ESS Scheduled Tasks
    scheduler.every().hour.at(":00").do(manage_sale_of_stored_energy_to_the_grid)

    # AI Optimization Loop (every 15 mins)
    scheduler.every(15).minutes.do(run_ai_optimizer)

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
    # Check if AI algorithm is enabled and healthy
    if _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        last_ai_success = STATE.get('ai_success_timestamp')
        if last_ai_success and (time.time() - float(last_ai_success) < 3600):
            logging.info("EnergyBroker: AI Optimizer is active and healthy. Skipping legacy manage_sale logic.")
            return
        else:
             logging.warning("EnergyBroker: AI Optimizer is enabled but seems unhealthy (no recent success). Falling back to legacy logic.")

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
    # Check if AI algorithm is enabled and healthy
    if _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        last_ai_success = STATE.get('ai_success_timestamp')
        if last_ai_success and (time.time() - float(last_ai_success) < 3600):
            logging.info(f"EnergyBroker: AI Algorithm active. Skipping legacy charging schedule request from {caller}.")
            return True
        else:
             logging.warning("EnergyBroker: AI Optimizer enabled but stale. Running legacy set_charging_schedule as fallback.")

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

def run_ai_optimizer():
    """
    Runs the AI optimizer if enabled.
    """
    if not _is_truthy(retrieve_setting('AI_POWERED_ESS_ALGORITHM'), False):
        return

    try:
        # 1. Retrieve data
        batt_soc = STATE.get('batt_soc')
        if batt_soc is None:
             logging.warning("AI_ESS: Battery SoC not available. Skipping.")
             return

        prices = get_all_price_points()

        if not prices:
            logging.warning("AI_ESS: No prices available.")
            return

        # Get forecasts (dummy for now, or from STATE if available)
        # TODO: Retrieve actual forecasts from solar_forecasting module if available
        load_forecast = None # Default to avg in optimizer
        pv_forecast = None # Default to 0 or avg

        # 2. Optimize
        result = optimize_schedule(batt_soc, prices, load_forecast, pv_forecast)

        if not result:
            logging.warning("AI_ESS: Optimization failed or returned nothing.")
            return

        # 3. Apply results

        # Setpoint
        setpoint = result.get('setpoint', 0.0)

        ac_power_setpoint(watts=str(setpoint), override_ess_net_mettering=False)

        # Schedule Victron Slots
        victron_slots = result.get('victron_slots', [])
        clear_victron_schedules()

        for i, slot in enumerate(victron_slots):
            if i >= 5: break
            start_dt = slot['start']

            seconds_from_midnight = start_dt.hour * 3600 + start_dt.minute * 60

            weekday = PythonToVictronWeekdayNumberConversion[start_dt.weekday()]

            topic_stub = f"W/{systemId0}/settings/0/Settings/CGwacs/BatteryLife/Schedule/Charge/{i}/"

            publish_message(f"{topic_stub}Duration", payload=f"{{\"value\": {slot['duration']}}}", retain=True)
            publish_message(f"{topic_stub}Soc", payload=f"{{\"value\": 100}}", retain=True) # Target SoC 100 for charging slot
            publish_message(f"{topic_stub}Start", payload=f"{{\"value\": {seconds_from_midnight}}}", retain=True)
            publish_message(f"{topic_stub}Day", payload=f"{{\"value\": {weekday}}}", retain=True)

            logging.info(f"AI_ESS: Scheduled charge slot {i}: {weekday} at {start_dt.strftime('%H:%M')} for {slot['duration']}s")

        logging.info(f"AI_ESS: Optimization complete. Setpoint: {setpoint}W. Scheduled {len(victron_slots)} charge slots.")

        # Record success
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
