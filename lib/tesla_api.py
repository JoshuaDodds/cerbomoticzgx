import teslapy
import math
import time
import paho.mqtt.publish as publish

import lib.helpers

from lib.constants import logging, dotenv_config, cerboGxEndpoint
from lib.domoticz_updater import domoticz_update

retry = teslapy.Retry(total=2, status_forcelist=(500, 502, 503, 504))
email = dotenv_config("TESLA_EMAIL")

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

        # self.update_vehicle_status()

    def __del__(self):
        self.cleanup()
        logging.info(f"TeslaApi (__del__): Exiting...")

    def update_vehicle_status(self, force=False):
        if (not self.last_update_ts
            or time.localtime() >= time.localtime(self.last_update_ts + (60 * 15))
            or self.is_charging
            or force):

            logging.info(f"TeslaApi: retrieving latest vehicle state... Last update was at: {self.last_update_ts_hr}")
            self.get_vehicle_name()
            self.battery_soc()
            self.battery_soc_setpoint()
            self.is_vehicle_online()
            self.is_vehicle_charging()
            self.is_vehicle_supercharging()
            self.is_vehicle_plugged()
            self.is_vehicle_home()
            self.charge_current_request()
            self.minutes_to_full_charge()
            self.is_max_soc_reached()
            self.last_update_ts = self.get_vehicle_data().timestamp
            self.last_update_ts_hr = time.strftime('%H:%M:%S', time.localtime(self.last_update_ts))

            self.update_mqtt_and_domoticz()

        else:
            logging.info(f"TeslaApi: Last vehicle status update was at: {self.last_update_ts_hr}. Skipping new request to mothership (Tesla API)")

    def update_mqtt_and_domoticz(self):
        publish.single("Tesla/vehicle0/vehicle_name", payload=f"{{\"value\": \"{self.vehicle_name}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/charging_status", payload=f"{{\"value\": \"{self.charging_status}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/battery_soc", payload=f"{{\"value\": \"{self.vehicle_soc}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/battery_soc_setpoint", payload=f"{{\"value\": \"{self.vehicle_soc_setpoint}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/plugged_status", payload=f"{{\"value\": \"{self.plugged_status}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/is_home", payload=f"{{\"value\": \"{self.is_home}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/is_supercharging", payload=f"{{\"value\": \"{self.is_supercharging}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/time_until_full", payload=f"{{\"value\": \"{self.time_until_full}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/is_charging", payload=f"{{\"value\": \"{self.is_charging}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        # send selected metrics to domoticz for tracking and display
        _msg = f"{self.charging_status} @ {self.charging_amp_limit}A, {self.vehicle_soc}% of {self.vehicle_soc_setpoint}%, {self.plugged_status}"
        domoticz_update('vehicle_status', _msg, "received vehicle metrics update from EvCharger and sent to domoticz")

    # Command Wrappers
    def set_charge(self, amps, error_msg):
        self.wake_vehicle()
        try:
            with teslapy.Tesla(email, retry=retry) as tesla:
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
            with teslapy.Tesla(email, retry=retry) as tesla:
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
            with teslapy.Tesla(email, retry=retry) as tesla:
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
    def stop_tesla_charge(self): return self.send_command('STOP_CHARGE', "Error stopping Tesla charge")

    def start_tesla_charge(self): return self.send_command('START_CHARGE', "Error starting Tesla charge")

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
            with teslapy.Tesla(email, retry=retry) as tesla:
                vehicles = tesla.vehicle_list()
                vehicles[0].get_vehicle_summary()
                if 'online' in vehicles[0]['state']:
                    vehicles[0].get_vehicle_data()
                    return vehicles[0]
                else:
                    logging.info(f"tesla_api: vehicle is sleeping. waking vehicle to service request...")
                    self.wake_vehicle()
                    vehicles[0].get_vehicle_data()
                    return vehicles[0]

        except Exception as e:
            logging.info(f"tesla_api: get_vehicle_data() rrror: {e}")
            return e

    def minutes_to_full_charge(self) -> str:
        minutes_until_full = self.get_vehicle_data()['charge_state']['minutes_to_full_charge'] if self.is_charging else "N/A"
        self.time_until_full = lib.helpers.convert_to_fractional_hour(minutes_until_full)
        self.update_mqtt_and_domoticz()
        return self.time_until_full

    def is_vehicle_charging(self):
        self.is_charging = self.get_vehicle_data()['charge_state']['charging_state'] == 'Charging'
        self.charging_status = "Charging" if self.is_charging else "Idle"
        self.update_mqtt_and_domoticz()
        return self.is_charging

    def is_vehicle_plugged(self):
        self.is_plugged = self.get_vehicle_data()['charge_state']['charge_port_latch'] == 'Engaged'
        self.plugged_status = "Plugged" if self.is_plugged else "Unplugged"
        self.update_mqtt_and_domoticz()
        return self.is_plugged

    def is_max_soc_reached(self):
        self.is_full = self.get_vehicle_data()['charge_state']['battery_level'] >= self.get_vehicle_data()['charge_state']['charge_limit_soc']
        self.update_mqtt_and_domoticz()
        return self.is_full

    def battery_soc_setpoint(self):
        self.vehicle_soc_setpoint = self.get_vehicle_data()['charge_state']['charge_limit_soc']
        self.update_mqtt_and_domoticz()
        return self.vehicle_soc_setpoint

    def battery_soc(self):
        self.vehicle_soc = self.get_vehicle_data()['charge_state']['battery_level']
        self.update_mqtt_and_domoticz()
        return self.vehicle_soc

    def charge_current_request(self):
        self. charging_amp_limit = self.get_vehicle_data()['charge_state']['charge_current_request']
        return self.charging_amp_limit

    def get_vehicle_name(self):
        self.vehicle_name = self.get_vehicle_data()['vehicle_state']['vehicle_name']
        return self.vehicle_name

    def is_vehicle_home(self):
        lat = round(float(dotenv_config('HOME_ADDRESS_LAT')), 3)
        long = round(float(dotenv_config('HOME_ADDRESS_LONG')), 3)

        if round(self.get_vehicle_data()['drive_state']['latitude'], 3) == lat and \
           round(self.get_vehicle_data()['drive_state']['longitude'], 3) == long:
            self.is_home = True
            self.update_mqtt_and_domoticz()
        else:
            self.is_home = False
            self.update_mqtt_and_domoticz()
        return self.is_home

    def is_vehicle_supercharging(self):
        self.is_supercharging = self.get_vehicle_data()['charge_state']['fast_charger_present']
        self.update_mqtt_and_domoticz()
        return self.is_supercharging

    def is_vehicle_online(self):
        self.is_online = self.get_vehicle_data().available()
        return self.is_online

    @staticmethod
    def cleanup():
        logging.info(f"TeslaApi: Topic housekeeping before exit...")
        publish.single("Tesla/vehicle0/battery_soc", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/battery_soc_setpoint", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/charging_amps", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/charging_status", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/charging_watts", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/is_charging", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/is_home", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/is_supercharging", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tesla/vehicle0/plugged_status", payload=f"", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
