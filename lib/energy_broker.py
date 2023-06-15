import time
import threading
import schedule as scheduler

import paho.mqtt.publish as publish

from lib.constants import logging, cerboGxEndpoint, systemId0, PythonToVictronWeekdayNumberConversion, dotenv_config
from lib.helpers import get_seasonal_max_items
from lib.tibber_api import lowest_48h_prices
from lib.notifications import pushover_notification
from lib.tibber_api import publish_pricing_data
from lib.global_state import GlobalStateClient
from lib.victron_integration import ac_power_setpoint


MAX_TIBBER_BUY_PRICE = float(dotenv_config('MAX_TIBBER_BUY_PRICE')) or None
STATE = GlobalStateClient()


def main():
    logging.info("EnergyBroker: Initializing...")

    main_thread = threading.Thread(target=scheduler_loop)
    main_thread.daemon = True
    main_thread.start()

    logging.info("EnergyBroker: Started.")

def scheduler_loop():
    def is_alive():
        logging.info(f"EnergyBroker: heartbeat...thumpThump!")

        # updating the hourly price is normally triggered on batter_soc changes.  Since this will not happen when
        # battery is full and idle, we check for that condition here and still update the hourly price at 5 minute
        # intervals.
        if dotenv_config('TIBBER_UPDATES_ENABLED') == '1' and STATE.get('batt_soc') == 100.0:
            publish_pricing_data(__name__)

    # scheduler.every().day.at("09:30").do(publish_mqtt_trigger)
    scheduler.every().day.at("14:00").do(publish_mqtt_trigger)
    scheduler.every(5).minutes.do(is_alive)

    for job in scheduler.get_jobs():
        logging.info(f"EnergyBroker: job: {job}")

    while True:
        scheduler.run_pending()
        time.sleep(1)

def manage_sale_of_stored_energy_to_the_grid(batt_soc: float) -> None:
    tibber_price_now = STATE.get('tibber_price_now')
    tibber_24h_high = STATE.get('tibber_cost_highest_today')
    ac_setpoint = STATE.get('ac_power_setpoint')
    ess_net_metering = STATE.get('ess_net_metering_enabled')

    if batt_soc > 70.0 and tibber_price_now >= tibber_24h_high and tibber_price_now != 0 and ess_net_metering:
        if ac_setpoint != -10000.0:
            ac_power_setpoint(watts="-10000.0")
            logging.info(f"Beginning to sell energy at {batt_soc}% SOC and a price of {round(tibber_price_now, 3)}")
            pushover_notification("Energy Sale Alert",
                                  f"Beginning to sell energy at a cost of {round(tibber_price_now, 3)}")
    else:
        if ac_setpoint < 0.0:
            ac_power_setpoint(watts="0.0")
            logging.info(f"Stopped energy export at {batt_soc}% SOC and a current price of {round(tibber_price_now, 3)}")
            pushover_notification("Energy Sale Alert",
                                  f"Stopped energy export at {batt_soc} and a current price of {round(tibber_price_now, 3)}")


def manage_grid_usage_based_on_current_price(price: float) -> None:
    inverter_mode = int(STATE.get("inverter_mode"))

    # if energy is free or the provider is paying, switch to using the grid and start vehicle charging
    if price <= 0.0001 and inverter_mode == 3:
        pushover_notification("Tibber Price Alert",
                              f"Energy cost is {round(price, 3)} cents per kWh. Switching to grid energy.")
        Utils.set_inverter_mode(mode=1)
        return

    # revese the above action when energy is no longer free
    if price >= 0.0001 and inverter_mode == 1:
        print(inverter_mode)
        pushover_notification("Tibber Price Alert",
                              f"Energy cost is {round(price, 3)} cents per kWh. Switching back to battery.")
        Utils.set_inverter_mode(mode=3)
        return

def publish_mqtt_trigger():
    publish.single("Cerbomoticzgx/EnergyBroker/RunTrigger", payload=f"{{\"value\": {time.localtime().tm_hour}}}", qos=0, retain=False,
                   hostname=cerboGxEndpoint)

def set_48h_charging_schedule(caller=None, price_cap=MAX_TIBBER_BUY_PRICE, max_items=get_seasonal_max_items()):
    logging.info(f"EnergyBroker: set up charging schedule request received by {caller}")

    if max_items < 1:
        return False

    clear_victron_schedules()
    new_schedule = lowest_48h_prices(price_cap=price_cap, max_items=max_items)

    if len(new_schedule) > 0:
        schedule = 0
        for item in new_schedule:
            hour = int(item[1])
            day = item[0]
            price = item[3]
            schedule_victron_ess_charging(int(hour), schedule=schedule, day=day)
            push_notification(hour, day, price)
            schedule += 1

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
    soc = 95
    start = hour * 3600

    publish.single(f"{topic_stub}Duration", payload=f"{{\"value\": {duration}}}", qos=0, retain=False,
                   hostname=cerboGxEndpoint)
    publish.single(f"{topic_stub}Soc", payload=f"{{\"value\": {soc}}}", qos=0, retain=False,
                   hostname=cerboGxEndpoint)
    publish.single(f"{topic_stub}Start", payload=f"{{\"value\": {start}}}", qos=0, retain=False,
                   hostname=cerboGxEndpoint)
    publish.single(f"{topic_stub}Day", payload=f"{{\"value\": {weekday}}}", qos=0, retain=False,
                   hostname=cerboGxEndpoint)

    logging.info(f"EnergyBroker: Adding schedule entry for day:{weekday}, duration:{duration}, start: {start}")

def clear_victron_schedules():
    for i in range(0, 5):
        day = -1
        topic_stub = f"W/{systemId0}/settings/0/Settings/CGwacs/BatteryLife/Schedule/Charge/{i}/"
        publish.single(f"{topic_stub}Day", payload=f"{{\"value\": {day}}}", qos=0, retain=False,
                       hostname=cerboGxEndpoint)

def push_notification(hour, day, price):
    topic = f"Energy Broker Alert"
    msg = f"ESS Charge scheduled for {hour}:00 {'Today' if day == 0 else 'Tomorrow'} @ {price}"
    pushover_notification(topic, msg)

class Utils:
    @staticmethod
    def set_inverter_mode(mode: int):
        """
        :param mode: 3 = normal mode. inverter on, batteries will be discharged if PV is not sufficient
                     1 = charger only mode - inverter will not switch on, batteries will not be discharged
        """
        mode_name = {1: "Charging Only Mode", 3: "Normal Inverter Mode"}
        topic = f"W/{systemId0}/vebus/276/Mode"

        if mode and mode == 1 or mode == 3:
            publish.single(topic, payload=f"{{\"value\": {mode}}}", qos=0, retain=False, hostname=cerboGxEndpoint)
            logging.info(f"EnergyBroker.Utils.set_inverter_mode: {__name__} has set Multiplus-II's mode to {mode_name.get(mode)}")
