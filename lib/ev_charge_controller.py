import datetime
import time
import urllib3
import pytz
import threading

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.tesla_api import TeslaApi
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message


PROPERTY_MAPPING = {
    "charging_watts": "tesla_power",
    "surplus_watts": "surplus_watts",
    "surplus_amps": "surplus_amps",
    "no_sun_production": "is_the_sun_shining",
    "pv_watts": "pv_power",
    "pv_amps": "pv_current",
    "ess_soc": "batt_soc",
    "ess_volts": "batt_voltage",
    "ess_watts": "batt_power",
    "ess_max_charge_voltage": "max_charge_voltage",
    "acin_watts": "ac_in_power",
    "acout_watts": "ac_out_power",
    "acload_watts": "ac_out_adjusted_power",
    "l1_charging_amps": "tesla_l1_current",
    "l2_charging_amps": "tesla_l2_current",
    "l3_charging_amps": "tesla_l3_current",
    "charging_amps": "tesla_charging_amps_total"
}


class DynamicProperty:
    def __init__(self, key):
        self.key = key

    def __get__(self, instance, owner):
        if instance is None:
            return self

        return instance.global_state.get(self.key)

def create_property(property_name: str, key: str):
    setattr(EvCharger, property_name, DynamicProperty(key))


class EvCharger:

    _http = urllib3.PoolManager()
    tz = pytz.timezone('Europe/Amsterdam')

    def __init__(self):
        logging.info("EvCharger (__init__): Initializing...")

        self.main_thread = None
        self.global_state = GlobalStateClient()

        # Dynamically creates properties from PROPERTY_MAPPING dict
        for property_name, key in PROPERTY_MAPPING.items():
            create_property(property_name, key)

        self.grid_charging_enabled = self.global_state.get('grid_charging_enabled')
        self.load_reservation = int(retrieve_setting("LOAD_RESERVATION"))  # see example .env.example file
        self.load_reservation_is_reduced = False
        self.load_reservation_reduction_factor = float(retrieve_setting("LOAD_REDUCTION_FACTOR"))
        self.minimum_ess_soc = int(retrieve_setting("MINIMUM_ESS_SOC"))  # see example .env.example file

        self.tesla = TeslaApi()

        self.surplus_checks = 0
        self.surplus_check_start_time = 0

        logging.info("EvCharger (__init__): Init complete.")

    def __del__(self):
        self.cleanup()
        self.tesla.__del__()
        logging.info("EvCharger (__del__): Exiting...")

    def main(self):
        try:
            if self.should_manage_or_initiate_charging():
                self.dynamic_load_reservation_adjustment()
                self.tesla.update_vehicle_status(force=False)
                if not self.tesla.is_charging:
                    self.initiate_charging()
                    self.main_thread = threading.Timer(5.0, self.main)
                elif self.tesla.is_charging:
                    self.manage_charging()
                    self.main_thread = threading.Timer(20.0, self.main)

                logging.info(self.vehicle_status_msg())

            else:
                if self.global_state.get('grid_charging_enabled') or self.global_state.get('tesla_charge_requested'):
                    self.tesla.update_vehicle_status(force=False)
                    logging.info(self.vehicle_status_msg())
                self.main_thread = threading.Timer(20.0, self.main)

            self.main_thread.daemon = True
            self.main_thread.start()

        except Exception as E:
            # todo: handle '401 Client Error: invalid bearer token' ?
            logging.info(str(E))

            # restart the main loop on failure
            self.main_thread = threading.Timer(5.0, self.main)
            self.main_thread.daemon = True
            self.main_thread.start()

    def should_manage_or_initiate_charging(self):
        # todo: move this logic into another function and make sure it does not keep triggering while charge_requested is True
        if self.global_state.get('tesla_charge_requested') or self.global_state.get('grid_charging_enabled'):
            if not self.tesla.is_charging or int(self.charging_watts) < 5:
                if self.tesla.is_home and self.tesla.is_plugged:
                    logging.info(f"EvChargeControl: Charge request received. Sending charge start TeslaApi command.")
                    self.tesla.start_tesla_charge()
                    self.tesla.update_vehicle_status(force=True)
                    return False

        if (int(self.charging_watts) > 5
            and self.tesla.is_home
            and not self.tesla.is_supercharging
            and not self.global_state.get('grid_charging_enabled')
            and not self.global_state.get('tesla_charge_requested')):
            return True

        if (self.is_the_sun_shining()
                and int(self.ess_soc) >= self.minimum_ess_soc
                and int(self.surplus_amps) >= 2
                and not self.global_state.get('grid_charging_enabled')
                and not self.global_state.get('tesla_charge_requested')
                and self.tesla.is_home
                and self.tesla.is_plugged
                and not self.tesla.is_supercharging
                and not self.tesla.is_full):
            return True

        logging.debug("No condition to initiate or manage charging was met. This means a no-op for the EV charging module.")
        return False

    def initiate_charging(self):
        # Inititial start charge logic

        # if not tesla.is_charging and tesla.is_plugged:  # todo: testing without this extra check
        if self.surplus_amps >= 2:
            try:
                logging.info(f"EvCharger (start charge): Surplus energy detected! Requesting start charge at "
                             f"{self.surplus_amps} Amps")
                if self.tesla.set_tesla_charge_amps(self.surplus_amps):
                    self.set_surplus_amps(self.surplus_amps)
                    self.tesla.start_tesla_charge()
                    return True

            except Exception as E:
                logging.info(E)
                return False

        if self.surplus_amps < 2:
            try:
                logging.info(f"EvCharger (start charge): {self.surplus_amps} Amp(s)/{self.surplus_watts} Watt(s) "
                             f"insufficient surplus solar energy.")
                logging.debug(self.vehicle_status_msg())
                self.set_surplus_amps(self.surplus_amps)
                self.update_charging_amp_totals(0)
                return False

            except Exception as E:
                logging.info(E)
                return False

        logging.debug(self.general_status_msg())

    def manage_charging(self):
        # adjusting charge rate when charge is active with 3 checks over 60 seconds to try and filter out temporary
        # loss of pv surplus due to passing clouds, etc
        current_time = time.time()

        if self.surplus_amps < 2:
            try:
                if self.surplus_checks == 0:
                    self.surplus_check_start_time = current_time
                    self.surplus_checks += 1
                    logging.info(
                        f"EvCharger (charge mgmt): Insufficient solar energy of {self.surplus_amps} Amps. Check count is: {self.surplus_checks}")
                # Second check, but ensuring it's within a 60-second window
                elif self.surplus_checks == 1 and (current_time - self.surplus_check_start_time) <= 60:
                    self.surplus_checks += 1
                    logging.info(
                        f"EvCharger (charge mgmt): Insufficient solar energy of {self.surplus_amps} Amps. Check count is: {self.surplus_checks}")
                # Third check
                elif self.surplus_checks == 2 and (current_time - self.surplus_check_start_time) <= 60:
                    self.surplus_checks += 1
                    logging.info(
                        f"EvCharger (charge mgmt): Should stop charge. Insufficient solar energy of {self.surplus_amps} Amps. Check count is: {self.surplus_checks}")
                    self.set_surplus_amps(self.surplus_amps)
                    self.tesla.stop_tesla_charge()
                    self.update_charging_amp_totals(0)
                    self.surplus_checks = 0
                # Reset if 60 seconds have passed since the first check
                if (current_time - self.surplus_check_start_time) >= 60:
                    self.surplus_checks = 0
                return True
            except Exception as E:
                logging.error(f"Error in manage_charging: {E}", exc_info=True)
                return False

        if self.surplus_amps < 2 and self.surplus_checks >= 3:
            try:
                logging.info(f"EvCharger (charge mgmt): Should stop charge. Insufficient solar energy of "
                             f"{self.surplus_amps} Amps")
                self.set_surplus_amps(self.surplus_amps)
                self.tesla.stop_tesla_charge()
                self.update_charging_amp_totals(0)
                self.surplus_checks = 0  # reset the check count
                return True
            except Exception as E:
                logging.info(E)
                return False
        else:
            logging.info(f"EvCharger (charge mgmt): Insufficient solar energy of {self.surplus_amps} Amps but will check this again in 20 seconds before taking action.")
            self.surplus_checks += 1

        if self.surplus_amps != round(self.charging_amps, 0) and self.surplus_amps >= 2:
            try:
                logging.info(f"EvCharger (charge mgmt): current charge limit is {self.charging_amps} Amp(s). Should "
                             f"adjust charge rate to {self.surplus_amps} surplus Amp(s).")
                self.set_surplus_amps(self.surplus_amps)
                self.tesla.set_tesla_charge_amps(self.surplus_amps)
                self.update_charging_amp_totals(self.surplus_amps)
                return True
            except Exception as E:
                logging.info(E)
                return False

        if self.tesla.is_max_soc_reached():
            try:
                logging.info(f"EvCharger (charge mgmt): Max SOC reached. Stopping charge.")
                self.tesla.stop_tesla_charge()
                self.update_charging_amp_totals(0)
                return True
            except Exception as E:
                logging.info(E)
                return False

        logging.debug(self.general_status_msg())

    def dynamic_load_reservation_adjustment(self):
        if int(self.ess_soc) >= int(self.minimum_ess_soc) and not self.load_reservation_is_reduced:
            self.load_reservation = round((self.load_reservation / self.load_reservation_reduction_factor))
            self.load_reservation_is_reduced = True
            logging.info(f"EvCharger (dynamic load adjustment): Desired ESS SOC is reached at {round(self.ess_soc, 2)}%. applying the load"
                         f" reservation factor and setting to {self.load_reservation} Watts")

        elif int(self.ess_soc) < int(self.minimum_ess_soc) and self.load_reservation_is_reduced:
            self.load_reservation = round((self.load_reservation * self.load_reservation_reduction_factor))
            self.load_reservation_is_reduced = False
            logging.info(f"EvCharger (dynamic load adjustment): ESS SOC is too low at {self.ess_soc}%. Restoring the load"
                         f"reservation to the default {self.load_reservation} Watts")

        else:
            logging.debug(f"EvCharger (dynamic load adjustment): No load adjustment is required. Current reservation is {self.load_reservation} Watts")

        publish_message("Tesla/vehicle0/solar/load_reservation", payload=f"{{\"value\": \"{self.load_reservation}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/solar/load_reservation_is_reduced", payload=f"{{\"value\": \"{self.load_reservation_is_reduced}\"}}", qos=0, retain=True)

    def set_surplus_amps(self, surplus_amps):
        self.global_state.set("surplus_amps", surplus_amps)
        publish_message("Tesla/vehicle0/solar/surplus_amps", payload=f"{{\"value\": \"{surplus_amps}\"}}", qos=0, retain=True)

        if surplus_amps > 0:
            publish_message("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"False\"}}", qos=0, retain=True)
        else:
            publish_message("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"True\"}}", qos=0, retain=True)

    def set_surplus_watts(self, surplus_watts):
        self.global_state.set("surplus_watts", round(surplus_watts, 2))
        publish_message("Tesla/vehicle0/solar/surplus_watts", payload=f"{{\"value\": \"{surplus_watts}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/solar/load_reservation", payload=f"{{\"value\": \"{self.load_reservation}\"}}", qos=0, retain=True)

    def update_charging_amp_totals(self, charging_amp_totals=None):
        if not charging_amp_totals:
            charging_amp_totals = (self.l1_charging_amps + self.l2_charging_amps + self.l3_charging_amps) / 3

        self.global_state.set("tesla_charging_amps_total", round(charging_amp_totals, 2))
        publish_message("Tesla/vehicle0/charging_amps", payload=f"{{\"value\": \"{self.charging_amps}\"}}", qos=0, retain=True)

    @staticmethod
    def is_the_sun_shining():
        return False if datetime.datetime.now().time().hour < 10 or datetime.datetime.now().time().hour >= 18 \
            else True

    def vehicle_status_msg(self):
        return f"EvCharger (vehicle): Charging: {self.tesla.is_charging}, Plugged: {self.tesla.is_plugged}, " \
               f"Car SOC: {self.tesla.vehicle_soc}%, Car SOC Setpoint: {self.tesla.vehicle_soc_setpoint}%, ESS SOC: {round(self.ess_soc, 2)}%, " \
               f"Surplus: {self.surplus_watts}W / {self.surplus_amps}A" \
               f" ETA: {self.tesla.time_until_full}"

    def general_status_msg(self):
        return f"EvCharger (general): PV Surplus: {self.surplus_amps}A / {self.surplus_watts}W" \
                f" AC Loads: {self.acload_watts}W"

    @staticmethod
    def cleanup():
        logging.info("EvCharger: Topic Housecleaning...")
        # clear out topic which activates this UI widget in dashboard
        publish_message("Tesla", payload=None, qos=0, retain=False)
        # Deprecated
        # publish_message("Tesla/vehicle0/Ac/ac_loads", payload=None, qos=0, retain=False)
        # publish_message("Tesla/vehicle0/Ac/ac_in", payload=None, qos=0, retain=False)
        # publish_message("Tesla/vehicle0/Ac/tesla_load", payload=None, qos=0, retain=False)
