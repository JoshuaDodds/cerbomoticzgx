import base64
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from dotenv import dotenv_values

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
TESLA_AUTH_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
VEHICLE_DATA_ENDPOINTS = "charge_state;drive_state;vehicle_state;location_data"
TOKEN_REFRESH_SKEW_S = 60
AUTH_FAILURE_BACKOFF_S = 15 * 60
AUTH_TRANSIENT_BACKOFF_S = 60
TESLA_WEEKDAY_NAMES = ("SUN", "MON", "TUES", "WED", "THURS", "FRI", "SAT")

# Poll cadence (minutes). We NEVER wake the car to read status, so these are just the
# minimum spacing between reads. When the car is asleep we back off hard: a sleeping car
# isn't charging, and plugging in wakes it, so a slow discovery poll is all that's needed.
DEFAULT_POLL_INTERVAL_MIN = 15
DEFAULT_POLL_INTERVAL_CHARGING_MIN = 10
DEFAULT_POLL_INTERVAL_ASLEEP_MIN = 30
# In telemetry mode, a refresh within this window is treated as proof the car is online (it's
# actively streaming), so the pre-command billable state read can be skipped entirely (audit M3).
TELEMETRY_ONLINE_MAX_AGE_S = 300

logging.getLogger('urllib3').setLevel(logging.WARNING)


