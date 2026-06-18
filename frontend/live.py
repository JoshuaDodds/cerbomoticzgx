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

try:
    import paho.mqtt.client as mqtt
except Exception:  # paho optional at import time
    mqtt = None


def _config():
    cfg = {}
    cfg.update(dotenv_values(".secrets") or {})
    cfg.update(dotenv_values(".env") or {})
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
            # EV charging power (Watts) — the main service reads it from Domoticz
            # and publishes it here. Absent => the EV node stays hidden.
            "ev_w": "Cerbomoticzgx/GlobalState/ev_power",
            "setpoint_w": f"N/{sid}/settings/0/Settings/CGwacs/AcPowerSetPoint",
            "mode": "Cerbomoticzgx/GlobalState/ai_mode",
            "control_action": "Cerbomoticzgx/GlobalState/ai_control_action",
            "reason": "Cerbomoticzgx/GlobalState/ai_reason",
            "feed_in_state": "Cerbomoticzgx/GlobalState/feed_in_limit_state",
            "day_import_kwh": "Tibber/home/energy/day/imported",
            "day_import_cost": "Tibber/home/energy/day/cost",
            "day_export_kwh": "Tibber/home/energy/day/exported",
            "day_export_reward": "Tibber/home/energy/day/reward",
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
        out["ev_w"] = _num("ev_w")
        out["setpoint_w"] = _num("setpoint_w")
        out["mode"] = vals.get("mode")
        out["control_action"] = vals.get("control_action")
        out["reason"] = vals.get("reason")
        out["feed_in_state"] = vals.get("feed_in_state")
        out["day_import_kwh"] = _num("day_import_kwh")
        out["day_import_cost"] = _num("day_import_cost")
        out["day_export_kwh"] = _num("day_export_kwh")
        out["day_export_reward"] = _num("day_export_reward")
        return out


# Singleton
live = MqttLive()
