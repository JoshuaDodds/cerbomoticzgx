"""Bridge Tesla Fleet Telemetry onto our own MQTT topics + GlobalState.

The `fleet-telemetry` server (in-cluster) terminates the car's mTLS stream, decodes the
protobuf, and republishes each field to MQTT (its native MQTT dispatcher). This bridge
subscribes to those firehose topics and normalizes them into the `Tesla/vehicle0/*` topics
and GlobalState keys the rest of the system already understands — so the EV controller can
consume PUSHED state instead of polling `vehicle_data`.

`translate()` and `parse_message()` are pure and unit-tested; the subscriber loop only runs
when TESLA_TELEMETRY_ENABLED is on, so this module is inert by default.
"""
import json
import os
import socket
import threading
import time
from datetime import datetime

from lib.constants import logging

_STREAM_FLUSH_EVERY = 20              # batch stream-signal counter writes to the durable file
# fleet-telemetry emits non-"V" record types too (connectivity/alerts/errors); those are not
# billed as vehicle-data SIGNALS, so exclude them from the streaming-signal estimate.
_NON_SIGNAL_FIELDS = {"connectivity", "alerts", "errors", "status", "V", "v"}
_ACK_TIMESTAMP_KEYS = {
    "ChargeLimitSoc": "tesla_soc_setpoint_updated_at",
    "ChargeCurrentRequest": "tesla_charge_current_request_updated_at",
    "ChargeCurrentRequestMax": "tesla_charge_current_max_updated_at",
    "DetailedChargeState": "tesla_charge_state_updated_at",
}


# --- pure translation (no I/O) --------------------------------------------

def mqtt_client_id() -> str:
    """Return an instance-unique MQTT client ID for this bridge process.

    Development and deployed services may briefly overlap during testing. A
    static ID makes the broker evict whichever bridge connected first, which
    can lose QoS-0 Tesla lifecycle events. Hostname plus PID is stable for one
    process and distinct for concurrent processes/pods.
    """
    host = "".join(
        character if character.isalnum() else "-"
        for character in socket.gethostname().lower()
    )[:18] or "host"
    return f"cerbo-tesla-{host}-{os.getpid()}"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_eta_hours(value) -> str:
    h = _num(value)
    if h is None or h <= 0:
        return "N/A"
    total_min = int(round(h * 60))
    hh, mm = divmod(total_min, 60)
    return f"{hh} hr {mm} min" if hh else f"{mm} min"


def _detailed_charge_state(value) -> dict:
    # Values arrive as e.g. "DetailedChargeStateCharging" or "Charging"/"Disconnected".
    norm = str(value).lower().replace("detailedchargestate", "").strip()
    # Unknown is not an unplugged edge. Tesla emits unavailable/null values while
    # the car goes to sleep; replacing the last valid state with Unplugged here
    # makes a physically connected car disappear from scheduling and the UI.
    known_states = {
        "disconnected", "nopower", "starting", "charging", "complete", "stopped",
    }
    if norm not in known_states:
        return {}
    plugged = norm != "disconnected"
    charging = norm == "charging"
    result = {
        "state": {
            "tesla_detailed_charge_state": norm,
            "tesla_is_plugged": str(plugged),
            "tesla_is_charging": str(charging),
        },
        "topics": {
            "Tesla/vehicle0/plugged_status": "Plugged" if plugged else "Unplugged",
            "Tesla/vehicle0/is_charging": str(charging),
            "Tesla/vehicle0/charging_status": "Charging" if charging else "Idle",
        },
    }
    if not charging:
        # TimeToFullCharge is change-driven and is not guaranteed to emit when a
        # charge stops. The charge-state edge is authoritative: an ETA is not
        # meaningful while stopped, complete, disconnected, or without power.
        result["state"]["tesla_time_to_full"] = "N/A"
        result["topics"]["Tesla/vehicle0/time_until_full"] = "N/A"
    return result


