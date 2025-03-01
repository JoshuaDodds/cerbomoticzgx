import requests.exceptions
import teslapy
import math
import time
import threading

import lib.helpers

from lib.global_state import GlobalStateClient
from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.domoticz_updater import domoticz_update
from lib.helpers import publish_message

STATE = GlobalStateClient()

retry = teslapy.Retry(total=2, status_forcelist=(500, 502, 503, 504))
timeout = 25
email = retrieve_setting("TESLA_EMAIL")

logging.getLogger('teslapy').setLevel(logging.WARNING)


class TeslaApi:
    def __init__(self):
        logging.info(f"TeslaApi (__init__): Initializing...")

        # self.vehicle_api = self.get_vehicle_data()
        self.is_online = False
        self.vehicle_name = "My Tesla"
        self.vehicle_soc = 0
        self.vehicle_soc_setpoint = 0
        self.charging_amp_limit = 0
        self.is_charging = False
        self.is_supercharging = False
        self.is_plugged = False
        self.is_home = False
        self.is_full = False
        self.time_until_full = "N/A"
        self.charging_status = "Unknown"
        self.plugged_status = "Unknown"
        self.last_update_ts = 0
        self.last_update_ts_hr = 0

        self.update_init = threading.Thread(target=self.update_vehicle_status, daemon=True)
        self.update_init.start()

        logging.info(f"TeslaApi: Init complete.")

    def __del__(self):
        self.cleanup()
        logging.info(f"TeslaApi (__del__): Exiting...")

    def update_vehicle_status(self, force=False):
        if (not self.last_update_ts
                or time.localtime() >= time.localtime(self.last_update_ts + (60 * 10))
                or self.is_charging
                or self.is_plugged
                or force):
            logging.debug(
                f"TeslaApi(update_vehicle_statue): (called from: {__name__}): retrieving latest vehicle state... Last update was at: {self.last_update_ts_hr}")

            vehicle_data = self.get_vehicle_data()

            if vehicle_data:
                self.get_vehicle_name(vehicle_data)
                self.battery_soc(vehicle_data)
                self.battery_soc_setpoint(vehicle_data)
                self.is_vehicle_online(vehicle_data)
                self.is_vehicle_charging(vehicle_data)
                self.is_vehicle_supercharging(vehicle_data)
                self.is_vehicle_plugged(vehicle_data)
                self.is_vehicle_home(vehicle_data)
                self.charge_current_request(vehicle_data)
                self.minutes_to_full_charge(vehicle_data)
                self.is_max_soc_reached(vehicle_data)
                self.last_update_ts = vehicle_data.timestamp
                self.last_update_ts_hr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.last_update_ts))
                self.update_mqtt_and_domoticz()
            else:
                logging.info(f"TeslaApi: Connection timed out. Last update was at: {self.last_update_ts_hr}")

        else:
            logging.info(f"TeslaApi: Last vehicle status update was at: {self.last_update_ts_hr}. Skipping new request to mothership (Tesla API)")

    def update_mqtt_and_domoticz(self):
        publish_message("Tesla/vehicle0/vehicle_name", payload=f"{{\"value\": \"{self.vehicle_name}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/charging_status", payload=f"{{\"value\": \"{self.charging_status}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/battery_soc", payload=f"{{\"value\": \"{self.vehicle_soc}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/battery_soc_setpoint", payload=f"{{\"value\": \"{self.vehicle_soc_setpoint}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/plugged_status", payload=f"{{\"value\": \"{self.plugged_status}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/is_home", payload=f"{{\"value\": \"{self.is_home}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/is_supercharging", payload=f"{{\"value\": \"{self.is_supercharging}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/time_until_full", payload=f"{{\"value\": \"{self.time_until_full}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/is_charging", payload=f"{{\"value\": \"{self.is_charging}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/last_update_at", payload=f"{{\"value\": \"{self.last_update_ts_hr}\"}}", qos=0, retain=True)

        # send selected metrics to domoticz for tracking and display
        _msg = f"{self.charging_status} @ {self.charging_amp_limit}A, {self.vehicle_soc}% of {self.vehicle_soc_setpoint}%, {self.plugged_status}"
        domoticz_update('vehicle_status', _msg, "received vehicle metrics update from EvCharger and sent to domoticz")

    # Command Wrappers
    def set_charge(self, amps, error_msg):
        self.wake_vehicle()
        try:
            with teslapy.Tesla(email, retry=retry, timeout=timeout) as tesla:
                vehicles = tesla.vehicle_list()
                vehicles[0].command('CHARGING_AMPS', charging_amps=amps)
                self.charging_amp_limit = amps
                self.update_mqtt_and_domoticz()
                return True
        except teslapy.VehicleError:
            logging.info(f"tesla_api: VehicleError: {error_msg}")
            return False
        except teslapy.HTTPError:
            logging.info(f"tesla_api: HTTPError: {error_msg}")
            return False

    def send_command(self, cmd, error_msg):
        self.wake_vehicle()
        try:
            with teslapy.Tesla(email, retry=retry, timeout=timeout) as tesla:
                vehicles = tesla.vehicle_list()
                vehicles[0].command(cmd)
                if 'START_CHARGE' in cmd:
                    self.is_charging = True
                    self.charging_status = "Charging"
                    self.update_vehicle_status(force=True)
                if 'STOP_CHARGE' in cmd:
                    self.is_charging = False
                    self.time_until_full = "N/A"
                    self.charging_status = "Idle"
                    self.update_vehicle_status(force=True)
                return True

        except teslapy.VehicleError:
            logging.info(f"tesla_api: VehicleError: {error_msg}")
            return False

    @staticmethod
    def wake_vehicle():
        try:
            with teslapy.Tesla(email, retry=retry, timeout=timeout) as tesla:
                vehicles = tesla.vehicle_list()
                if vehicles[0].available():
                    # vehicle is already awake
                    return True
                else:
                    vehicles[0].sync_wake_up()
                    return True

        except teslapy.VehicleError as e:
            logging.info(f"tesla_api: VehicleError: {e}")
            return False
        except teslapy.HTTPError as e:
            logging.info(f"tesla_api: HTTPError: {e}")
            return False

    # Commands
    def stop_tesla_charge(self):
        STATE.set('tesla_charge_requested', "False")
        return self.send_command('STOP_CHARGE', "Error stopping Tesla charge")

    def start_tesla_charge(self):
        STATE.set('tesla_charge_requested', "True")
        return self.send_command('START_CHARGE', "Error starting Tesla charge")

    def set_tesla_charge_amps(self, amps):
        amps = 0 if amps < 0 else amps
        amps = 18 if amps > 18 else amps
        amps = math.floor(amps)

        if amps >= 5:
            return self.set_charge(amps, f"Error setting Tesla charge current to: {amps} Amp(s)")
        if amps < 5:  # when amps are < 5, you need to send the command twice for it to take effect
            self.set_charge(amps, f"Error setting Tesla charge current to: {amps} Amp(s)")
            self.set_charge(amps, f"Error setting Tesla charge current to: {amps} Amp(s)")
            return True
        else:
            return False

    # Metrics / Data
    def get_vehicle_data(self):
        try:
            with teslapy.Tesla(email, retry=retry, timeout=timeout) as tesla:
                vehicles = tesla.vehicle_list()

                if vehicles[0]['state'] == 'online':
                    logging.info("tesla_api: vehicle is online. Fetching vehicle data...")
                else:
                    logging.info("tesla_api: vehicle is sleeping. Waking vehicle to service request...")
                    self.wake_vehicle()

                vehicle_data = vehicles[0].get_vehicle_data()
                return vehicle_data

        except Exception as e:
            logging.error(f"tesla_api: get_vehicle_data() error: {e}")
            return None

    def minutes_to_full_charge(self, vehicle_data) -> str:
        minutes_until_full = vehicle_data['charge_state']['minutes_to_full_charge'] if self.is_charging else "N/A"
        self.time_until_full = lib.helpers.convert_to_fractional_hour(minutes_until_full)
        self.update_mqtt_and_domoticz()
        return self.time_until_full

    def is_vehicle_charging(self, vehicle_data):
        self.is_charging = vehicle_data['charge_state']['charging_state'] == 'Charging'
        self.charging_status = "Charging" if self.is_charging else "Idle"
        self.update_mqtt_and_domoticz()
        return self.is_charging

    def is_vehicle_plugged(self, vehicle_data):
        self.is_plugged = vehicle_data['charge_state']['charge_port_latch'] == 'Engaged'
        self.plugged_status = "Plugged" if self.is_plugged else "Unplugged"
        self.update_mqtt_and_domoticz()
        return self.is_plugged

    def is_max_soc_reached(self, vehicle_data):
        self.is_full = vehicle_data['charge_state']['battery_level'] >= vehicle_data['charge_state']['charge_limit_soc']
        self.update_mqtt_and_domoticz()
        return self.is_full

    def battery_soc_setpoint(self, vehicle_data):
        self.vehicle_soc_setpoint = vehicle_data['charge_state']['charge_limit_soc']
        self.update_mqtt_and_domoticz()
        return self.vehicle_soc_setpoint

    def battery_soc(self, vehicle_data):
        self.vehicle_soc = vehicle_data['charge_state']['battery_level']
        self.update_mqtt_and_domoticz()
        return self.vehicle_soc

    def charge_current_request(self, vehicle_data):
        self. charging_amp_limit = vehicle_data['charge_state']['charge_current_request']
        return self.charging_amp_limit

    def get_vehicle_name(self, vehicle_data):
        self.vehicle_name = vehicle_data['vehicle_state']['vehicle_name']
        return self.vehicle_name

    def is_vehicle_home(self, vehicle_data):
        lat = round(float(retrieve_setting('HOME_ADDRESS_LAT')), 3)
        long = round(float(retrieve_setting('HOME_ADDRESS_LONG')), 3)

        try:
            vehicle_data = vehicle_data['drive_state']
            if 'latitude' in vehicle_data and 'longitude' in vehicle_data:
                if round(vehicle_data['latitude'], 3) == lat and round(vehicle_data['longitude'], 3) == long:
                    self.is_home = True
                else:
                    self.is_home = False
            else:
                logging.info("TeslaApi: latitude or longitude data is missing.")
                self.is_home = None

            self.update_mqtt_and_domoticz()

        except requests.exceptions.InvalidSchema:
            logging.info("TeslaApi: vehicle location could not be determined.")
            self.is_home = None
        except KeyError:
            logging.info("TeslaApi: KeyError in accessing vehicle data.")
            self.is_home = None

        return self.is_home

    def is_vehicle_supercharging(self, vehicle_data):
        self.is_supercharging = vehicle_data['charge_state']['fast_charger_present'] or False
        self.update_mqtt_and_domoticz()
        return self.is_supercharging

    def is_vehicle_online(self, vehicle_data):
        self.is_online = vehicle_data.available()
        return self.is_online

    @staticmethod
    def cleanup():
        logging.info(f"TeslaApi: Topic housekeeping before exit...")
        publish_message("Tesla/vehicle0/battery_soc", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/battery_soc_setpoint", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/charging_amps", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/charging_status", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/charging_watts", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/is_charging", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/is_home", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/is_supercharging", payload=f"", qos=0, retain=False)
        publish_message("Tesla/vehicle0/plugged_status", payload=f"", qos=0, retain=False)
