import os
import signal
from lib.helpers import get_topic_key
from lib.constants import dotenv_config, logging
from lib.victron_integration import regulate_battery_max_voltage
from lib.tibber_api import publish_pricing_data
from lib.energy_broker import set_48h_charging_schedule

class Event:

    def __init__(self, mqtt_topic, value, logging_msg=None, ev_charger=None):
        self.EvCharger = ev_charger
        # map mqqt topic to a shorter friendly  name which will match the class method name as well
        self.topic_key = get_topic_key(topic=mqtt_topic)
        self.mqtt_topic = mqtt_topic
        self.value = value
        self.logging_msg = logging_msg

        # logging.info(f"{self.topic_key} = {self.value}")

    def dispatch(self):
        try:
            if self.topic_key:
                # call the method which matches self.topic_key
                getattr(self, self.topic_key, self._invalid_method)()
                # logging.info(f"{self.topic_key} method")
            else:
                self._invalid_method()
        except TypeError as e:
            logging.info(e)

    def _invalid_method(self):
        logging.info(f"{__name__}: Invalid method or nothing implemented for topic: '{self.mqtt_topic}'")

    def system_shutdown(self):
        _value = self.value
        if "True" in _value:
            _pid = os.getpid()
            logging.info(f"lib.event_handler: received shutdown message from broker. Sending SIGKILL to PID {_pid}...")
            os.kill(_pid, signal.SIGKILL)
        else:
            logging.info(f"lib.event_handler: received invalid message \"{_value}\" from broker on shutdown topic. Ignoring.")

    def system_state(self):
        pass

    def inverter_mode(self):
        """ Triggered when inverter mode setting is changed: on, off, charger only"""
        pass

    def batt_voltage(self):
        _value = round(self.value, 2)
        self.EvCharger.set_ess_volts(_value)

    def batt_soc(self):
        _value = round(self.value, 2)
        self.EvCharger.set_ess_soc(_value)
        if dotenv_config('VICTRON_OPTIMIZED_CHARGING') == '1':
            regulate_battery_max_voltage(_value)
        if dotenv_config('TIBBER_UPDATES_ENABLED') == '1':
            publish_pricing_data(__name__)

    def modules_online(self):
        pass

    def minimum_ess_soc(self):
        pass

    def batt_current(self):
        pass

    def batt_power(self):
        _value = round(self.value)
        self.EvCharger.set_ess_watts(_value)

    def pv_power(self):
        _value = round(self.value)
        self.EvCharger.set_pv_watts(_value)

    def pv_current(self):
        _value = round(self.value)
        self.EvCharger.set_pv_amps(_value)

    def c1_daily_yield(self):
        pass

    def c2_daily_yield(self):
        pass

    def tesla_power(self):
        _value = round(self.value)
        self.EvCharger.set_charging_watts(_value)

    def tesla_l1_current(self):
        self.EvCharger.set_l1_charging_amps(self.value)

    def tesla_l2_current(self):
        self.EvCharger.set_l2_charging_amps(self.value)

    def tesla_l3_current(self):
        self.EvCharger.set_l3_charging_amps(self.value)

    def ac_out_power(self):
        self.EvCharger.set_acload_watts(self.value)

    def ac_in_power(self):
        self.EvCharger.set_acin_watts(self.value)

    def max_charge_voltage(self):
        _value = float(self.value)
        self.EvCharger.set_ess_max_charge_voltage(_value)

    def grid_charging_enabled(self):
        _value = self.value == "True"
        self.EvCharger.set_grid_charging_enabled(_value)

    def ac_power_setpoint(self):
        pass

    @staticmethod
    def trigger_ess_charge_scheduling():
        set_48h_charging_schedule(__name__)
