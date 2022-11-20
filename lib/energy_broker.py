import time
import requests
import threading
import schedule as scheduler

import paho.mqtt.publish as publish

from .constants import logging, cerboGxEndpoint, systemId0, PythonToVictronWeekdayNumberConversion, PushOverConfig, dotenv_config
from .tibber_api import lowest_48h_prices

def main():
    logging.info("EnergyBroker: Initializing...")

    main_thread = threading.Thread(target=scheduler_loop)
    main_thread.daemon = True
    main_thread.start()

    logging.info("EnergyBroker: Started.")

def scheduler_loop():
    def is_alive(): logging.info(f"EnergyBroker: heartbeat...thumpThump!")

    # scheduler.every().day.at("09:30").do(publish_mqtt_trigger)
    scheduler.every().day.at("13:05").do(publish_mqtt_trigger)
    scheduler.every(5).minutes.do(is_alive)

    for job in scheduler.get_jobs():
        logging.info(f"EnergyBroker: job: {job}")

    while True:
        scheduler.run_pending()
        time.sleep(1)

def publish_mqtt_trigger():
    publish.single("EnergyBroker/RunTrigger", payload=f"{{\"value\": {time.localtime().tm_hour}}}", qos=0, retain=False,
                   hostname=cerboGxEndpoint)

def set_48h_charging_schedule(caller=None, price_cap=0.22):
    logging.info(f"EnergyBroker: set up charging schedule request received by {caller}")

    if dotenv_config('MAX_TIBBER_BUY_PRICE'):
        price_cap = int(dotenv_config('MAX_TIBBER_BUY_PRICE'))

    clear_victron_schedules()
    new_schedule = lowest_48h_prices(price_cap=price_cap)

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
    soc = 100
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
    _id = PushOverConfig.get("id")
    _key = PushOverConfig.get("key")
    msg = f"Energy Broker Alert: ESS Charge scheduled for {hour}:00 {'Today' if day == 0 else 'Tomorrow'} @ {price}"
    payload = {"message": msg, "user": _id, "token": _key}
    _req = requests.post('https://api.pushover.net/1/messages.json', data=payload, headers={'User-Agent': 'CerbomoticzGx'})

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
            logging.info(f"{__name__}: Requested Multiplus-II's mode switch to {mode_name.get(mode)}")
