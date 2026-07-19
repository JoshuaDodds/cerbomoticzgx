"""Real-time MQTT feed for the dashboard.

Subscribes to the same broker the main service uses (MOSQUITTO_IP) and caches the
latest value for a handful of live topics (SoC, price, grid/PV/battery/load power,
setpoint, AI mode/reason). Read-only — it never publishes control. Exposed to the
UI via /api/live so the dashboard can show truly live values instead of only the
plan snapshot (which updates every optimization cycle).
"""
import json
import threading

from dotenv import dotenv_values
from lib.config_paths import env_path, secrets_path

try:
    import paho.mqtt.client as mqtt
except Exception:  # paho optional at import time
    mqtt = None


def _config():
    cfg = {}
    cfg.update(dotenv_values(secrets_path()) or {})
    cfg.update(dotenv_values(env_path()) or {})
    return cfg


class MqttLive:
    """Caches the latest value of subscribed topics in a thread-safe dict."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {}
        self._connected = False
        self._started = False
        self._key_by_topic = {}
        self._cond = threading.Condition()   # notified on every new MQTT value (for SSE push)

    def _build_topics(self, sid):
        return {
            "soc": f"N/{sid}/battery/277/Soc",
            "price": "Tibber/home/price_info/now/total",
            "grid_w": f"N/{sid}/vebus/276/Ac/ActiveIn/P",
            "pv_w": f"N/{sid}/system/0/Dc/Pv/Power",
            "load_w": f"N/{sid}/vebus/276/Ac/Out/P",
            "batt_w": f"N/{sid}/battery/277/Dc/0/Power",
            # --- Power-flow v2: richer per-component telemetry (all read-only) ----
            # Grid (AC-in) and AC-loads (AC-out) per-phase active power (W). Same
            # vebus/276 service as the totals above, so signs match the totals
            # (grid: +import / -export).
            "grid_l1": f"N/{sid}/vebus/276/Ac/ActiveIn/L1/P",
            "grid_l2": f"N/{sid}/vebus/276/Ac/ActiveIn/L2/P",
            "grid_l3": f"N/{sid}/vebus/276/Ac/ActiveIn/L3/P",
            "load_l1": f"N/{sid}/vebus/276/Ac/Out/L1/P",
            "load_l2": f"N/{sid}/vebus/276/Ac/Out/L2/P",
            "load_l3": f"N/{sid}/vebus/276/Ac/Out/L3/P",
            # Battery detail. Topic choices mirror lib/constants.py: LFP pack voltage
            # on battery/512; current on the BMV service 277. Temperature/TimeToGo are
            # standard Victron battery paths — if a given Venus OS build doesn't
            # publish one, that snapshot field simply stays None and the UI hides it.
            "batt_temp": f"N/{sid}/battery/512/Dc/0/Temperature",
            "batt_voltage": f"N/{sid}/battery/512/Dc/0/Voltage",
            "batt_current": f"N/{sid}/battery/277/Dc/0/Current",
            "batt_ttg": f"N/{sid}/battery/277/TimeToGo",
            # Battery pack detail from the BMS (service 512): per-cell voltage/temp
            # extremes, module count, and remaining/installed capacity (Ah).
            "batt_min_cell_v": f"N/{sid}/battery/512/System/MinCellVoltage",
            "batt_max_cell_v": f"N/{sid}/battery/512/System/MaxCellVoltage",
            "batt_min_cell_t": f"N/{sid}/battery/512/System/MinCellTemperature",
            "batt_max_cell_t": f"N/{sid}/battery/512/System/MaxCellTemperature",
            "batt_modules_online": f"N/{sid}/battery/512/System/NrOfModulesOnline",
            "batt_capacity": f"N/{sid}/battery/512/Capacity",
            "batt_installed_capacity": f"N/{sid}/battery/512/InstalledCapacity",
            # Solar detail: total DC current, per-string V/W (2 MPPT RS chargers, 2
            # strings each — string D on 283/Pv/1 is unused, so we surface A/B/C),
            # live surplus watts, and the optimizer's projected full-day PV total.
            "pv_current": f"N/{sid}/system/0/Dc/Pv/Current",
            "pv_a_v": f"N/{sid}/solarcharger/283/Pv/0/V",
            "pv_a_p": f"N/{sid}/solarcharger/283/Pv/0/P",
            "pv_b_v": f"N/{sid}/solarcharger/282/Pv/1/V",
            "pv_b_p": f"N/{sid}/solarcharger/282/Pv/1/P",
            "pv_c_v": f"N/{sid}/solarcharger/282/Pv/0/V",
            "pv_c_p": f"N/{sid}/solarcharger/282/Pv/0/P",
            "pv_surplus_w": "Tesla/vehicle0/solar/surplus_watts",
            "pv_forecast_today": "Cerbomoticzgx/GlobalState/pv_projected_today",
            # Inverter/charger system state (integer code -> word in the UI, mirroring
            # lib/constants.py SystemState — e.g. 256 = "Discharging").
            "system_state": f"N/{sid}/system/0/SystemState/State",
            # EV charging power (Watts) — the main service reads it from Domoticz
            # and publishes it here. Absent => the EV node stays hidden.
            "ev_w": "Tesla/vehicle0/charging_watts",   # local evcharger meter: fast + accurate (was the laggy domoticz-derived ev_power, which flapped)
            # EV charger lifetime forward energy (kWh) + present session time (s),
            # from the Victron evcharger service (instance 42; matches lib/constants.py).
            "ev_energy_kwh": f"N/{sid}/evcharger/42/Ac/Energy/Forward",
            "ev_charge_time": f"N/{sid}/evcharger/42/ChargingTime",
            # ABB meter phase currents. The power-flow EV card intentionally sums
            # these three physical measurements to match the Tesla total-current
            # convention; it must not multiply the car's retained ChargeAmps value.
            "ev_l1_a": f"N/{sid}/evcharger/42/Ac/L1/Current",
            "ev_l2_a": f"N/{sid}/evcharger/42/Ac/L2/Current",
            "ev_l3_a": f"N/{sid}/evcharger/42/Ac/L3/Current",
            # Tesla vehicle status (published by tesla_api / ev_charge_controller as
            # {"value": ...}). Read-only in the UI — no Fleet API cost. Absent topics
            # simply leave the field None and the Vehicle tab hides that row.
            "veh_name": "Tesla/vehicle0/vehicle_name",
            "veh_soc": "Tesla/vehicle0/battery_soc",
            "veh_soc_limit": "Tesla/vehicle0/battery_soc_setpoint",
            "veh_charging_status": "Tesla/vehicle0/charging_status",
            "veh_plugged_status": "Tesla/vehicle0/plugged_status",
            "veh_is_home": "Tesla/vehicle0/is_home",
            "veh_is_charging": "Tesla/vehicle0/is_charging",
            "veh_is_supercharging": "Tesla/vehicle0/is_supercharging",
            "veh_eta": "Tesla/vehicle0/time_until_full",
            "veh_amps": "Tesla/vehicle0/charging_amps",
            "veh_surplus_amps": "Tesla/vehicle0/solar/surplus_amps",
            "veh_last_update": "Tesla/vehicle0/last_update_at",
            "setpoint_w": f"N/{sid}/settings/0/Settings/CGwacs/AcPowerSetPoint",
            "mode": "Cerbomoticzgx/GlobalState/ai_mode",
            "control_action": "Cerbomoticzgx/GlobalState/ai_control_action",
            "reason": "Cerbomoticzgx/GlobalState/ai_reason",
            "feed_in_state": "Cerbomoticzgx/GlobalState/feed_in_limit_state",
            "ai_ess_override_enabled": "Cerbomoticzgx/system/ai_ess_override_enabled",
            "grid_charging_enabled": "Cerbomoticzgx/system/grid_charging_enabled",
            "day_import_kwh": "Tibber/home/energy/day/imported",
            "day_import_cost": "Tibber/home/energy/day/cost",
            "day_export_kwh": "Tibber/home/energy/day/exported",
            "day_export_reward": "Tibber/home/energy/day/reward",
            # Today / tomorrow lowest & highest buy price (cost €/kWh + the hour it
            # occurs) — already published retained by the Tibber module. Tomorrow's
            # values read "not_yet_published" until Tibber releases them (~13:00).
            "price_today_low": "Tibber/home/price_info/today/lowest/0/cost",
            "price_today_low_at": "Tibber/home/price_info/today/lowest/0/hour",
            "price_today_high": "Tibber/home/price_info/today/highest/0/cost",
            "price_today_high_at": "Tibber/home/price_info/today/highest/0/hour",
            "price_tom_low": "Tibber/home/price_info/tomorrow/lowest/0/cost",
            "price_tom_low_at": "Tibber/home/price_info/tomorrow/lowest/0/hour",
            "price_tom_high": "Tibber/home/price_info/tomorrow/highest/0/cost",
            "price_tom_high_at": "Tibber/home/price_info/tomorrow/highest/0/hour",
        }

    def start(self):
        if self._started or mqtt is None:
            return
        self._started = True

        cfg = _config()
        host = cfg.get("MOSQUITTO_IP")
        sid = cfg.get("VRM_PORTAL_ID")
        if not host or not sid:
            return

        topics = self._build_topics(sid)
        self._key_by_topic = {t: k for k, t in topics.items()}

        client = mqtt.Client(client_id="cerbo-dashboard-live", reconnect_on_failure=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        self._client = client
        try:
            client.connect_async(host, 1883, keepalive=45)
            client.loop_start()
        except Exception:
            self._started = False

    def _on_connect(self, client, _u, _f, _rc):
        self._connected = True
        for topic in self._key_by_topic:
            client.subscribe(topic)

    def _on_disconnect(self, _c, _u, _rc):
        self._connected = False

    def _on_message(self, _c, _u, msg):
        key = self._key_by_topic.get(msg.topic)
        if not key:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            value = payload.get("value", payload) if isinstance(payload, dict) else payload
        except Exception:
            value = msg.payload.decode("utf-8", "ignore")
        with self._lock:
            self._values[key] = value
        with self._cond:                      # wake any SSE streams waiting for a change
            self._cond.notify_all()

    def wait_for_change(self, timeout: float = 15.0) -> None:
        """Block until the next MQTT value arrives (or ``timeout`` for keepalive)."""
        with self._cond:
            self._cond.wait(timeout=timeout)

    def publish(self, topic: str, payload: str = "", retain: bool = False) -> bool:
        """Publish a message on the shared broker (used by the dashboard's deliberate
        actions, e.g. the Replan trigger). Best-effort; returns success."""
        try:
            client = getattr(self, "_client", None)
            if client is not None:
                client.publish(topic, payload=payload, retain=retain)
                return True
        except Exception:
            pass
        return False

    def snapshot(self) -> dict:
        with self._lock:
            vals = dict(self._values)
        out = {"connected": self._connected}

        def _num(k):
            try:
                return float(vals.get(k))
            except (TypeError, ValueError):
                return None

        out["soc"] = _num("soc")
        out["price"] = _num("price")
        out["grid_w"] = _num("grid_w")
        out["pv_w"] = _num("pv_w")
        out["load_w"] = _num("load_w")
        out["batt_w"] = _num("batt_w")
        # Power-flow v2 richer telemetry — each stays None until its topic first
        # publishes, so the UI can degrade gracefully (hide the line) if absent.
        out["grid_l1"] = _num("grid_l1")
        out["grid_l2"] = _num("grid_l2")
        out["grid_l3"] = _num("grid_l3")
        out["load_l1"] = _num("load_l1")
        out["load_l2"] = _num("load_l2")
        out["load_l3"] = _num("load_l3")
        out["batt_temp"] = _num("batt_temp")
        out["batt_voltage"] = _num("batt_voltage")
        out["batt_current"] = _num("batt_current")
        out["batt_ttg"] = _num("batt_ttg")           # seconds; None/large when not discharging
        out["batt_min_cell_v"] = _num("batt_min_cell_v")
        out["batt_max_cell_v"] = _num("batt_max_cell_v")
        out["batt_min_cell_t"] = _num("batt_min_cell_t")
        out["batt_max_cell_t"] = _num("batt_max_cell_t")
        out["batt_modules_online"] = _num("batt_modules_online")
        out["batt_capacity"] = _num("batt_capacity")            # remaining Ah
        out["batt_installed_capacity"] = _num("batt_installed_capacity")  # total Ah
        out["pv_current"] = _num("pv_current")                  # total solar DC current (A)
        out["pv_a_v"] = _num("pv_a_v"); out["pv_a_p"] = _num("pv_a_p")
        out["pv_b_v"] = _num("pv_b_v"); out["pv_b_p"] = _num("pv_b_p")
        out["pv_c_v"] = _num("pv_c_v"); out["pv_c_p"] = _num("pv_c_p")
        out["pv_surplus_w"] = _num("pv_surplus_w")
        out["pv_forecast_today"] = _num("pv_forecast_today")    # projected full-day PV (Wh; UI ÷1000)
        # System-state integer code (UI maps it to a word, mirroring SystemState).
        out["system_state"] = _num("system_state")
        out["ev_w"] = _num("ev_w")
        out["ev_energy_kwh"] = _num("ev_energy_kwh")  # lifetime forward energy (kWh)
        out["ev_charge_time"] = _num("ev_charge_time")  # present session time (s)
        out["ev_l1_a"] = _num("ev_l1_a")
        out["ev_l2_a"] = _num("ev_l2_a")
        out["ev_l3_a"] = _num("ev_l3_a")
        # Tesla vehicle status (read-only mirror of the MQTT bus; no Fleet API cost).
        out["veh_soc"] = _num("veh_soc")
        out["veh_soc_limit"] = _num("veh_soc_limit")
        out["veh_amps"] = _num("veh_amps")                 # measured charge current (A)
        out["veh_surplus_amps"] = _num("veh_surplus_amps")
        out["veh_name"] = vals.get("veh_name")
        out["veh_charging_status"] = vals.get("veh_charging_status")
        out["veh_plugged_status"] = vals.get("veh_plugged_status")
        out["veh_is_home"] = vals.get("veh_is_home")
        out["veh_is_charging"] = vals.get("veh_is_charging")
        out["veh_is_supercharging"] = vals.get("veh_is_supercharging")
        out["veh_eta"] = vals.get("veh_eta")               # time-to-limit while charging
        out["veh_last_update"] = vals.get("veh_last_update")
        out["setpoint_w"] = _num("setpoint_w")
        out["mode"] = vals.get("mode")
        out["control_action"] = vals.get("control_action")
        out["reason"] = vals.get("reason")
        out["feed_in_state"] = vals.get("feed_in_state")
        out["ai_ess_override_enabled"] = vals.get("ai_ess_override_enabled")
        out["grid_charging_enabled"] = vals.get("grid_charging_enabled")
        out["day_import_kwh"] = _num("day_import_kwh")
        out["day_import_cost"] = _num("day_import_cost")
        out["day_export_kwh"] = _num("day_export_kwh")
        out["day_export_reward"] = _num("day_export_reward")
        # Today / tomorrow price extremes (cost is None when not yet published).
        out["price_today_low"] = _num("price_today_low")
        out["price_today_high"] = _num("price_today_high")
        out["price_tom_low"] = _num("price_tom_low")
        out["price_tom_high"] = _num("price_tom_high")
        out["price_today_low_at"] = vals.get("price_today_low_at")
        out["price_today_high_at"] = vals.get("price_today_high_at")
        out["price_tom_low_at"] = vals.get("price_tom_low_at")
        out["price_tom_high_at"] = vals.get("price_tom_high_at")
        return out


# Singleton
live = MqttLive()
