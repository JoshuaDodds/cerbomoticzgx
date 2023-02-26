import datetime

import urllib3
import pytz
import math
import threading
import paho.mqtt.publish as publish

from lib.constants import logging, cerboGxEndpoint, dotenv_config
from lib.tesla_api import TeslaApi
from lib.victron_integration import is_grid_import_enabled
from lib.energy_broker import Utils as EnergyBrokerUtils

tesla = TeslaApi()

class EvCharger:

    _http = urllib3.PoolManager()
    tz = pytz.timezone('Europe/Amsterdam')

    def __init__(self):
        logging.info("EvCharger (__init__): Initializing...")

        self.main_thread = None

        self.surplus_watts = 0
        self.surplus_amps = 0
        self.no_sun_production = self.is_the_sun_shining()
        self.pv_watts = 0                   # will be set by the mqtt bus
        self.pv_amps = 0                    # will be set by the mqtt bus
        self.ess_soc = 0                    # will be set by the mqtt bus
        self.ess_volts = 0                  # will be set by the mqtt bus
        self.ess_watts = 0                  # will be set by the mqtt bus
        self.ess_max_charge_voltage = 0.0   # will be set by the mqtt bus
        self.acin_watts = 0                 # will be set by the mqtt bus
        self.acload_watts = 0               # will be set by the mqtt bus

        self.charging_amps = 0
        self.charging_watts = self.amps_to_watts(self.charging_amps)
        self.l1_charging_amps = 0
        self.l2_charging_amps = 0
        self.l3_charging_amps = 0

        self.grid_charging_enabled = is_grid_import_enabled()
        self.load_reservation = int(dotenv_config("LOAD_RESERVATION"))  # see example .env.example file
        self.load_reservation_is_reduced = False
        self.load_reservation_reduction_factor = float(dotenv_config("LOAD_REDUCTION_FACTOR"))
        self.minimum_ess_soc = int(dotenv_config("MINIMUM_ESS_SOC"))  # see example .env.example file

        tesla.update_vehicle_status(force=True)

        logging.info("EvCharger (__init__): Init complete.")

    def __del__(self):
        self.cleanup()
        tesla.__del__()
        logging.info("EvCharger (__del__): Exiting...")

    def main(self):
        try:
            if self.should_manage_or_initiate_charging():
                self.dynamic_load_reservation_adjustment()
                tesla.update_vehicle_status(force=True)
                if not tesla.is_vehicle_charging():
                    self.initiate_charging()
                elif tesla.is_vehicle_charging():
                    self.manage_charging()

                logging.info(self.vehicle_status_msg())
                self.main_thread = threading.Timer(3.0, self.main)

            else:
                if self.grid_charging_enabled:
                    tesla.update_vehicle_status(force=True)
                    logging.info(self.vehicle_status_msg())
                elif tesla.is_vehicle_plugged() and self.is_the_sun_shining():
                    tesla.update_vehicle_status(force=True)
                    logging.info(self.vehicle_status_msg())
                else:
                    tesla.update_mqtt_and_domoticz()
                    logging.info(self.general_status_msg())
                self.main_thread = threading.Timer(20.0, self.main)

            self.main_thread.daemon = True
            self.main_thread.start()

        except Exception as E:
            # todo: handle '401 Client Error: invalid bearer token' ?
            logging.info(E)

            # restart the main loop on failure
            self.main_thread = threading.Timer(5.0, self.main)
            self.main_thread.daemon = True
            self.main_thread.start()

    def should_manage_or_initiate_charging(self):
        if (tesla.is_charging
                and tesla.is_home
                and not tesla.is_supercharging
                and not self.grid_charging_enabled):
            return True

        if (self.is_the_sun_shining()
                and int(self.ess_soc) >= 95
                and int(self.surplus_amps) >= 2
                and not self.grid_charging_enabled
                and tesla.is_home
                and tesla.is_plugged
                and not tesla.is_supercharging
                and not tesla.is_full):
            return True

        return False

    def initiate_charging(self):
        # Inititial start charge logic
        if not tesla.is_charging and tesla.is_plugged:

            if self.surplus_amps >= 2:
                try:
                    logging.info(f"EvCharger (start charge): Surplus energy detected! Requesting start charge at "
                                 f"{self.surplus_amps} Amps")
                    if tesla.set_tesla_charge_amps(self.surplus_amps):
                        self.set_surplus_amps(self.surplus_amps)
                        tesla.start_tesla_charge()
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

        logging.info(self.general_status_msg())

    def manage_charging(self):
        # adjusting charge rate when charge is active
        if tesla.is_charging:
            if self.surplus_amps < 2:
                try:
                    logging.info(f"EvCharger (charge mgmt): Should stop charge. Insufficient solar energy of "
                                 f"{self.surplus_amps} Amps")
                    self.set_surplus_amps(self.surplus_amps)
                    tesla.stop_tesla_charge()
                    self.update_charging_amp_totals(0)
                    return True
                except Exception as E:
                    logging.info(E)
                    return False

            if self.surplus_amps != round(self.charging_amps, 0) and self.surplus_amps >= 2:
                try:
                    logging.info(f"EvCharger (charge mgmt): current charge limit is {self.charging_amps} Amp(s). Should "
                                 f"adjust charge rate to {self.surplus_amps} surplus Amp(s).")
                    self.set_surplus_amps(self.surplus_amps)
                    tesla.set_tesla_charge_amps(self.surplus_amps)
                    self.update_charging_amp_totals(self.surplus_amps)
                    return True
                except Exception as E:
                    logging.info(E)
                    return False

            if tesla.is_max_soc_reached():
                try:
                    logging.info(f"EvCharger (charge mgmt): Max SOC reached. Stopping charge.")
                    tesla.stop_tesla_charge()
                    self.update_charging_amp_totals(0)
                    return True
                except Exception as E:
                    logging.info(E)
                    return False

        logging.info(self.general_status_msg())

    def dynamic_load_reservation_adjustment(self):
        if int(self.ess_soc) >= int(self.minimum_ess_soc) and not self.load_reservation_is_reduced:
            self.load_reservation = round((self.load_reservation / self.load_reservation_reduction_factor))
            self.load_reservation_is_reduced = True
            logging.info(f"EvCharger (dynamic load adjustment): Desired ESS SOC is reached at {self.ess_soc}%. applying the load"
                         f" reservation factor and setting to {self.load_reservation} Watts")

        elif int(self.ess_soc) < int(self.minimum_ess_soc) and self.load_reservation_is_reduced:
            self.load_reservation = round((self.load_reservation * self.load_reservation_reduction_factor))
            self.load_reservation_is_reduced = False
            logging.info(f"EvCharger (dynamic load adjustment): ESS SOC is too low at {self.ess_soc}%. Restoring the load"
                         f"reservation to the default {self.load_reservation} Watts")

        else:
            logging.debug(f"EvCharger (dynamic load adjustment): No load adjustment is required. Current reservation is {self.load_reservation} Watts")

        publish.single("Tesla/vehicle0/solar/load_reservation", payload=f"{{\"value\": \"{self.load_reservation}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/solar/load_reservation_is_reduced", payload=f"{{\"value\": \"{self.load_reservation_is_reduced}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    @staticmethod
    def amps_to_watts(amps):
        return amps * 230 * 3

    @staticmethod
    def watts_to_amps(watts):
        return math.floor(watts / 230 / 3)

    def calculate_and_set_surplus_amps(self, surplus_watts):
        surplus_amps = self.watts_to_amps(surplus_watts)
        surplus_amps = 0 if surplus_amps <= 0 else surplus_amps

        self.set_surplus_amps(surplus_amps)

        return surplus_amps

    def calculate_and_set_surplus_watts(self):
        surplus_watts = self.pv_watts - self.load_reservation

        self.set_surplus_watts(round(surplus_watts))

        # update surplus amps as well
        self.calculate_and_set_surplus_amps(surplus_watts)

        return round(surplus_watts, 0)

    def calculate_and_set_precise_surplus_watts(self):
        ess_watts = self.ess_watts
        if self.ess_watts < 0:
            ess_watts = -self.ess_watts

        surplus_watts = round(self.pv_watts - (ess_watts + self.acload_watts + self.load_reservation))

        self.set_surplus_watts(round(surplus_watts))
        self.calculate_and_set_surplus_amps(surplus_watts)

        return round(surplus_watts, 0)

    def set_surplus_amps(self, surplus_amps):
        self.surplus_amps = surplus_amps
        publish.single("Tesla/vehicle0/solar/surplus_amps", payload=f"{{\"value\": \"{surplus_amps}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        if surplus_amps > 0:
            publish.single("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"false\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        else:
            publish.single("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"true\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_surplus_watts(self, surplus_watts):
        self.surplus_watts = round(surplus_watts, 2)
        publish.single("Tesla/vehicle0/solar/surplus_watts", payload=f"{{\"value\": \"{surplus_watts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/solar/load_reservation", payload=f"{{\"value\": \"{self.load_reservation}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_acin_watts(self, watts):
        self.acin_watts = watts
        publish.single("Tesla/vehicle0/Ac/ac_in", payload=f"{{\"value\": \"{watts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_acload_watts(self, watts):
        self.acload_watts = round(watts - self.charging_watts, 2)
        publish.single("Tesla/vehicle0/Ac/ac_loads", payload=f"{{\"value\": \"{self.acload_watts}\"}}", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)

    def update_charging_amp_totals(self, charging_amp_totals=None):
        if not charging_amp_totals:
            charging_amp_totals = (self.l1_charging_amps + self.l2_charging_amps + self.l3_charging_amps) / 3

        self.charging_amps = round(charging_amp_totals, 2)

        publish.single("Tesla/vehicle0/charging_amps", payload=f"{{\"value\": \"{self.charging_amps}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_l1_charging_amps(self, amps):
        self.l1_charging_amps = amps
        self.update_charging_amp_totals()

    def set_l2_charging_amps(self, amps):
        self.l2_charging_amps = amps
        self.update_charging_amp_totals()

    def set_l3_charging_amps(self, amps):
        self.l3_charging_amps = amps
        self.update_charging_amp_totals()

    def set_charging_watts(self, watts):
        self.charging_watts = watts
        publish.single("Tesla/vehicle0/charging_watts", payload=f"{{\"value\": \"{self.charging_watts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/Ac/tesla_load", payload=f"{{\"value\": \"{self.charging_watts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_pv_watts(self, watts):
        self.pv_watts = round(watts)
        publish.single("Tesla/vehicle0/solar/pv_watts", payload=f"{{\"value\": \"{self.pv_watts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        if dotenv_config('ABB_METER_INTEGRATION') == '1':
            self.calculate_and_set_precise_surplus_watts()
        else:
            self.calculate_and_set_surplus_watts()

    def set_pv_amps(self, amps):
        self.pv_amps = round(amps)
        publish.single("Tesla/vehicle0/solar/pv_amps", payload=f"{{\"value\": \"{self.pv_amps}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_ess_soc(self, soc):
        self.ess_soc = soc
        publish.single("Tesla/vehicle0/solar/ess_soc", payload=f"{{\"value\": \"{self.ess_soc}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_ess_volts(self, volts):
        self.ess_volts = float(volts)
        publish.single("Tesla/vehicle0/solar/ess_volts", payload=f"{{\"value\": \"{volts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_ess_watts(self, watts):
        self.ess_watts = watts
        publish.single("Tesla/vehicle0/solar/ess_watts", payload=f"{{\"value\": \"{self.ess_watts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        if dotenv_config('ABB_METER_INTEGRATION') == '1':
            self.calculate_and_set_precise_surplus_watts()
        else:
            self.calculate_and_set_surplus_watts()

    def set_ess_max_charge_voltage(self, volts: float):
        self.ess_max_charge_voltage = volts
        publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{volts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    def set_grid_charging_enabled(self, status: bool = False):
        self.grid_charging_enabled = status
        if status is True:
            logging.info(f"EvCharger: Charging Vehicle from Grid power is -- ENABLED --")
            # ac_power_setpoint('13000.0')
            EnergyBrokerUtils.set_inverter_mode(mode=1)
            if not tesla.is_vehicle_charging():
                tesla.start_tesla_charge()
            # tesla.set_tesla_charge_amps(18) and tesla.set_tesla_charge_amps(18)
        else:
            logging.info(f"EvCharger: Charging Vehicle from Grid power is -- DISABLED --")
            if tesla.is_vehicle_charging():
                tesla.stop_tesla_charge()
            # ac_power_setpoint('0.0')
            EnergyBrokerUtils.set_inverter_mode(mode=3)

            # publish.single(Topics["system0"]["grid_charging_enabled"], payload=f"{{\"value\": \"{status}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    @staticmethod
    def is_the_sun_shining():
        return False if datetime.datetime.now().time().hour < 10 or datetime.datetime.now().time().hour >= 18 \
            else True

    def vehicle_status_msg(self):
        return f"EvCharger (vehicle): Charging: {tesla.is_charging}, Plugged: {tesla.is_plugged}, " \
               f"Car SOC: {tesla.vehicle_soc}%, Car SOC Setpoint: {tesla.vehicle_soc_setpoint}%, ESS SOC: {self.ess_soc}%, " \
               f"Surplus: {self.surplus_watts}W / {self.surplus_amps}A" \
               f" ETA: {tesla.time_until_full}"

    def general_status_msg(self):
        return f"EvCharger (general): PV Surplus: {self.surplus_amps}A / {self.surplus_watts}W" \
                f" AC Loads: {self.acload_watts}W"

    @staticmethod
    def cleanup():
        logging.info("EvCharger: Topic Housecleaning before exit...")
        # clear out topics which toggle on functionality only this module uses
        publish.single("Tesla/vehicle0/Ac/ac_loads", payload=None, qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/Ac/ac_in", payload=None, qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/Ac/tesla_load", payload=None, qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