def _charge_port_latch(value) -> dict:
    # CONFIRM-ONLY: "Engaged" is a seated, latched connector -> plugged. Any other value
    # ("Blocking"/"Disengaged") is ambiguous (observed "Blocking" while unplugged, and it can
    # also blip during an active charge), so we do NOT set plugged=False here — that would fight
    # DetailedChargeState (which owns the unplugged edge via "Disconnected") and flap the state.
    norm = str(value).lower().replace("chargeportlatch", "").strip()
    if norm == "engaged":
        return {"state": {"tesla_is_plugged": "True"},
                "topics": {"Tesla/vehicle0/plugged_status": "Plugged"}}
    return {}


def _bool_stream(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "present", "engaged")


def _parse_latlon(value):
    """Location arrives as {'latitude': x, 'longitude': y} (decoded JSON) or 'lat,long'."""
    if isinstance(value, dict):
        return _num(value.get("latitude")), _num(value.get("longitude"))
    s = str(value)
    if "," in s:
        parts = s.split(",")
        if len(parts) >= 2:
            return _num(parts[0]), _num(parts[1])
    return None, None


def _location(value, home=None) -> dict:
    """Publish raw lat/long, and derive is_home using the SAME round-to-3-decimals geofence the
    REST path always used (home = (HOME_ADDRESS_LAT, HOME_ADDRESS_LONG))."""
    lat, lon = _parse_latlon(value)
    if lat is None or lon is None:
        return {}
    out = {
        "state": {"tesla_latitude": lat, "tesla_longitude": lon},
        "topics": {"Tesla/vehicle0/latitude": lat, "Tesla/vehicle0/longitude": lon},
    }
    if home and home[0] is not None and home[1] is not None:
        is_home = (round(lat, 3) == round(home[0], 3) and round(lon, 3) == round(home[1], 3))
        out["state"]["tesla_is_home"] = str(is_home)
        out["topics"]["Tesla/vehicle0/is_home"] = str(is_home)
    return out


def translate(field, value, home=None) -> dict:
    """Map one telemetry field to {'state': {...}, 'topics': {...}} updates, or {} if unmapped.

    ``home`` is (lat, long) used only for the Location geofence; other fields ignore it."""
    # Fleet Telemetry can emit JSON null as charge-only signals become
    # unavailable during sleep. Null means "no current reading", not zero,
    # false, unplugged, idle, N/A, or the string "None". Preserve the last
    # valid normalized state until an explicit replacement arrives.
    if value is None:
        return {}
    f = str(field)
    if f == "DetailedChargeState":
        return _detailed_charge_state(value)
    if f == "ChargePortLatch":
        return _charge_port_latch(value)
    if f == "FastChargerPresent":
        sc = _bool_stream(value)
        return {"state": {"tesla_is_supercharging": str(sc)},
                "topics": {"Tesla/vehicle0/is_supercharging": str(sc)}}
    if f == "Location":
        return _location(value, home=home)
    if f == "Soc":
        v = _num(value)
        return {"state": {"tesla_soc": v}, "topics": {"Tesla/vehicle0/battery_soc": v}} if v is not None else {}
    if f == "ChargeLimitSoc":
        v = _num(value)
        return {"state": {"tesla_soc_setpoint": v},
                "topics": {"Tesla/vehicle0/battery_soc_setpoint": v}} if v is not None else {}
    if f == "TimeToFullCharge":
        eta = _fmt_eta_hours(value)
        return {"state": {"tesla_time_to_full": eta}, "topics": {"Tesla/vehicle0/time_until_full": eta}}
    if f == "ACChargingPower":
        # The local Victron EV-charger meter owns actual drawn power/amps (real-time, used for
        # PV surplus rate-matching) and is what frontend/live.py reads as ev_w. The car's own
        # report is slower (change-only >=5s) and has no consumer, so it's intentionally unmapped
        # here rather than publishing a vestigial *_reported topic nothing reads (audit L2).
        return {}
    if f == "ChargeAmps":
        # Diagnostic car-reported value only. The local ABB meter exclusively
        # owns Tesla/vehicle0/charging_amps because Fleet telemetry is change-only
        # and can retain a non-zero ChargeAmps value after charging stops.
        v = _num(value)                     # per-phase amps drawn (as Tesla reports)
        if v is None:
            return {}
        return {"state": {"tesla_amps_reported": v}}
    if f == "ChargeCurrentRequest":
        v = _num(value)                     # amps requested
        return {"state": {"tesla_charge_current_request": v},
                "topics": {"Tesla/vehicle0/charge_current_request": v}} if v is not None else {}
    if f == "ChargeCurrentRequestMax":
        v = _num(value)                     # max amps the car accepts (surplus ceiling)
        return {"state": {"tesla_charge_current_max": v},
                "topics": {"Tesla/vehicle0/charge_current_max": v}} if v is not None else {}
    if f == "ChargerVoltage":
        v = _num(value)
        return {"state": {"tesla_charger_voltage": v},
                "topics": {"Tesla/vehicle0/charger_voltage": v}} if v is not None else {}
    if f == "ChargerPhases":
        v = _num(value)
        return {"state": {"tesla_charger_phases": v},
                "topics": {"Tesla/vehicle0/charger_phases": v}} if v is not None else {}
    if f == "ACChargingEnergyIn":
        v = _num(value)                     # session kWh added
        return {"state": {"tesla_charge_energy_added_kwh": v},
                "topics": {"Tesla/vehicle0/charge_energy_added": v}} if v is not None else {}
    if f == "VehicleName":
        s = str(value)
        return {"state": {"tesla_vehicle_name": s}, "topics": {"Tesla/vehicle0/vehicle_name": s}}
    return {}


