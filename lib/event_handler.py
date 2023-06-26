import os
import math
import signal

from lib.helpers import get_topic_key, publish_message
from lib.constants import dotenv_config, logging
from lib.victron_integration import regulate_battery_max_voltage
from lib.global_state import GlobalStateClient
from lib.energy_broker import (
    manage_sale_of_stored_energy_to_the_grid,
    set_48h_charging_schedule,
    manage_grid_usage_based_on_current_price
)


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

    def ess_net_metering_enabled(self):
        if self.gs_client.get('ess_net_metering_enabled') is None:
            pass
        if self.gs_client.get('ess_net_metering_enabled'):
            logging.info(f"ESS Net Metering is ENABLED.")
        else:
            logging.info(f"ESS Net Metering is DISABLED.")

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
        publish_message("Tesla/vehicle0/solar/ess_volts", message=f"{_value}", retain=True)

    def batt_soc(self):
        _value = round(self.value, 2)
        publish_message("Tesla/vehicle0/solar/ess_soc", message=f"{_value}", retain=True)

        if dotenv_config('VICTRON_OPTIMIZED_CHARGING') == '1':
            regulate_battery_max_voltage(_value)
        if dotenv_config('TIBBER_UPDATES_ENABLED') == '1':
            manage_sale_of_stored_energy_to_the_grid()

    def batt_power(self):
        _value = round(self.value)
        publish_message("Tesla/vehicle0/solar/ess_watts", message=f"{_value}", retain=True)
        self.calculate_surplus_watts()

    def pv_power(self):
        _value = round(self.value)
        publish_message("Tesla/vehicle0/solar/pv_watts", message=f"{_value}", retain=True)
        self.calculate_surplus_watts()

    def pv_current(self):
        _value = round(self.value)
        publish_message("Tesla/vehicle0/solar/pv_amps", message=f"{_value}", retain=True)

    def tesla_power(self):
        _value = round(self.value)
        self.adjust_ac_out_power()
        publish_message("Tesla/vehicle0/charging_watts", message=f"{_value}", retain=True)
        publish_message("Tesla/vehicle0/Ac/tesla_load", message=f"{_value}", retain=True)

    def ac_out_power(self):
        self.adjust_ac_out_power()

    def ac_in_power(self):
        _value = round(self.value)
        self.adjust_ac_out_power()
        publish_message("Tesla/vehicle0/Ac/ac_in", message=f"{_value}", retain=True)

    def max_charge_voltage(self):
        _value = float(self.value)
        publish_message("Tesla/vehicle0/solar/ess_max_charge_voltage", message=f"{_value}", retain=True)

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
        publish_message("Tesla/vehicle0/charging_amps", message=f"{charging_amps}", retain=True)

    def set_surplus_amps(self, surplus_amps):
        self.gs_client.set("surplus_amps", surplus_amps)
        publish_message("Tesla/vehicle0/solar/surplus_amps", message=f"{surplus_amps}", retain=True)

        if surplus_amps > 0:
            publish_message("Tesla/vehicle0/solar/insufficient_surplus", message="False", retain=True)
        else:
            publish_message("Tesla/vehicle0/solar/insufficient_surplus", message="True", retain=True)

    def set_surplus_watts(self, surplus_watts):
        surplus_watts = round(surplus_watts, 2)
        self.gs_client.set("surplus_watts", surplus_watts)
        publish_message("Tesla/vehicle0/solar/surplus_watts", message=f"{surplus_watts}", retain=True)
        publish_message("Tesla/vehicle0/solar/load_reservation", message=f"{LOAD_RESERVATION}", retain=True)

    def adjust_ac_out_power(self):
        adjusted_ac_out_power = round(self.gs_client.get("ac_out_power") - self.gs_client.get("tesla_power"), 2)
        self.gs_client.set("ac_out_adjusted_power", adjusted_ac_out_power)
        publish_message("Tesla/vehicle0/Ac/ac_loads", message=f"{adjusted_ac_out_power}", retain=False)

    @staticmethod
    def trigger_ess_charge_scheduling():
        set_48h_charging_schedule(__name__)
