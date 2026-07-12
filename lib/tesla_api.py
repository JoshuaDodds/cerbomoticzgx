import math
import re
import threading
import time

import requests

import lib.helpers

from lib.config_paths import secrets_path
from lib.global_state import GlobalStateClient
from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.domoticz_updater import domoticz_update
from lib.helpers import publish_message
from lib.tesla_budget import default_budget

STATE = GlobalStateClient()

TIMEOUT = 25
TESLA_AUTH_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
VEHICLE_DATA_ENDPOINTS = "charge_state;drive_state;vehicle_state;location_data"

# Poll cadence (minutes). We NEVER wake the car to read status, so these are just the
# minimum spacing between reads. When the car is asleep we back off hard: a sleeping car
# isn't charging, and plugging in wakes it, so a slow discovery poll is all that's needed.
DEFAULT_POLL_INTERVAL_MIN = 15
DEFAULT_POLL_INTERVAL_CHARGING_MIN = 10
DEFAULT_POLL_INTERVAL_ASLEEP_MIN = 30

logging.getLogger('urllib3').setLevel(logging.WARNING)


def _setting_int(name, default):
    raw = retrieve_setting(name)
    try:
        return int(raw) if raw not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


class TeslaApi:
    def __init__(self):
        logging.info(f"TeslaApi (__init__): Initializing...")

        self._client_id = retrieve_setting("TESLA_FLEET_CLIENT_ID")
        self._client_secret = retrieve_setting("TESLA_FLEET_CLIENT_SECRET")
        self._refresh_token = retrieve_setting("TESLA_FLEET_REFRESH_TOKEN")
        self._base_url = retrieve_setting("TESLA_FLEET_API_BASE_URL")
        self._vehicle_id = retrieve_setting("TESLA_FLEET_VEHICLE_ID")

        self._access_token = None
        self._token_expires_at = 0
        self._token_lock = threading.Lock()

        # Hard spend guard: every billable Fleet API call gates on this, so the account
        # can never exceed Tesla's $10/month credit no matter how the loop behaves.
        self._budget = default_budget()

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
        self._last_read_attempt_ts = 0   # throttle on the attempt, so asleep reads back off too
        self._asleep = False

        self.update_init = threading.Thread(target=self.update_vehicle_status, daemon=True)
        self.update_init.start()

        logging.info(f"TeslaApi: Init complete.")

    def __del__(self):
        self.cleanup()
        logging.info(f"TeslaApi (__del__): Exiting...")

    def _poll_interval_seconds(self):
        if getattr(self, '_asleep', False):
            minutes = _setting_int('TESLA_POLL_INTERVAL_ASLEEP_MIN', DEFAULT_POLL_INTERVAL_ASLEEP_MIN)
        elif self.is_charging:
            minutes = _setting_int('TESLA_POLL_INTERVAL_CHARGING_MIN', DEFAULT_POLL_INTERVAL_CHARGING_MIN)
        else:
            minutes = _setting_int('TESLA_POLL_INTERVAL_MIN', DEFAULT_POLL_INTERVAL_MIN)
        return max(60, minutes * 60)

    def update_vehicle_status(self, force=False, allow_wake=False):
        """Refresh cached vehicle state from ONE cheap data read.

        Throttled on the last ATTEMPT (not just the last success), so an asleep car — which
        returns no data — still backs off to the long asleep interval instead of re-polling
        every loop. By default we never wake the car to read; ``allow_wake`` is set only when
        there is explicit intent to charge (the caller wants to check + act), and even then
        the wake is budget-capped.
        """
        last = self._last_read_attempt_ts
        due = (not last) or (time.time() - last >= self._poll_interval_seconds())
        if not (force or due):
            return

        self._last_read_attempt_ts = time.time()   # count the attempt even if it yields nothing
        vehicle_data = self.get_vehicle_data(allow_wake=allow_wake)
        if not vehicle_data:
            self._asleep = True
            logging.debug(f"TeslaApi: no fresh data (asleep or budget-limited); keeping last state from {self.last_update_ts_hr}.")
            return
        self._asleep = False

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
        self.last_update_ts = time.time()
        self.last_update_ts_hr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.last_update_ts))
        self.update_mqtt_and_domoticz()

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

    # Fleet API auth / transport
    def _refresh_access_token(self):
        response = requests.post(
            TESLA_AUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        token_data = response.json()
        self._access_token = token_data["access_token"]
        self._token_expires_at = time.time() + token_data.get("expires_in", 28800) - 60
        # Tesla rotates the refresh token on each use - the old one is invalidated,
        # so it must be persisted or the next restart will fail to authenticate.
        new_refresh_token = token_data.get("refresh_token", self._refresh_token)
        if new_refresh_token != self._refresh_token:
            self._refresh_token = new_refresh_token
            self._persist_tokens()

    def _persist_tokens(self):
        try:
            path = secrets_path()
            with open(path) as f:
                content = f.read()
            content = re.sub(r'TESLA_FLEET_ACCESS_TOKEN="[^"]*"', f'TESLA_FLEET_ACCESS_TOKEN="{self._access_token}"', content)
            content = re.sub(r'TESLA_FLEET_REFRESH_TOKEN="[^"]*"', f'TESLA_FLEET_REFRESH_TOKEN="{self._refresh_token}"', content)
            with open(path, "w") as f:
                f.write(content)
        except OSError as e:
            logging.info(f"tesla_api: could not persist refreshed Fleet API tokens to {secrets_path()}: {e}")

    def _get_access_token(self):
        with self._token_lock:
            if not self._access_token or time.time() >= self._token_expires_at:
                self._refresh_access_token()
            return self._access_token

    def _request(self, method, path, retry_on_auth_failure=True, **kwargs):
        token = self._get_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        response = requests.request(
            method, f"{self._base_url}{path}", headers=headers, timeout=TIMEOUT, **kwargs
        )

        if response.status_code == 401 and retry_on_auth_failure:
            with self._token_lock:
                self._access_token = None
            return self._request(method, path, retry_on_auth_failure=False, **kwargs)

        return response

    def _get_vehicle_state(self):
        # Billable "data" request (vehicle list/state). Gate it; return None if capped.
        if not self._budget.spend("data"):
            return None
        response = self._request("GET", f"/api/1/vehicles/{self._vehicle_id}")
        return (response.json().get("response") or {}).get("state")

    def _command(self, name, error_msg, json_body=None):
        ok, _cat = self._command_ex(name, json_body=json_body, error_msg=error_msg)
        return ok

    def _command_ex(self, name, json_body=None, error_msg=""):
        """Send a command and classify the outcome so callers can react appropriately.

        Returns (success, category) where category is one of:
          'ok'       — command accepted;
          'budget'   — blocked by the spend guard;
          'asleep'   — car / command bus is asleep (a wake + retry can fix it);
          'network'  — transport failure or 5xx (waking won't help; may need manual action);
          'error'    — some other rejection.
        """
        if not self._budget.spend("command"):
            logging.info(f"tesla_api: command '{name}' blocked by budget guard (daily cap reached). {error_msg}")
            return False, 'budget'
        try:
            response = self._request(
                "POST", f"/api/1/vehicles/{self._vehicle_id}/command/{name}", json=json_body or {}
            )
        except requests.exceptions.RequestException as e:
            logging.info(f"tesla_api: command '{name}' network error: {e}. {error_msg}")
            return False, 'network'
        try:
            data = response.json()
        except ValueError:
            data = {}
        result = data.get("response") or {}
        if response.status_code == 200 and result.get("result"):
            return True, 'ok'
        reason = str(result.get('reason') or data.get('error') or '').lower()
        logging.info(f"tesla_api: command '{name}' failed: {reason or response.status_code}. {error_msg}")
        if response.status_code == 408 or any(k in reason for k in
                ('could_not_wake', 'asleep', 'unavailable', 'offline', 'timed out')):
            return False, 'asleep'
        if response.status_code >= 500:
            return False, 'network'
        return False, 'error'

    # Command Wrappers
    def set_charge(self, amps, error_msg):
        self.wake_vehicle()
        success = self._command("set_charging_amps", error_msg, {"charging_amps": amps})
        if success:
            self.charging_amp_limit = amps
            self.update_mqtt_and_domoticz()
        return success

    def send_command(self, cmd, error_msg):
        self.wake_vehicle()
        fleet_command = {"START_CHARGE": "charge_start", "STOP_CHARGE": "charge_stop"}.get(cmd, cmd)
        success = self._command(fleet_command, error_msg)

        if success:
            if 'START_CHARGE' in cmd:
                self.is_charging = True
                self.charging_status = "Charging"
                self.update_vehicle_status(force=True)
            if 'STOP_CHARGE' in cmd:
                self.is_charging = False
                self.time_until_full = "N/A"
                self.charging_status = "Idle"
                self.update_vehicle_status(force=True)

        return success

    def wake_vehicle(self, skip_online_check=False):
        """Wake the car — used before a command, never just to read status.

        A wake is the most expensive call ($0.02), so it is budget-gated and the confirm
        polls are bounded. ``skip_online_check`` forces a wake_up even when the car reports
        'online' — needed when a command was rejected with could_not_wake_buses (the car is
        online but its command bus is asleep).
        """
        try:
            if not skip_online_check and self._get_vehicle_state() == "online":
                return True

            if not self._budget.spend("wake"):
                logging.info("tesla_api: wake blocked by budget guard (daily cap reached).")
                return False

            self._request("POST", f"/api/1/vehicles/{self._vehicle_id}/wake_up")

            for _ in range(3):   # bounded confirm-polls (each a gated data call)
                time.sleep(3)
                if self._get_vehicle_state() == "online":
                    return True

            return False

        except requests.exceptions.RequestException as e:
            logging.info(f"tesla_api: HTTPError: {e}")
            return False

    def _on_charge_stopped(self):
        """Reflect a confirmed stop in cached state + retained MQTT (no extra API read)."""
        self.is_charging = False
        self.time_until_full = "N/A"
        self.charging_status = "Idle"
        self.charging_amp_limit = 0
        self.update_mqtt_and_domoticz()

    def stop_charge_robust(self):
        """Stop charging with escalation. Returns 'ok' | 'network' | 'failed'.

        A charging car can pull a lot of energy quickly, so if the first stop is rejected
        because the command bus is asleep, we FORCE a wake and retry immediately. A genuine
        network/transport failure is reported back ('network') so the caller can alert for
        manual intervention rather than silently leaving the car charging.
        """
        ok, cat = self._command_ex('charge_stop', error_msg="stop charge")
        if ok:
            self._on_charge_stopped()
            return 'ok'
        if cat == 'asleep':
            if self.wake_vehicle(skip_online_check=True):
                ok, cat = self._command_ex('charge_stop', error_msg="stop charge (after wake)")
                if ok:
                    self._on_charge_stopped()
                    return 'ok'
            return 'network' if cat == 'network' else 'failed'
        return 'network' if cat == 'network' else 'failed'

    # Commands
    def stop_tesla_charge(self):
        STATE.set('tesla_charge_requested', "False")
        return self.stop_charge_robust()

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
    def get_vehicle_data(self, allow_wake=False):
        """Fetch vehicle data in a SINGLE billable read.

        No separate 'is it online?' pre-check (that was a second data call every poll) and
        no wake-to-read: a sleeping car returns HTTP 408, which we treat as "no fresh data"
        unless ``allow_wake`` is set (only command flows do that). Budget-gated.
        """
        try:
            if not self._budget.spend("data"):
                logging.info("tesla_api: vehicle_data read blocked by budget guard (daily cap reached).")
                return None

            response = self._request(
                "GET", f"/api/1/vehicles/{self._vehicle_id}/vehicle_data",
                params={"endpoints": VEHICLE_DATA_ENDPOINTS},
            )

            if response.status_code == 408:   # vehicle asleep / offline
                if not (allow_wake and self.wake_vehicle()):
                    logging.debug("tesla_api: vehicle asleep; not waking just to read status.")
                    return None
                if not self._budget.spend("data"):
                    return None
                response = self._request(
                    "GET", f"/api/1/vehicles/{self._vehicle_id}/vehicle_data",
                    params={"endpoints": VEHICLE_DATA_ENDPOINTS},
                )

            data = response.json()
            if not data.get("response"):
                logging.error(f"tesla_api: get_vehicle_data() error: {data.get('error')}")
                return None

            return data["response"]

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

        except KeyError:
            logging.info("TeslaApi: KeyError in accessing vehicle data.")
            self.is_home = None

        return self.is_home

    def is_vehicle_supercharging(self, vehicle_data):
        self.is_supercharging = vehicle_data['charge_state']['fast_charger_present'] or False
        self.update_mqtt_and_domoticz()
        return self.is_supercharging

    def is_vehicle_online(self, vehicle_data):
        self.is_online = vehicle_data.get('state') == 'online'
        return self.is_online

    @staticmethod
    def cleanup():
        # Intentionally does NOT clear the retained Tesla/vehicle0/* state topics on shutdown.
        # They are retained so the dashboards show the last-known vehicle state immediately
        # after a restart (the Vehicle tab shows `last_update_at` so its age is obvious),
        # instead of appearing to know nothing until the next successful poll. The control
        # loop refreshes them when the car is next reachable. (Previously this wiped every
        # topic on each restart, which blanked the EV card / Vehicle tab.)
        logging.info("TeslaApi: shutdown — preserving retained vehicle state topics for the dashboard.")