def parse_message(topic, payload, topic_base="telemetry"):
    """Extract (vin, field, value) from a fleet-telemetry MQTT message, or None.

    Topic scheme is ``<base>/<vin>/.../<Field>``; payload may be a raw scalar or JSON such as
    ``{"data": x}`` / ``{"value": x}``. Kept flexible so it tolerates dispatcher-format drift.
    """
    parts = str(topic).split("/")
    if len(parts) < 3 or parts[0] != topic_base:
        return None
    vin, field = parts[1], parts[-1]
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "ignore")
    value = payload
    try:
        j = json.loads(payload)
        value = j.get("data", j.get("value", j)) if isinstance(j, dict) else j
    except (ValueError, TypeError):
        value = payload
    return vin, field, value


# --- subscriber (I/O; only runs when enabled) -----------------------------

class TeslaTelemetryBridge:
    """Subscribes to the fleet-telemetry MQTT firehose and republishes normalized state."""

    def __init__(self, broker_host, broker_port=1883, topic_base="telemetry", vin=None):
        self._host = broker_host
        self._port = int(broker_port or 1883)
        self._topic_base = topic_base or "telemetry"
        self._vin = (vin or "").strip() or None
        self._client = None
        self._started = False
        self._home = "unset"          # cached (lat, long); read from settings on first use
        self._sig_seen = 0            # approximate "Streaming Signals" counter (display-only)
        self._sig_flushed = 0
        self._live_fields_seen = set()

    def _count_stream_signal(self):
        """Approximate Tesla's 'Streaming Signals' billing by counting received telemetry
        messages (~one signal each). Batched to the durable tesla_budget state file every
        _STREAM_FLUSH_EVERY messages to avoid write churn; rolls at the UTC month boundary.
        Display-only — streaming isn't something we gate, so it never touches the tesla_budget
        spend guard's caps, only its persisted month_counts. Durable (same file as
        command/data/wake) rather than GlobalState, which lives in a SQLite file on tmpfs that
        main.py explicitly recreates on every process start, so a signal count kept there was
        silently lost on every restart."""
        self._sig_seen += 1
        if (self._sig_seen - self._sig_flushed) < _STREAM_FLUSH_EVERY:
            return
        try:
            from lib.config_retrieval import retrieve_setting
            from lib.tesla_budget import bump_signal_count, DEFAULT_STATE_PATH
            path = retrieve_setting("TESLA_BUDGET_STATE_PATH") or DEFAULT_STATE_PATH
            bump_signal_count(self._sig_seen - self._sig_flushed, path)
            self._sig_flushed = self._sig_seen
        except Exception as e:                 # pragma: no cover - counter must never break the loop
            logging.debug("tesla_telemetry_bridge: stream-signal flush failed: %s", e)

    def _home_coords(self):
        if self._home == "unset":
            from lib.config_retrieval import retrieve_setting
            try:
                self._home = (float(retrieve_setting("HOME_ADDRESS_LAT")),
                              float(retrieve_setting("HOME_ADDRESS_LONG")))
            except (TypeError, ValueError):
                self._home = (None, None)
        return self._home

    def apply(self, field, value, *, retained=False):
        """Translate one field and push the result to GlobalState + retained MQTT topics.

        A retained raw MQTT value is useful to rebuild last-known state after an app restart,
        but it is not a fresh observation from the car. Do not advance freshness/command-
        acknowledgement timestamps for those broker replays.
        """
        updates = translate(field, value, home=self._home_coords())
        if not updates:
            return
        from lib.global_state import GlobalStateClient
        from lib.helpers import publish_message
        state = GlobalStateClient()
        previous = {}
        # Preserve prior live state for plug/start edges and the physical home->away
        # disconnect invariant. A retained broker replay is only last-known state and
        # must never invent any of those transitions.
        if not retained and field in {
            "DetailedChargeState", "ChargePortLatch", "Location",
        }:
            has = getattr(state, "has", None)
            get = getattr(state, "get", None)
            if callable(has) and callable(get):
                for key in (
                    "tesla_is_home", "tesla_is_plugged", "tesla_is_charging",
                ):
                    if has(key):
                        previous[key] = get(key)
        new_state = updates.get("state", {})
        if (
            not retained
            and field == "Location"
            and "tesla_is_home" in new_state
            and "tesla_is_home" in previous
            and _bool_stream(previous["tesla_is_home"])
            and not _bool_stream(new_state["tesla_is_home"])
        ):
            # A live home->away transition physically requires disconnecting the home cable.
            # Fleet charge-only signals can be absent while driving, so derive this one
            # impossible-state correction from Location. Do not repeat it while already away:
            # a later explicit public-charging state must remain valid.
            new_state.update({
                "tesla_is_plugged": "False",
                "tesla_is_charging": "False",
                "tesla_time_to_full": "N/A",
            })
            updates.setdefault("topics", {}).update({
                "Tesla/vehicle0/plugged_status": "Unplugged",
                "Tesla/vehicle0/is_charging": "False",
                "Tesla/vehicle0/charging_status": "Idle",
                "Tesla/vehicle0/time_until_full": "N/A",
            })
        for k, v in updates.get("state", {}).items():
            state.set(k, v)
        if not retained:
            observed_at = time.time()
            state.set("tesla_telemetry_last_update_ts", observed_at)
            state.set("tesla_vehicle_last_update_ts", observed_at)
            if (
                "tesla_is_plugged" in new_state
                and "tesla_is_plugged" in previous
                and not _bool_stream(previous["tesla_is_plugged"])
                and _bool_stream(new_state["tesla_is_plugged"])
            ):
                state.set("tesla_plugged_transition_ts", observed_at)
            if (
                "tesla_is_charging" in new_state
                and "tesla_is_charging" in previous
                and not _bool_stream(previous["tesla_is_charging"])
                and _bool_stream(new_state["tesla_is_charging"])
            ):
                state.set("tesla_charge_started_ts", observed_at)
            acknowledgement_key = _ACK_TIMESTAMP_KEYS.get(field)
            if acknowledgement_key:
                state.set(acknowledgement_key, observed_at)
        for topic, v in updates.get("topics", {}).items():
            publish_message(topic, payload=f'{{"value": "{v}"}}', qos=0, retain=True)
        if not retained:
            publish_message(
                "Tesla/vehicle0/last_update_at",
                payload=f'{{"value": "{time.strftime("%Y-%m-%d %H:%M:%S")}"}}',
                qos=0,
                retain=True,
            )

    @staticmethod
    def _connectivity_status(value):
        if not isinstance(value, dict):
            return str(value or "").strip().upper()
        for key, item in value.items():
            if str(key).lower() == "status":
                return str(item or "").strip().upper()
        return ""

    @staticmethod
    def _connectivity_created_at(value):
        if not isinstance(value, dict):
            return 0.0
        raw = next((
            item for key, item in value.items()
            if str(key).lower() == "createdat"
        ), None)
        if not raw:
            return 0.0
        try:
            parsed = datetime.fromisoformat(
                str(raw).replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _connectivity_id(value):
        if not isinstance(value, dict):
            return ""
        return str(next((
            item for key, item in value.items()
            if str(key).lower() == "connectionid"
        ), "") or "")

    @staticmethod
    def _set_bridge_transport(status):
        """Publish subscriber health without altering Tesla's source lifecycle."""
        from lib.global_state import GlobalStateClient
        from lib.helpers import publish_message

        normalized = str(status or "").strip().upper()
        state = GlobalStateClient()
        get = getattr(state, "get", None)
        if callable(get):
            try:
                current = str(
                    get("tesla_telemetry_bridge_status") or ""
                ).strip().upper()
                if current == normalized:
                    return
            except Exception:
                pass
        state.set("tesla_telemetry_bridge_status", normalized)
        state.set("tesla_telemetry_bridge_updated_at", time.time())
        publish_message(
            "Tesla/vehicle0/telemetry_bridge_status",
            payload=f'{{"value": "{normalized}"}}',
            qos=0,
            retain=True,
        )

    def _apply_connectivity(self, value, *, retained=False):
        """Persist Fleet Telemetry's connection lifecycle separately from vehicle signals.

        Connectivity records are operational metadata, not billable streaming signals. They
        must not advance the last *vehicle-data* timestamp, but a retained DISCONNECTED record
        is authoritative evidence that the cached Soc/charge state may be stale.
        """
        status = self._connectivity_status(value)
        if not status:
            return
        from lib.global_state import GlobalStateClient
        from lib.helpers import publish_message
        state = GlobalStateClient()
        event_at = self._connectivity_created_at(value)
        connection_id = self._connectivity_id(value)
        try:
            current_event_at = float(
                state.get("tesla_telemetry_connection_event_at") or 0)
        except (AttributeError, TypeError, ValueError):
            current_event_at = 0.0
        current_status = (
            str(state.get("tesla_telemetry_connection_status") or "")
            .strip().upper()
            if hasattr(state, "get") else ""
        )
        current_id = (
            str(state.get("tesla_telemetry_connection_id") or "")
            if hasattr(state, "get") else ""
        )
        if event_at and current_event_at:
            if event_at < current_event_at:
                logging.debug(
                    "tesla_telemetry_bridge: ignored delayed connectivity "
                    "%s event created at %.3f (current event %.3f).",
                    status, event_at, current_event_at,
                )
                return
            if (
                abs(event_at - current_event_at) < 0.001
                and current_status == "DISCONNECTED"
                and status == "CONNECTED"
            ):
                # Equal-time ambiguity fails closed. A disconnect is safer than
                # treating command buses as available.
                return
            if (
                abs(event_at - current_event_at) < 0.001
                and current_status == status
                and current_id == connection_id
            ):
                # Retained lifecycle data is Tesla's authoritative last event.
                # Its original CreatedAt remains intact, so this hydrates
                # startup without pretending that a new wake occurred.
                self._set_bridge_transport("SYNCHRONIZED")
                return
        state.set("tesla_telemetry_connection_status", status)
        source_at = event_at or time.time()
        state.set("tesla_telemetry_connection_updated_at", source_at)
        if event_at:
            state.set("tesla_telemetry_connection_event_at", event_at)
        if connection_id:
            state.set("tesla_telemetry_connection_id", connection_id)
        if status == "DISCONNECTED":
            state.set("tesla_telemetry_connected_at", 0)
        elif status == "CONNECTED":
            # Mirror Tesla's CreatedAt exactly, including retained replay.
            # Command readiness is gated separately by bridge synchronization,
            # so this source timestamp is never mistaken for a fresh wake.
            state.set("tesla_telemetry_connected_at", source_at)
        publish_message(
            "Tesla/vehicle0/telemetry_status",
            payload=f'{{"value": "{status}"}}',
            qos=0,
            retain=True,
        )
        self._set_bridge_transport("SYNCHRONIZED")

    def _on_connect(self, client, _userdata, _flags, _reason_code, *args):
        # A broker connection alone does not prove the vehicle stream is
        # current. Wait for a source lifecycle event before commands trust it.
        self._set_bridge_transport("AWAITING_SOURCE")
        client.subscribe(f"{self._topic_base}/#")

    def _on_disconnect(self, _client, _userdata, _reason_code, *args):
        self._set_bridge_transport("DISCONNECTED")

    def _on_message(self, _c, _u, msg):
        parsed = parse_message(msg.topic, msg.payload, self._topic_base)
        if not parsed:
            return
        vin, field, value = parsed
        if self._vin and vin != self._vin:
            return
        if field == "connectivity":
            try:
                self._apply_connectivity(
                    value, retained=bool(getattr(msg, "retain", False)))
            except Exception as e:             # pragma: no cover - subscriber must stay alive
                logging.debug(
                    "tesla_telemetry_bridge: connectivity apply failed: %s", e)
            return
        if field not in _NON_SIGNAL_FIELDS:      # count only actual vehicle-data signals
            self._count_stream_signal()
        retained = bool(getattr(msg, "retain", False))
        if retained and field in self._live_fields_seen:
            # A retained broker replay is useful only until this process has
            # observed a live value for the same source topic. Never let a
            # delayed retained delivery roll a field back afterwards.
            return
        try:
            self.apply(field, value, retained=retained)
            if not retained and value is not None:
                self._live_fields_seen.add(field)
                # A live vehicle signal proves source hydration even if no
                # connectivity edge accompanied it.
                self._set_bridge_transport("SYNCHRONIZED")
        except Exception as e:                 # pragma: no cover - never let a bad msg kill the loop
            logging.debug("tesla_telemetry_bridge: apply failed for %s=%s: %s", field, value, e)

    def start(self):
        if self._started:
            return
        # Publish the startup race synchronously before importing/connecting so
        # the independently-started EV worker cannot mistake absent bridge state
        # for permission to issue remote commands.
        self._set_bridge_transport("CONNECTING")
        try:
            import paho.mqtt.client as mqtt
        except Exception as e:                 # pragma: no cover
            logging.warning("tesla_telemetry_bridge: paho-mqtt unavailable: %s", e)
            self._set_bridge_transport("DISCONNECTED")
            return
        self._started = True
        client = mqtt.Client(
            client_id=mqtt_client_id(), reconnect_on_failure=True)
        client.on_message = self._on_message
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        self._client = client
        try:
            client.connect_async(self._host, self._port, keepalive=45)
            client.loop_start()
            logging.info("tesla_telemetry_bridge: subscribed to %s/# on %s:%d (vin=%s).",
                         self._topic_base, self._host, self._port, self._vin or "any")
        except Exception as e:                 # pragma: no cover
            logging.warning("tesla_telemetry_bridge: could not connect to broker: %s", e)
            self._started = False
            self._set_bridge_transport("DISCONNECTED")


def start_bridge_if_enabled(retrieve_setting):
    """Start the bridge iff TESLA_TELEMETRY_ENABLED. Returns the bridge or None. Called from
    service startup; the broker host defaults to the in-cluster Mosquitto service."""
    from lib.helpers import is_truthy
    if not is_truthy(retrieve_setting("TESLA_TELEMETRY_ENABLED"), default=False):
        return None
    host = retrieve_setting("MOSQUITTO_IP") or "mosquitto"
    bridge = TeslaTelemetryBridge(
        broker_host=host,
        topic_base=retrieve_setting("TESLA_TELEMETRY_TOPIC_BASE") or "telemetry",
        vin=retrieve_setting("TESLA_TELEMETRY_VIN"),
    )
    bridge.start()
    return bridge
