import os
import math
import signal
import paho.mqtt.publish as publish

from lib.helpers import get_topic_key
from lib.constants import dotenv_config, logging, cerboGxEndpoint
from lib.victron_integration import regulate_battery_max_voltage
from lib.tibber_api import publish_pricing_data
from lib.global_state import GlobalStateClient
from lib.tesla_api import TeslaApi
from lib.energy_broker import (
    manage_sale_of_stored_energy_to_the_grid,
    set_48h_charging_schedule,
    manage_grid_usage_based_on_current_price
)


tesla = TeslaApi()

LOAD_RESERVATION = int(dotenv_config("LOAD_RESERVATION")) or 0
LOAD_RESERVATION_REDUCTION_FACTOR = float(dotenv_config("LOAD_REDUCTION_FACTOR")) or 1
MINIMUM_ESS_SOC = int(dotenv_config("MINIMUM_ESS_SOC")) or 100


class Event:

    def __init__(self, mqtt_topic, value, logging_msg=None):
        # map mqqt topic to a shorter friendly  name which will match the class method name as well
        self.topic_key = get_topic_key(topic=mqtt_topic)
        self.mqtt_topic = mqtt_topic
        self.value = value
        self.logging_msg = logging_msg
        self.gs_client = GlobalStateClient()

        logging.debug(f"{self.topic_key} = {self.value}")

    def dispatch(self):
        try:
            if self.topic_key:
                # Update the Global State db even if we do not have an explicit method defined for this topi_key
                self.gs_client.set(self.topic_key, self.value)

                # if a method is defined, call it. Otherwise, call _unhandled_method()
                getattr(self, self.topic_key, self._unhandled_method)()
                logging.debug(f"{self.topic_key} method")
            else:
                self._unhandled_method()
        except TypeError as e:
            logging.info(e)

    def _unhandled_method(self):
        # if a specific handle method is not specified here for a topic, it will still get written to the
        # global state db but will just be uncaught in this event handler.
        logging.debug(f"{__name__}: Invalid method or nothing implemented for topic: '{self.mqtt_topic}'")

    def tibber_price_now(self):
        _value = float(self.value)
        manage_grid_usage_based_on_current_price(_value)

    def system_shutdown(self):
        _value = self.value

        if _value == "False":
            return True

        if _value == "True":
            _pid = os.getpid()
            logging.info(f"lib.event_handler: received shutdown message from broker. Sending SIGKILL to PID {_pid}...")
            os.kill(_pid, signal.SIGKILL)
        else:
            logging.info(f"lib.event_handler: received invalid message \"{_value}\" from broker on shutdown topic. Ignoring.")

    def batt_voltage(self):
        _value = round(self.value, 2)
        publish.single("Tesla/vehicle0/solar/ess_volts", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def batt_soc(self):
        _value = round(self.value, 2)
        publish.single("Tesla/vehicle0/solar/ess_soc", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        if dotenv_config('VICTRON_OPTIMIZED_CHARGING') == '1':
            regulate_battery_max_voltage(_value)
        if dotenv_config('TIBBER_UPDATES_ENABLED') == '1':
            publish_pricing_data(__name__)
            manage_sale_of_stored_energy_to_the_grid(_value)

    def batt_power(self):
        _value = round(self.value)
        publish.single("Tesla/vehicle0/solar/ess_watts", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        self.calculate_surplus_watts()

    def pv_power(self):
        _value = round(self.value)
        publish.single("Tesla/vehicle0/solar/pv_watts", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        self.calculate_surplus_watts()

    def pv_current(self):
        _value = round(self.value)
        publish.single("Tesla/vehicle0/solar/pv_amps", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def tesla_power(self):
        _value = round(self.value)
        self.adjust_ac_out_power()
        publish.single("Tesla/vehicle0/charging_watts", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/Ac/tesla_load", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def ac_out_power(self):
        self.adjust_ac_out_power()

    def ac_in_power(self):
        _value = round(self.value)
        self.adjust_ac_out_power()
        publish.single("Tesla/vehicle0/Ac/ac_in", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def max_charge_voltage(self):
        _value = float(self.value)
        publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{_value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def grid_charging_enabled(self):
        _value = self.value == "True"
        # todo: EvCharger.set_grid_charging_enabled(_value)

    def tesla_l1_current(self):
        self.update_charging_amp_totals()

    def tesla_l2_current(self):
        self.update_charging_amp_totals()

    def tesla_l3_current(self):
        self.update_charging_amp_totals()

    #
    # calculation and helper methods
    #

    @staticmethod
    def amps_to_watts(amps):
        return amps * 230 * 3

    @staticmethod
    def watts_to_amps(watts):
        return math.floor(watts / 230 / 3)

    def calculate_surplus_watts(self):
        if dotenv_config('ABB_METER_INTEGRATION') == '1':
            self.calculate_and_set_precise_surplus_watts()
        else:
            self.calculate_and_set_surplus_watts()

    def calculate_and_set_surplus_amps(self, surplus_watts):
        surplus_amps = self.watts_to_amps(surplus_watts)
        surplus_amps = 0 if surplus_amps <= 0 else surplus_amps

        self.set_surplus_amps(surplus_amps)

        return surplus_amps

    def calculate_and_set_surplus_watts(self):
        pv_watts = self.gs_client.get("pv_power")
        surplus_watts = pv_watts - LOAD_RESERVATION

        self.set_surplus_watts(round(surplus_watts))
        # update surplus amps as well
        self.calculate_and_set_surplus_amps(surplus_watts)

        return round(surplus_watts, 0)

    def calculate_and_set_precise_surplus_watts(self):
        ess_watts = self.gs_client.get("batt_power")
        pv_watts = self.gs_client.get("pv_power")
        acload_watts = self.gs_client.get("ac_out_adjusted_power")

        if ess_watts < 0:
            ess_watts = -ess_watts

        surplus_watts = round(pv_watts - (ess_watts + acload_watts + LOAD_RESERVATION))

        self.set_surplus_watts(round(surplus_watts))
        self.calculate_and_set_surplus_amps(surplus_watts)

        return round(surplus_watts, 0)

    def update_charging_amp_totals(self, charging_amp_totals=None):
        if not charging_amp_totals:
            l1, l2, l3 = self.gs_client.get("tesla_l1_current"), self.gs_client.get("tesla_l2_current"), self.gs_client.get("tesla_l3_current")
            charging_amp_totals = (l1 + l2 + l3) / 3

        charging_amps = round(charging_amp_totals, 2)

        self.gs_client.set("tesla_charging_amps_total", charging_amps)
        publish.single("Tesla/vehicle0/charging_amps", payload=f"{{\"value\": \"{charging_amps}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_surplus_amps(self, surplus_amps):
        self.gs_client.set("surplus_amps", surplus_amps)
        publish.single("Tesla/vehicle0/solar/surplus_amps", payload=f"{{\"value\": \"{surplus_amps}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        if surplus_amps > 0:
            publish.single("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"false\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        else:
            publish.single("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"true\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_surplus_watts(self, surplus_watts):
        surplus_watts = round(surplus_watts, 2)
        self.gs_client.set("surplus_watts", surplus_watts)
        publish.single("Tesla/vehicle0/solar/surplus_watts", payload=f"{{\"value\": \"{surplus_watts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/solar/load_reservation", payload=f"{{\"value\": \"{LOAD_RESERVATION}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def adjust_ac_out_power(self):
        adjusted_ac_out_power = round(self.gs_client.get("ac_out_power") - self.gs_client.get("tesla_power"), 2)
        self.gs_client.set("ac_out_adjusted_power", adjusted_ac_out_power)
        publish.single("Tesla/vehicle0/Ac/ac_loads", payload=f"{{\"value\": \"{adjusted_ac_out_power}\"}}", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)

    @staticmethod
    def trigger_ess_charge_scheduling():
        set_48h_charging_schedule(__name__)