class TeslaAuthenticationError(requests.exceptions.RequestException):
    """Sanitized OAuth failure which never includes credentials or response bodies."""

    def __init__(self, status_code=0, error_code="authentication_failed",
                 fleet_response_received=False):
        self.status_code = int(status_code or 0)
        self.fleet_response_received = bool(fleet_response_received)
        normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", str(error_code or ""))
        self.error_code = normalized[:80] or "authentication_failed"
        super().__init__(
            f"Tesla Fleet authentication failed (HTTP {self.status_code or 'unknown'}, "
            f"{self.error_code})"
        )


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

        self._access_token = retrieve_setting("TESLA_FLEET_ACCESS_TOKEN") or None
        self._token_expires_at = self._access_token_expiry(self._access_token)
        self._token_lock = threading.Lock()
        self._auth_retry_after = 0
        self._auth_failure_code = None
        self._auth_failure_status = 0
        self._last_auth_log_signature = None
        self._last_auth_log_ts = 0

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
        self.charge_limit_update_ts = 0
        self.charge_current_request_update_ts = 0
        self.charge_state_update_ts = 0
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

    def _telemetry_on(self) -> bool:
        """Fleet Telemetry (push) mode. Read dynamically so toggling the .env flag takes effect
        on the next tick without a restart."""
        return lib.helpers.is_truthy(retrieve_setting("TESLA_TELEMETRY_ENABLED"), False)

    def _refresh_from_telemetry(self):
        """Telemetry mode: the fleet-telemetry bridge pushes fresh state onto the tesla_* STATE
        keys, so we read those instead of making a billable vehicle_data call. NO REST, NO wake,
        zero cost. Commands (start/stop/set amps) still go via the Fleet API."""
        def _f(key):
            try:
                return float(STATE.get(key))
            except (TypeError, ValueError):
                return None

        soc, setp = _f("tesla_soc"), _f("tesla_soc_setpoint")
        if soc is not None:
            self.vehicle_soc = soc
        if setp is not None:
            self.vehicle_soc_setpoint = setp
        if soc is not None and setp is not None:
            self.is_full = soc >= setp
        amp_req = _f("tesla_charge_current_request")
        if amp_req is not None:
            self.charging_amp_limit = amp_req
        limit_updated = _f("tesla_soc_setpoint_updated_at")
        current_updated = _f("tesla_charge_current_request_updated_at")
        state_updated = _f("tesla_charge_state_updated_at")
        if limit_updated is not None:
            self.charge_limit_update_ts = limit_updated
        if current_updated is not None:
            self.charge_current_request_update_ts = current_updated
        if state_updated is not None:
            self.charge_state_update_ts = state_updated

        self.is_charging = lib.helpers.is_truthy(STATE.get("tesla_is_charging"), False)
        self.is_plugged = lib.helpers.is_truthy(STATE.get("tesla_is_plugged"), False)
        self.is_supercharging = lib.helpers.is_truthy(STATE.get("tesla_is_supercharging"), False)
        # is_home may be genuinely unknown until the car streams a Location; keep None so the
        # controller stays conservative rather than assuming home.
        home = STATE.get("tesla_is_home")
        self.is_home = None if home in (None, "", "None") else lib.helpers.is_truthy(home, False)
        ttf = STATE.get("tesla_time_to_full")
        if self.is_charging and ttf:
            self.time_until_full = ttf
        else:
            self.time_until_full = "N/A"
            if ttf not in (None, 0, "", "N/A"):
                # Reconcile a retained ETA left by an older process/version even
                # when the change-driven vehicle stream is currently quiet.
                STATE.set("tesla_time_to_full", "N/A")
                publish_message(
                    "Tesla/vehicle0/time_until_full",
                    payload='{"value": "N/A"}', qos=0, retain=True)

        self.plugged_status = "Plugged" if self.is_plugged else "Unplugged"
        self.charging_status = "Charging" if self.is_charging else "Idle"
        self.is_online = True                      # an actively-streaming car is by definition online
        self._asleep = False
        telemetry_updated = _f("tesla_telemetry_last_update_ts")
        if telemetry_updated is not None:
            self.last_update_ts = telemetry_updated
        elif not hasattr(self, "last_update_ts"):
            self.last_update_ts = 0
        self.last_update_ts_hr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.last_update_ts))

    def update_vehicle_status(self, force=False, allow_wake=False):
        """Refresh cached vehicle state from ONE cheap data read.

        In telemetry (push) mode we read the bridge-maintained STATE keys and skip the REST
        call entirely (no billable request, no wake). Otherwise we fall back to the throttled
        vehicle_data poll: throttled on the last ATTEMPT (not just the last success), so an
        asleep car — which returns no data — still backs off to the long asleep interval instead
        of re-polling every loop. By default we never wake the car to read; ``allow_wake`` is set
        only when there is explicit intent to charge, and even then the wake is budget-capped.
        """
        if self._telemetry_on():
            self._refresh_from_telemetry()
            return

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
        self.charge_limit_update_ts = self.last_update_ts
        self.charge_current_request_update_ts = self.last_update_ts
        self.charge_state_update_ts = self.last_update_ts
        self.last_update_ts_hr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.last_update_ts))
        self.update_mqtt_and_domoticz()

    def update_mqtt_and_domoticz(self):
        # In telemetry (push) mode the fleet-telemetry bridge OWNS the Tesla/vehicle0/* topics
        # (fresher + retained). Re-publishing them here from our last cached snapshot would fight
        # the bridge and can flap stale values (plugged/home/soc), so skip the MQTT writes and
        # only update Domoticz. In legacy polling mode we remain the sole publisher.
        if self._telemetry_on():
            self._domoticz_vehicle_status()
            return
        self._publish_vehicle_topics()
        self._domoticz_vehicle_status()

    def _publish_vehicle_topics(self):
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

    def _domoticz_vehicle_status(self):
        # send selected metrics to domoticz for tracking and display
        _msg = f"{self.charging_status} @ {self.charging_amp_limit}A, {self.vehicle_soc}% of {self.vehicle_soc_setpoint}%, {self.plugged_status}"
        domoticz_update('vehicle_status', _msg, "received vehicle metrics update from EvCharger and sent to domoticz")

    # Fleet API auth / transport
    @staticmethod
    def _access_token_expiry(token) -> float:
        """Read a JWT expiry without trusting or logging any token content."""
        try:
            encoded_payload = str(token).split(".")[1]
            encoded_payload += "=" * (-len(encoded_payload) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded_payload))
            expiry = float(payload.get("exp", 0))
            return expiry if math.isfinite(expiry) and expiry > 0 else 0
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            return 0

    @staticmethod
    def _oauth_error_code(response) -> str:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        raw = payload.get("error") or "authentication_failed"
        normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", str(raw))
        return normalized[:80] or "authentication_failed"

    def _refresh_access_token(self):
        # A one-off setup process may have rotated and persisted the refresh token since this
        # controller was constructed. Re-read only this credential immediately before use so
        # separate processes cannot strand each other with a stale in-memory token.
        try:
            latest = dotenv_values(secrets_path()).get("TESLA_FLEET_REFRESH_TOKEN")
        except OSError:
            latest = None
        if latest:
            self._refresh_token = latest
        if not self._client_id or not self._refresh_token:
            raise TeslaAuthenticationError(0, "missing_refresh_credentials")

        try:
            response = requests.post(
                TESLA_AUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "refresh_token": self._refresh_token,
                },
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as error:
            raise TeslaAuthenticationError(
                0, "auth_service_unavailable") from error
        status_code = int(getattr(response, "status_code", 0) or 0)
        if 400 <= status_code < 500:
            raise TeslaAuthenticationError(status_code, self._oauth_error_code(response))
        if status_code >= 500:
            raise TeslaAuthenticationError(status_code, "auth_service_unavailable")
        try:
            token_data = response.json()
        except (TypeError, ValueError) as error:
            raise TeslaAuthenticationError(status_code, "invalid_token_response") from error
        if not isinstance(token_data, dict) or not token_data.get("access_token"):
            raise TeslaAuthenticationError(status_code, "missing_access_token")

        try:
            expires_in = float(token_data.get("expires_in", 28800))
        except (TypeError, ValueError):
            expires_in = 28800
        if not math.isfinite(expires_in) or expires_in <= 0:
            expires_in = 28800

        self._access_token = str(token_data["access_token"])
        self._token_expires_at = time.time() + expires_in
        # Refresh tokens are rotated. Persist both values after every successful exchange so
        # a restart uses the newest pair even if Tesla omitted or reused refresh_token.
        self._refresh_token = str(token_data.get("refresh_token") or self._refresh_token)
        self._persist_tokens()

    def _persist_tokens(self):
        """Atomically replace only Tesla token values while preserving every other secret."""
        temporary_path = None
        try:
            path = Path(secrets_path())
            content = path.read_text(encoding="utf-8")
            original_mode = stat.S_IMODE(path.stat().st_mode)

            def replace_or_append(source, key, value):
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                pattern = re.compile(
                    rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*).*$",
                    re.MULTILINE,
                )
                replacement = lambda match: f'{match.group("prefix")}"{escaped}"'
                updated, count = pattern.subn(replacement, source)
                if count:
                    return updated
                separator = "" if not updated or updated.endswith("\n") else "\n"
                return f'{updated}{separator}{key}="{escaped}"\n'

            content = replace_or_append(
                content, "TESLA_FLEET_ACCESS_TOKEN", self._access_token)
            content = replace_or_append(
                content, "TESLA_FLEET_REFRESH_TOKEN", self._refresh_token)

            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=path.parent,
                    prefix=f".{path.name}.", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, path)
            temporary_path = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The file itself is already flushed and replaced; some mounted filesystems
                # simply do not support syncing a directory descriptor.
                pass

            # retrieve_setting caches .secrets by path. Keep that cache coherent so another
            # TeslaApi constructed in this process cannot reuse the rotated refresh token.
            cached = getattr(retrieve_setting, "_secrets", None)
            cached_path = getattr(retrieve_setting, "_secrets_path", None)
            if isinstance(cached, dict) and cached_path == str(path):
                cached["TESLA_FLEET_ACCESS_TOKEN"] = self._access_token
                cached["TESLA_FLEET_REFRESH_TOKEN"] = self._refresh_token
            return True
        except OSError as error:
            logging.error(
                "tesla_api: could not atomically persist refreshed Fleet API tokens "
                "to %s: %s", secrets_path(), error)
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _log_auth_failure(self, context: str, error: TeslaAuthenticationError) -> None:
        now = time.time()
        signature = (context, error.error_code)
        if (signature == getattr(self, "_last_auth_log_signature", None)
                and now - getattr(self, "_last_auth_log_ts", 0) < AUTH_FAILURE_BACKOFF_S):
            return
        self._last_auth_log_signature = signature
        self._last_auth_log_ts = now
        if error.error_code == "login_required":
            logging.error(
                "tesla_api: %s authentication failed (login_required); Tesla account "
                "reauthorization is required if this persists on the Fleet Auth endpoint.",
                context)
        else:
            logging.error(
                "tesla_api: %s authentication failed (%s, HTTP %s).",
                context, error.error_code, error.status_code or "unknown")

    def _get_access_token(self):
        with self._token_lock:
            now = time.time()
            if (self._access_token
                    and now + TOKEN_REFRESH_SKEW_S < self._token_expires_at):
                return self._access_token
            if now < getattr(self, "_auth_retry_after", 0):
                raise TeslaAuthenticationError(
                    getattr(self, "_auth_failure_status", 0),
                    getattr(self, "_auth_failure_code", None) or "auth_backoff")
            try:
                self._refresh_access_token()
            except TeslaAuthenticationError as error:
                self._auth_failure_code = error.error_code
                self._auth_failure_status = error.status_code
                backoff = (AUTH_TRANSIENT_BACKOFF_S
                           if error.error_code == "auth_service_unavailable"
                           else AUTH_FAILURE_BACKOFF_S)
                self._auth_retry_after = now + backoff
                raise
            self._auth_failure_code = None
            self._auth_failure_status = 0
            self._auth_retry_after = 0
            return self._access_token

    def _request(self, method, path, retry_on_auth_failure=True,
                 auth_retry_budget=None, auth_retry_critical=None, **kwargs):
        if auth_retry_budget is None:
            if path.endswith("/wake_up"):
                auth_retry_budget = "wake"
            elif "/command/" in path or path.endswith("/fleet_telemetry_config"):
                auth_retry_budget = "command"
            else:
                auth_retry_budget = "data"
        if auth_retry_critical is None:
            auth_retry_critical = path.endswith("/command/charge_stop")
        token = self._get_access_token()
        headers = dict(kwargs.pop("headers", {}))
        headers.pop("Authorization", None)
        request_headers = {**headers, "Authorization": f"Bearer {token}"}
        response = requests.request(
            method, f"{self._base_url}{path}", headers=request_headers,
            timeout=TIMEOUT, **kwargs
        )

        if response.status_code == 401 and retry_on_auth_failure:
            with self._token_lock:
                # Another request may already have refreshed while this one was in flight.
                if self._access_token == token:
                    self._access_token = None
                    self._token_expires_at = 0
            try:
                self._get_access_token()
            except TeslaAuthenticationError as error:
                error.fleet_response_received = True
                raise
            # The original 401 and retry are both billable Fleet calls. Reserve the retry
            # separately so an auth recovery cannot silently bypass the monthly guard.
            if (auth_retry_budget
                    and not self._budget.spend(
                        auth_retry_budget, critical=auth_retry_critical)):
                logging.info(
                    "tesla_api: authenticated %s retry blocked by budget guard.",
                    auth_retry_budget)
                return response
            return self._request(
                method, path, retry_on_auth_failure=False,
                auth_retry_budget=auth_retry_budget,
                auth_retry_critical=auth_retry_critical,
                headers=headers, **kwargs)

        return response

    def _get_vehicle_state(self):
        # Billable "data" request (vehicle list/state). Gate it; return None if capped.
        if not self._budget.spend("data"):
            return None
        try:
            response = self._request("GET", f"/api/1/vehicles/{self._vehicle_id}")
        except TeslaAuthenticationError as error:
            self._log_auth_failure("vehicle-state", error)
            if not error.fleet_response_received:
                self._budget.refund("data")
            return None
        except requests.exceptions.RequestException:
            self._budget.refund("data")        # no HTTP response -> not billed
            return None
        if response.status_code >= 500:
            self._budget.refund("data")        # Tesla does not bill responses >= 500
            return None
        return (response.json().get("response") or {}).get("state")

    def _command(self, name, error_msg, json_body=None):
        ok, _cat = self._command_ex(name, json_body=json_body, error_msg=error_msg)
        return ok

    def _command_ex(self, name, json_body=None, error_msg="", critical=False,
                    accepted_reasons=(), preserve_accepted_reason=False):
        """Send a command and classify the outcome so callers can react appropriately.

        ``critical=True`` (e.g. charge_stop) bypasses the spend guard — a safety-essential
        command must never be blocked by the budget.

        ``accepted_reasons`` lets an idempotent wrapper treat a documented response such as
        ``already_set`` as success without weakening command handling globally. A caller which
        must distinguish an applied mutation from an already-absent object may request the
        normalized accepted reason as the successful category.

        Returns (success, category) where category is one of:
          'ok'       — command accepted;
          'budget'   — blocked by the spend guard;
          'asleep'   — car / command bus is asleep (a wake + retry can fix it);
          'auth'     — OAuth refresh failed or account reauthorization is required;
          'network'  — transport failure or 5xx (waking won't help; may need manual action);
          'unsupported' — this vehicle/firmware does not support the command;
          'error'    — some other rejection.
        """
        if not self._budget.spend("command", critical=critical):
            logging.info(f"tesla_api: command '{name}' blocked by budget guard (monthly ceiling reached). {error_msg}")
            return False, 'budget'
        try:
            response = self._request(
                "POST", f"/api/1/vehicles/{self._vehicle_id}/command/{name}",
                json=json_body or {}
            )
        except TeslaAuthenticationError as error:
            self._log_auth_failure(f"command '{name}'", error)
            if not error.fleet_response_received:
                self._budget.refund("command")
            return False, 'auth'
        except requests.exceptions.RequestException as e:
            logging.info(f"tesla_api: command '{name}' network error: {e}. {error_msg}")
            self._budget.refund("command")     # no HTTP response -> Tesla did not bill it
            return False, 'network'
        try:
            data = response.json()
        except ValueError:
            data = {}
        result = data.get("response") or {}
        if response.status_code == 200 and result.get("result"):
            return True, 'ok'
        reason = str(result.get('reason') or data.get('error') or '').lower()
        normalized_reason = reason.replace('-', '_').replace(' ', '_')
        normalized_accepted = {
            str(value).lower().replace('-', '_').replace(' ', '_')
            for value in accepted_reasons
        }
        if response.status_code == 200 and normalized_reason in normalized_accepted:
            return True, (normalized_reason if preserve_accepted_reason else 'ok')
        # Not "failed" per se — the command wasn't delivered. Whether that matters depends on the
        # caller (e.g. an asleep bus on charge_stop just means the car isn't charging).
        logging.info(f"tesla_api: command '{name}' not delivered ({reason or response.status_code}). {error_msg}")
        if response.status_code >= 500:
            self._budget.refund("command")     # Tesla does not bill responses >= 500
            return False, 'network'
        if response.status_code == 408 or any(k in reason for k in
                ('could_not_wake', 'asleep', 'unavailable', 'offline', 'timed out')):
            return False, 'asleep'
        if normalized_reason in ('not_supported', 'unsupported', 'unsupported_vehicle'):
            return False, 'unsupported'
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

    def _telemetry_considers_online(self) -> bool:
        """In telemetry mode an actively-streaming car is online by definition — a recent
        telemetry refresh is proof enough, so the billable pre-command state read is skipped
        entirely (audit M3). Falls through to the real check if telemetry hasn't refreshed
        recently (e.g. the bridge/MQTT connection dropped), so this never masks a genuinely
        stale/disconnected stream."""
        if not self._telemetry_on():
            return False
        return (time.time() - (self.last_update_ts or 0)) <= TELEMETRY_ONLINE_MAX_AGE_S

    def wake_vehicle(self, skip_online_check=False, critical=False):
        """Wake the car — used before a command, never just to read status.

        A wake is the most expensive call ($0.02), so it is budget-gated and the confirm
        polls are bounded. ``skip_online_check`` forces a wake_up even when the car reports
        'online' — needed when a command was rejected with could_not_wake_buses (the car is
        online but its command bus is asleep). ``critical=True`` (a wake to deliver a
        charge_stop) bypasses the budget guard so a stop can always be delivered.
        """
        try:
            if not skip_online_check:
                if self._telemetry_considers_online():
                    return True
                if self._get_vehicle_state() == "online":
                    return True

            if not self._budget.spend("wake", critical=critical):
                logging.info("tesla_api: wake blocked by budget guard (monthly ceiling reached).")
                return False

            resp = self._request("POST", f"/api/1/vehicles/{self._vehicle_id}/wake_up")
            if getattr(resp, "status_code", 0) >= 500:
                self._budget.refund("wake")        # Tesla does not bill responses >= 500

            for _ in range(3):   # bounded confirm-polls (each a gated data call)
                time.sleep(3)
                if self._get_vehicle_state() == "online":
                    return True

            return False

        except TeslaAuthenticationError as error:
            self._log_auth_failure("wake", error)
            if not error.fleet_response_received:
                self._budget.refund("wake")
            return False
        except requests.exceptions.RequestException as e:
            logging.info(f"tesla_api: HTTPError: {e}")
            self._budget.refund("wake")        # wake_up POST failed -> not billed
            return False

    def _on_charge_stopped(self):
        """Reflect a confirmed stop in cached state + retained MQTT (no extra API read)."""
        self.is_charging = False
        self.time_until_full = "N/A"
        self.charging_status = "Idle"
        self.charging_amp_limit = 0
        if self._telemetry_on():
            # Fleet Telemetry normally owns spontaneous vehicle-state updates, but it is
            # change-driven and may omit the post-command DetailedChargeState edge. A successful
            # charge_stop response is authoritative enough to replace the stale retained UI
            # state. The bridge can still overwrite this if the car later reports otherwise.
            STATE.set("tesla_is_charging", "False")
            STATE.set("tesla_time_to_full", "N/A")
            publish_message(
                "Tesla/vehicle0/is_charging",
                payload='{"value": "False"}', qos=0, retain=True)
            publish_message(
                "Tesla/vehicle0/charging_status",
                payload='{"value": "Idle"}', qos=0, retain=True)
            publish_message(
                "Tesla/vehicle0/time_until_full",
                payload='{"value": "N/A"}', qos=0, retain=True)
        self.update_mqtt_and_domoticz()

    def stop_charge_robust(self):
        """Stop charging with escalation. Returns 'ok' | 'network' | 'failed'.

        A car can report 'could_not_wake_buses' even while it IS charging (the command bus is
        asleep but the charge continues), so we must NOT assume asleep == stopped. We FORCE a
        wake and retry. Only a confirmed result ('ok') means stopped — the CONTROLLER additionally
        verifies against the local charge meter and re-issues if the car is still drawing.
        """
        # A stop is safety-essential: bypass the spend guard for the command AND the wake needed
        # to deliver it, so the budget can never leave the car charging.
        ok, cat = self._command_ex('charge_stop', error_msg="stop charge", critical=True)
        if ok:
            self._on_charge_stopped()
            return 'ok'
        if cat == 'asleep':
            if self.wake_vehicle(skip_online_check=True, critical=True):
                ok, cat = self._command_ex('charge_stop', error_msg="stop charge (after wake)", critical=True)
                if ok:
                    self._on_charge_stopped()
                    return 'ok'
            return 'network' if cat == 'network' else 'failed'
        return 'network' if cat == 'network' else 'failed'

    def _on_charge_started(self):
        self.is_charging = True
        self.charging_status = "Charging"
        self.update_mqtt_and_domoticz()

    def start_charge_robust(self):
        """Start charging with the same asleep-bus escalation as stop. Returns 'ok'|'network'|'failed'."""
        ok, cat = self._command_ex('charge_start', error_msg="start charge")
        if ok:
            self._on_charge_started()
            return 'ok'
        if cat == 'asleep':
            if self.wake_vehicle(skip_online_check=True):
                ok, cat = self._command_ex('charge_start', error_msg="start charge (after wake)")
                if ok:
                    self._on_charge_started()
                    return 'ok'
            return 'network' if cat == 'network' else 'failed'
        return 'network' if cat == 'network' else 'failed'

    # Commands
    def stop_tesla_charge(self):
        # NOTE: does NOT touch 'tesla_charge_requested'. That flag is meant to be the USER's
        # request; our own start/stop must not mutate it, or the controller's intent latches
        # "on" forever (our start set it True -> grid-assist-off never registered).
        return self.stop_charge_robust()

    def start_tesla_charge(self):
        return self.start_charge_robust()

    def set_fleet_telemetry_config(self, config: dict):
        """Push a Fleet Telemetry config to the vehicle(s) so they stream to our receiver.
        One-off setup call (billable as a command); returns the parsed response or None."""
        if not self._budget.spend("command"):
            logging.info("tesla_api: fleet_telemetry_config blocked by budget guard (daily cap reached).")
            return None
        try:
            resp = self._request(
                "POST", "/api/1/vehicles/fleet_telemetry_config", json=config)
            return resp.json()
        except TeslaAuthenticationError as error:
            self._log_auth_failure("Fleet Telemetry configuration", error)
            if not error.fleet_response_received:
                self._budget.refund("command")
            return None
        except Exception as e:
            logging.error(f"tesla_api: fleet_telemetry_config error: {e}")
            return None

    @staticmethod
    def _bounded_integer(value, minimum, maximum):
        """Return a validated integral value, or ``None`` without coercing fractions."""
        if isinstance(value, bool):
            return None
        try:
            # Do not route identifiers through binary float: Tesla schedule IDs are uint64,
            # and float conversion silently rounded the application-owned ID before sending.
            decimal_value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
            return None
        number = int(decimal_value)
        return number if minimum <= number <= maximum else None

    def _smart_charge_command(self, name, json_body, error_msg,
                              accepted_reasons=(), preserve_accepted_reason=False):
        """Issue one planned-charge command, waking/retrying only after an asleep rejection.

        Fleet Telemetry is the state source, so these adapters never add a vehicle-data read.
        A first-attempt command avoids the billable wake/read sequence when the command bus is
        already available. An explicit asleep response gets at most one wake and one retry.
        """
        result = self._command_ex(
            name,
            json_body=json_body,
            error_msg=error_msg,
            accepted_reasons=accepted_reasons,
            preserve_accepted_reason=preserve_accepted_reason,
        )
        if result[1] != 'asleep':
            return result
        if not self.wake_vehicle(skip_online_check=True):
            return result
        return self._command_ex(
            name,
            json_body=json_body,
            error_msg=f"{error_msg} (after wake)",
            accepted_reasons=accepted_reasons,
            preserve_accepted_reason=preserve_accepted_reason,
        )

    def set_tesla_charge_limit(self, percent):
        """Set the vehicle charge limit without a state read.

        Tesla vehicles expose a 50–100% user charge-limit range. Invalid values are rejected
        locally so they cannot consume a billable command. Returns the normalized
        ``(success, category)`` command result.
        """
        percent = self._bounded_integer(percent, 50, 100)
        if percent is None:
            logging.info("tesla_api: refusing invalid charge limit; expected 50-100 percent.")
            return False, 'invalid'
        result = self._smart_charge_command(
            'set_charge_limit',
            {'percent': percent},
            f"set Tesla charge limit to {percent}%",
            accepted_reasons=('already_set',),
        )
        return result

    def upsert_owned_charge_schedule(self, schedule_id, *, start_time, end_time,
                                     days_of_week, latitude, longitude, one_time=True,
                                     enabled=True):
        """Add or update this application's deterministic Tesla charge schedule.

        Reusing the same non-zero ID updates that one schedule and avoids accumulating entries.
        Times are vehicle-local minutes after midnight. ``days_of_week`` is Tesla's seven-bit
        weekday mask. Location is mandatory because Tesla schedules are location-scoped.
        This direct Fleet command is compatible with the existing pre-2021 Model S/X path and
        performs no schedule-list read.
        """
        schedule_id = self._bounded_integer(schedule_id, 1, (2 ** 64) - 1)
        start_time = self._bounded_integer(start_time, 0, 1439)
        days_mask = self._bounded_integer(days_of_week, 1, 127)
        normalized_end = (None if end_time is None
                          else self._bounded_integer(end_time, 0, 1439))
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            latitude = longitude = math.nan
        valid_location = (
            math.isfinite(latitude) and -90 <= latitude <= 90
            and math.isfinite(longitude) and -180 <= longitude <= 180
        )
        if (schedule_id is None or start_time is None or days_mask is None
                or (end_time is not None and normalized_end is None)
                or not valid_location
                or not isinstance(one_time, bool) or not isinstance(enabled, bool)):
            logging.info("tesla_api: refusing invalid owned charge schedule payload.")
            return False, 'invalid'

        # The planner/controller uses Tesla's compact Sunday=1 ... Saturday=64 bitmap.
        # Tesla's public REST-compatible command handler expects that bitmap rendered as
        # comma-separated weekday names and converts it back internally.
        days_of_week = ",".join(
            name for bit, name in enumerate(TESLA_WEEKDAY_NAMES)
            if days_mask & (1 << bit)
        )

        payload = {
            'id': schedule_id,
            'days_of_week': days_of_week,
            'start_enabled': True,
            'start_time': start_time,
            'end_enabled': normalized_end is not None,
            'end_time': normalized_end or 0,
            'one_time': one_time,
            'enabled': enabled,
            # The Fleet command/proxy schema calls these fields lat/lon (not latitude/longitude).
            'lat': latitude,
            'lon': longitude,
        }
        return self._smart_charge_command(
            'add_charge_schedule',
            payload,
            f"add/update owned Tesla charge schedule {schedule_id}",
        )

    def remove_owned_charge_schedule(self, schedule_id):
        """Remove exactly the caller-provided application-owned schedule ID.

        There is intentionally no list-and-clear or batch-delete behavior: foreign schedules
        configured by the user or another application are never queried or touched.
        """
        schedule_id = self._bounded_integer(schedule_id, 1, (2 ** 64) - 1)
        if schedule_id is None:
            logging.info("tesla_api: refusing invalid owned charge schedule ID.")
            return False, 'invalid'
        return self._smart_charge_command(
            'remove_charge_schedule',
            {'id': schedule_id},
            f"remove owned Tesla charge schedule {schedule_id}",
            accepted_reasons=('schedule_not_found', 'not_found'),
            preserve_accepted_reason=True,
        )

    def set_tesla_charge_amps(self, amps, installation_ceiling=None):
        amps = 0 if amps < 0 else amps
        try:
            configured_max = int(float(
                18 if installation_ceiling is None else installation_ceiling))
        except (TypeError, ValueError):
            configured_max = 18
        configured_max = max(1, configured_max)
        amps = configured_max if amps > configured_max else amps
        amps = math.floor(amps)

        if amps >= 5:
            return self.set_charge(amps, f"Error setting Tesla charge current to: {amps} Amp(s)")
        if amps < 5:  # Tesla quirk: a sub-5A request takes effect only when deliberately sent twice
            first = self.set_charge(amps, f"Error setting Tesla charge current to: {amps} Amp(s)")
            second = self.set_charge(amps, f"Error setting Tesla charge current to: {amps} Amp(s)")
            return bool(first or second)
        else:
            return False

    # Metrics / Data
    def get_vehicle_data(self, allow_wake=False):
        """Fetch vehicle data in a SINGLE billable read.

        No separate 'is it online?' pre-check (that was a second data call every poll) and
        no wake-to-read: a sleeping car returns HTTP 408, which we treat as "no fresh data"
        unless ``allow_wake`` is set (only command flows do that). Budget-gated.
        """
        # Up to two attempts: a first read, then (only with allow_wake) a wake + one retry if the
        # car is asleep (408). Each read is budget-gated up front and REFUNDED if the response is
        # non-billable (network error or >= 500), so the displayed usage matches Tesla's (which
        # bills only responses < 500; 408 asleep IS billable).
        for attempt in range(2):
            if not self._budget.spend("data"):
                logging.info("tesla_api: vehicle_data read blocked by budget guard (ceiling reached).")
                return None
            try:
                response = self._request(
                    "GET", f"/api/1/vehicles/{self._vehicle_id}/vehicle_data",
                    params={"endpoints": VEHICLE_DATA_ENDPOINTS},
                )
            except TeslaAuthenticationError as error:
                self._log_auth_failure("vehicle-data", error)
                if not error.fleet_response_received:
                    self._budget.refund("data")
                return None
            except requests.exceptions.RequestException as e:
                logging.error(f"tesla_api: get_vehicle_data() network error: {e}")
                self._budget.refund("data")        # no HTTP response -> not billed
                return None

            if response.status_code >= 500:
                self._budget.refund("data")        # Tesla does not bill responses >= 500
                return None

            if response.status_code == 408:        # vehicle asleep / offline (billable)
                if attempt == 0 and allow_wake and self.wake_vehicle():
                    continue                       # woke it -> retry the read once
                logging.debug("tesla_api: vehicle asleep; not waking just to read status.")
                return None

            try:
                data = response.json()
            except ValueError:
                logging.error("tesla_api: get_vehicle_data() returned non-JSON.")
                return None
            if not data.get("response"):
                logging.error(f"tesla_api: get_vehicle_data() error: {data.get('error')}")
                return None
            return data["response"]
        return None

    def minutes_to_full_charge(self, vehicle_data) -> str:
        minutes_until_full = vehicle_data['charge_state']['minutes_to_full_charge'] if self.is_charging else "N/A"
        self.time_until_full = lib.helpers.convert_to_fractional_hour(minutes_until_full)
        self.update_mqtt_and_domoticz()
        return self.time_until_full

    def is_vehicle_charging(self, vehicle_data):
        self.is_charging = vehicle_data['charge_state']['charging_state'] == 'Charging'
        self.charging_status = "Charging" if self.is_charging else "Idle"
        if not self.is_charging:
            self.time_until_full = "N/A"
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
