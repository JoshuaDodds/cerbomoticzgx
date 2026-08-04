"""Tests for the pure translation core of the Tesla Fleet Telemetry bridge."""
from datetime import datetime

import pytest

from lib import tesla_telemetry_bridge as tb


def test_detailed_charge_state_charging():
    out = tb.translate("DetailedChargeState", "DetailedChargeStateCharging")
    assert out["state"]["tesla_detailed_charge_state"] == "charging"
    assert out["state"]["tesla_is_plugged"] == "True"
    assert out["state"]["tesla_is_charging"] == "True"
    assert out["topics"]["Tesla/vehicle0/plugged_status"] == "Plugged"
    assert out["topics"]["Tesla/vehicle0/charging_status"] == "Charging"
    assert "tesla_time_to_full" not in out["state"]
    assert "Tesla/vehicle0/time_until_full" not in out["topics"]


def test_detailed_charge_state_disconnected():
    out = tb.translate("DetailedChargeState", "DetailedChargeStateDisconnected")
    assert out["state"]["tesla_is_plugged"] == "False"
    assert out["state"]["tesla_is_charging"] == "False"
    assert out["state"]["tesla_time_to_full"] == "N/A"
    assert out["topics"]["Tesla/vehicle0/plugged_status"] == "Unplugged"
    assert out["topics"]["Tesla/vehicle0/time_until_full"] == "N/A"


@pytest.mark.parametrize(
    "value",
    (None, "", "Unknown", "DetailedChargeStateUnknown"),
)
def test_unknown_detailed_charge_state_preserves_last_known_state(value):
    assert tb.translate("DetailedChargeState", value) == {}


def test_detailed_charge_state_plugged_not_charging():
    # Stopped/Complete/NoPower -> plugged in but not charging.
    for st in ("DetailedChargeStateStopped", "DetailedChargeStateComplete", "DetailedChargeStateNoPower"):
        out = tb.translate("DetailedChargeState", st)
        assert out["state"]["tesla_detailed_charge_state"] in {
            "stopped", "complete", "nopower",
        }
        assert out["state"]["tesla_is_plugged"] == "True"
        assert out["state"]["tesla_is_charging"] == "False"
        assert out["state"]["tesla_time_to_full"] == "N/A"
        assert out["topics"]["Tesla/vehicle0/time_until_full"] == "N/A"


def test_charge_port_latch_confirms_plugged_only():
    plugged = tb.translate("ChargePortLatch", "ChargePortLatchEngaged")
    assert plugged["state"]["tesla_is_plugged"] == "True"
    assert plugged["topics"]["Tesla/vehicle0/plugged_status"] == "Plugged"
    # Non-Engaged is ambiguous -> NO claim, so it can't fight DetailedChargeState / flap plugged.
    assert tb.translate("ChargePortLatch", "Disengaged") == {}
    assert tb.translate("ChargePortLatch", "ChargePortLatchBlocking") == {}


def test_fast_charger_present_maps_supercharging():
    on = tb.translate("FastChargerPresent", "true")
    assert on["state"]["tesla_is_supercharging"] == "True"
    assert on["topics"]["Tesla/vehicle0/is_supercharging"] == "True"
    assert tb.translate("FastChargerPresent", False)["state"]["tesla_is_supercharging"] == "False"


def test_location_geofence_home_and_away():
    home = (52.1234, 4.5678)
    at_home = tb.translate("Location", {"latitude": 52.12341, "longitude": 4.56779}, home=home)
    assert at_home["state"]["tesla_is_home"] == "True"
    assert at_home["topics"]["Tesla/vehicle0/latitude"] == 52.12341
    away = tb.translate("Location", {"latitude": 52.200, "longitude": 4.600}, home=home)
    assert away["state"]["tesla_is_home"] == "False"
    # Without home coords, still publishes raw lat/long but no is_home claim.
    no_home = tb.translate("Location", "52.1,4.5")
    assert "tesla_is_home" not in no_home["state"]
    assert no_home["topics"]["Tesla/vehicle0/longitude"] == 4.5


def test_charge_current_request_and_max_and_energy():
    assert tb.translate("ChargeCurrentRequest", 12)["topics"]["Tesla/vehicle0/charge_current_request"] == 12.0
    assert tb.translate("ChargeCurrentRequestMax", 16)["state"]["tesla_charge_current_max"] == 16.0
    assert tb.translate("ACChargingEnergyIn", 3.4)["topics"]["Tesla/vehicle0/charge_energy_added"] == 3.4


def test_soc_and_limit_and_amps():
    assert tb.translate("Soc", 64)["topics"]["Tesla/vehicle0/battery_soc"] == 64.0
    assert tb.translate("ChargeLimitSoc", 80)["topics"]["Tesla/vehicle0/battery_soc_setpoint"] == 80.0
    # Fleet ChargeAmps is diagnostic only. The ABB meter is the sole owner of the
    # shared measured-current topic, preventing retained/stale car telemetry from
    # overwriting the value used by legacy UI and control.
    out = tb.translate("ChargeAmps", 15)
    assert out["state"]["tesla_amps_reported"] == 15.0
    assert "Tesla/vehicle0/charging_amps" not in out.get("topics", {})


def test_apply_records_field_specific_acknowledgement_timestamp(monkeypatch):
    stored = {}

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *a, **k: None)
    monkeypatch.setattr(tb.time, "time", lambda: 1234.5)
    bridge = tb.TeslaTelemetryBridge("broker")

    bridge.apply("ChargeLimitSoc", 95)

    assert stored["tesla_soc_setpoint"] == 95.0
    assert stored["tesla_telemetry_last_update_ts"] == 1234.5
    assert stored["tesla_soc_setpoint_updated_at"] == 1234.5


def test_live_charge_state_records_plug_and_charge_start_edges(monkeypatch):
    stored = {
        "tesla_is_plugged": "False",
        "tesla_is_charging": "False",
    }

    class State:
        def has(self, key):
            return key in stored

        def get(self, key):
            return stored.get(key)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *a, **k: None)
    monkeypatch.setattr(tb.time, "time", lambda: 1234.5)
    bridge = tb.TeslaTelemetryBridge("broker")

    bridge.apply("DetailedChargeState", "DetailedChargeStateCharging")

    assert stored["tesla_plugged_transition_ts"] == 1234.5
    assert stored["tesla_charge_started_ts"] == 1234.5


def test_live_home_to_away_transition_clears_impossible_home_plug_state(
        monkeypatch):
    """A car cannot leave home without disconnecting its home charge cable."""
    stored = {
        "tesla_is_home": "True",
        "tesla_is_plugged": "True",
        "tesla_is_charging": "True",
        "tesla_time_to_full": "2 hr 40 min",
    }
    published = {}

    class State:
        def has(self, key):
            return key in stored

        def get(self, key):
            return stored.get(key)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())

    def publish(topic, **kwargs):
        published[topic] = kwargs["payload"]

    monkeypatch.setattr("lib.helpers.publish_message", publish)
    bridge = tb.TeslaTelemetryBridge("broker")
    bridge._home = (52.1234, 4.5678)

    bridge.apply(
        "Location",
        {"latitude": 52.2, "longitude": 4.6},
        retained=False,
    )

    assert stored["tesla_is_home"] == "False"
    assert stored["tesla_is_plugged"] == "False"
    assert stored["tesla_is_charging"] == "False"
    assert stored["tesla_time_to_full"] == "N/A"
    assert '"Unplugged"' in published["Tesla/vehicle0/plugged_status"]
    assert '"False"' in published["Tesla/vehicle0/is_charging"]
    assert '"N/A"' in published["Tesla/vehicle0/time_until_full"]

    # If it is then plugged into a different charger while away, Tesla's explicit
    # charge-state event overrides the derived disconnect normally.
    bridge.apply(
        "DetailedChargeState",
        "DetailedChargeStateCharging",
        retained=False,
    )
    assert stored["tesla_is_home"] == "False"
    assert stored["tesla_is_plugged"] == "True"
    assert stored["tesla_is_charging"] == "True"


def test_away_location_does_not_override_explicit_public_charging_state(
        monkeypatch):
    """Only the home->away edge derives an unplug; later away updates preserve public charging."""
    stored = {
        "tesla_is_home": "False",
        "tesla_is_plugged": "True",
        "tesla_is_charging": "True",
        "tesla_time_to_full": "45 min",
    }

    class State:
        def has(self, key):
            return key in stored

        def get(self, key):
            return stored.get(key)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *a, **k: None)
    bridge = tb.TeslaTelemetryBridge("broker")
    bridge._home = (52.1234, 4.5678)

    bridge.apply(
        "Location",
        {"latitude": 52.2, "longitude": 4.6},
        retained=False,
    )

    assert stored["tesla_is_home"] == "False"
    assert stored["tesla_is_plugged"] == "True"
    assert stored["tesla_is_charging"] == "True"
    assert stored["tesla_time_to_full"] == "45 min"


def test_retained_charge_state_does_not_invent_plug_or_start_edges(monkeypatch):
    stored = {
        "tesla_is_plugged": "False",
        "tesla_is_charging": "False",
    }

    class State:
        def has(self, key):
            return key in stored

        def get(self, key):
            return stored.get(key)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *a, **k: None)
    bridge = tb.TeslaTelemetryBridge("broker")

    bridge.apply(
        "DetailedChargeState",
        "DetailedChargeStateCharging",
        retained=True,
    )

    assert "tesla_plugged_transition_ts" not in stored
    assert "tesla_charge_started_ts" not in stored


def test_retained_signal_rebuilds_value_without_claiming_fresh_observation(
        monkeypatch):
    stored = {}
    published = []

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr(
        "lib.helpers.publish_message",
        lambda topic, **kwargs: published.append(topic),
    )
    bridge = tb.TeslaTelemetryBridge("broker")

    bridge.apply("Soc", 35.4, retained=True)

    assert stored["tesla_soc"] == 35.4
    assert "tesla_telemetry_last_update_ts" not in stored
    assert "tesla_vehicle_last_update_ts" not in stored
    assert "Tesla/vehicle0/battery_soc" in published
    assert "Tesla/vehicle0/last_update_at" not in published


def test_live_current_availability_gets_freshness_timestamp(monkeypatch):
    stored = {}

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(tb.time, "time", lambda: 1234.5)
    bridge = tb.TeslaTelemetryBridge("broker")

    bridge.apply("ChargeCurrentRequestMax", 25)

    assert stored["tesla_charge_current_max"] == 25.0
    assert stored["tesla_charge_current_max_updated_at"] == 1234.5


@pytest.mark.parametrize(
    "field",
    (
        "DetailedChargeState",
        "ChargePortLatch",
        "FastChargerPresent",
        "Location",
        "Soc",
        "ChargeLimitSoc",
        "TimeToFullCharge",
        "ACChargingPower",
        "ChargeAmps",
        "ChargeCurrentRequest",
        "ChargeCurrentRequestMax",
        "ChargerVoltage",
        "ChargerPhases",
        "ACChargingEnergyIn",
        "VehicleName",
    ),
)
def test_null_stream_value_does_not_replace_last_known_normalized_state(
        monkeypatch, field):
    stored = {}
    published = []

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr(
        "lib.helpers.publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    bridge = tb.TeslaTelemetryBridge("broker")

    bridge.apply(field, None)

    assert stored == {}
    assert published == []


def test_ac_power_is_unmapped():
    # ACChargingPower is intentionally not translated (audit L2 cleanup): the local meter owns
    # Tesla/vehicle0/charging_watts and nothing consumed the old *_reported topic/state.
    out = tb.translate("ACChargingPower", 7.2)
    assert out == {}


def test_eta_formatting():
    assert tb.translate("TimeToFullCharge", 1.5)["topics"]["Tesla/vehicle0/time_until_full"] == "1 hr 30 min"
    assert tb.translate("TimeToFullCharge", 0)["topics"]["Tesla/vehicle0/time_until_full"] == "N/A"


def test_on_message_counts_only_real_signals(monkeypatch):
    # connectivity/alerts/errors records are not billed as vehicle-data signals -> not counted.
    b = tb.TeslaTelemetryBridge("h", vin="VIN")
    counted = {"n": 0}
    b._count_stream_signal = lambda: counted.__setitem__("n", counted["n"] + 1)
    b.apply = lambda field, value: None

    class Msg:
        def __init__(self, topic, payload):
            self.topic, self.payload = topic, payload

    b._on_message(None, None, Msg("telemetry/VIN/connectivity", '{"Status":"CONNECTED"}'))
    assert counted["n"] == 0                              # connectivity not a billed signal
    b._on_message(None, None, Msg("telemetry/VIN/v/ChargeAmps", "5"))
    assert counted["n"] == 1                              # real vehicle-data signal counted


def test_connectivity_record_is_persisted_and_published_without_counting_signal(
        monkeypatch):
    stored = {}
    published = []

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr(
        "lib.helpers.publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    monkeypatch.setattr(tb.time, "time", lambda: 1234.5)
    bridge = tb.TeslaTelemetryBridge("broker", vin="VIN")
    bridge._count_stream_signal = lambda: pytest.fail(
        "connectivity events are not billable signals")

    class Msg:
        topic = "telemetry/VIN/connectivity"
        payload = b'{"Status":"DISCONNECTED","CreatedAt":"2026-07-23T11:09:30Z"}'
        retain = False

    bridge._on_message(None, None, Msg())

    assert stored["tesla_telemetry_connection_status"] == "DISCONNECTED"
    assert stored["tesla_telemetry_connection_updated_at"] == pytest.approx(
        datetime.fromisoformat(
            "2026-07-23T11:09:30+00:00").timestamp())
    assert stored["tesla_telemetry_connected_at"] == 0
    assert any(
        topic == "Tesla/vehicle0/telemetry_status"
        and '"DISCONNECTED"' in kwargs["payload"]
        and kwargs["retain"] is True
        for topic, kwargs in published
    )


def test_connected_event_mirrors_source_timestamp_and_retained_source_synchronizes(
        monkeypatch):
    stored = {}

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(tb.time, "time", lambda: 4321.5)
    bridge = tb.TeslaTelemetryBridge("broker", vin="VIN")

    class Msg:
        topic = "telemetry/VIN/connectivity"
        payload = b'{"Status":"CONNECTED","CreatedAt":"2026-07-26T09:08:12Z"}'
        retain = False

    bridge._on_message(None, None, Msg())
    assert stored["tesla_telemetry_connection_status"] == "CONNECTED"
    source_at = datetime.fromisoformat(
        "2026-07-26T09:08:12+00:00").timestamp()
    assert stored["tesla_telemetry_connection_updated_at"] == source_at
    assert stored["tesla_telemetry_connected_at"] == source_at
    assert stored["tesla_telemetry_connection_event_at"] == pytest.approx(
        datetime.fromisoformat(
            "2026-07-26T09:08:12+00:00").timestamp())

    stored.clear()
    Msg.retain = True
    bridge._on_message(None, None, Msg())
    assert stored["tesla_telemetry_connection_status"] == "CONNECTED"
    assert stored["tesla_telemetry_connected_at"] == source_at
    assert stored["tesla_telemetry_bridge_status"] == "SYNCHRONIZED"


def test_live_vehicle_signal_synchronizes_bridge_without_connectivity_edge(
        monkeypatch):
    stored = {}

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *args, **kwargs: None)
    bridge = tb.TeslaTelemetryBridge("broker", vin="VIN")
    bridge._count_stream_signal = lambda: None

    class Msg:
        topic = "telemetry/VIN/v/Soc"
        payload = b"72"
        retain = False

    bridge._on_message(None, None, Msg())

    assert stored["tesla_telemetry_bridge_status"] == "SYNCHRONIZED"


def test_mqtt_client_id_is_unique_per_runtime_instance(monkeypatch):
    monkeypatch.setattr(tb.socket, "gethostname", lambda: "Cerbo Dev/Worker")
    monkeypatch.setattr(tb.os, "getpid", lambda: 31415)

    assert tb.mqtt_client_id() == "cerbo-tesla-cerbo-dev-worker-31415"

    monkeypatch.setattr(tb.os, "getpid", lambda: 31416)
    assert tb.mqtt_client_id() == "cerbo-tesla-cerbo-dev-worker-31416"


def test_delayed_connected_event_cannot_overwrite_newer_disconnect(monkeypatch):
    stored = {}
    published = []

    class State:
        def get(self, key):
            return stored.get(key, 0)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr(
        "lib.helpers.publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    bridge = tb.TeslaTelemetryBridge("broker", vin="VIN")

    bridge._apply_connectivity({
        "ConnectionId": "new-disconnect",
        "CreatedAt": "2026-07-26T09:24:57Z",
        "Status": "DISCONNECTED",
    })
    bridge._apply_connectivity({
        "ConnectionId": "old-connect",
        "CreatedAt": "2026-07-26T09:20:00Z",
        "Status": "CONNECTED",
    })

    assert stored["tesla_telemetry_connection_status"] == "DISCONNECTED"
    assert stored["tesla_telemetry_connected_at"] == 0
    assert len([
        item for item in published
        if item[0] == "Tesla/vehicle0/telemetry_status"
    ]) == 1


def test_retained_vehicle_field_cannot_overwrite_fresher_live_value(monkeypatch):
    bridge = tb.TeslaTelemetryBridge("broker", vin="VIN")
    applied, counted = [], []
    bridge._count_stream_signal = lambda: counted.append(True)
    bridge.apply = lambda field, value, *, retained=False: applied.append(
        (field, value, retained))

    class Msg:
        topic = "telemetry/VIN/v/Soc"

        def __init__(self, payload, retain):
            self.payload = payload
            self.retain = retain

    bridge._on_message(None, None, Msg(b"71", False))
    bridge._on_message(None, None, Msg(b"68", True))

    assert applied == [("Soc", 71, False)]
    assert counted == [True]


def test_bridge_transport_disconnect_invalidates_command_bus_readiness(
        monkeypatch):
    stored = {}
    published = []

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr(
        "lib.helpers.publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    bridge = tb.TeslaTelemetryBridge("broker", vin="VIN")

    bridge._on_disconnect(None, None, 1)

    assert stored["tesla_telemetry_bridge_status"] == "DISCONNECTED"
    assert "tesla_telemetry_connection_status" not in stored
    assert "tesla_telemetry_connected_at" not in stored
    assert any(
        topic == "Tesla/vehicle0/telemetry_bridge_status"
        and '"DISCONNECTED"' in kwargs["payload"]
        for topic, kwargs in published
    )


def test_bridge_connect_subscribes_and_starts_unknown_until_source_event(
        monkeypatch):
    stored = {}

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("lib.global_state.GlobalStateClient", lambda: State())
    monkeypatch.setattr("lib.helpers.publish_message", lambda *args, **kwargs: None)
    subscriptions = []
    bridge = tb.TeslaTelemetryBridge("broker", vin="VIN")

    class Client:
        def subscribe(self, topic):
            subscriptions.append(topic)

    bridge._on_connect(Client(), None, None, 0)

    assert subscriptions == ["telemetry/#"]
    assert stored["tesla_telemetry_bridge_status"] == "AWAITING_SOURCE"
    assert "tesla_telemetry_connection_status" not in stored


def test_stream_signal_counter_batches_to_durable_file(tmp_path, monkeypatch):
    # Durable (tesla_budget state file), not GlobalState (SQLite on tmpfs, wiped every restart
    # by main.py's GlobalStateDatabase.__init__) -- a pod restart must not lose this count.
    from lib import tesla_budget as budget_mod
    from lib import config_retrieval

    path = str(tmp_path / "budget.json")
    monkeypatch.setattr(config_retrieval, "retrieve_setting", lambda k: path)

    b = tb.TeslaTelemetryBridge("broker")
    for _ in range(tb._STREAM_FLUSH_EVERY - 1):
        b._count_stream_signal()
    assert budget_mod.usage_snapshot(path)["streaming"]["count"] == 0   # below threshold -> no write yet
    b._count_stream_signal()                                            # hits threshold -> flush
    assert budget_mod.usage_snapshot(path)["streaming"]["count"] == tb._STREAM_FLUSH_EVERY

    # Surviving a "restart" just means re-reading the same file -- nothing in-memory to lose.
    assert budget_mod.usage_snapshot(path)["streaming"]["count"] == tb._STREAM_FLUSH_EVERY


def test_stream_signal_counter_flushes_partial_batch_on_disconnect(
        tmp_path, monkeypatch):
    from lib import tesla_budget as budget_mod
    from lib import config_retrieval

    path = str(tmp_path / "budget.json")
    monkeypatch.setattr(config_retrieval, "retrieve_setting", lambda key: path)
    monkeypatch.setattr(
        tb.TeslaTelemetryBridge, "_set_bridge_transport", lambda *args: None)
    bridge = tb.TeslaTelemetryBridge("broker")
    for _ in range(7):
        bridge._count_stream_signal()

    bridge._on_disconnect(None, None, 1)

    assert budget_mod.usage_snapshot(path)["streaming"]["count"] == 7
    assert bridge._sig_flushed == bridge._sig_seen == 7


def test_unknown_field_is_ignored():
    assert tb.translate("SomeRandomField", 123) == {}


def test_parse_message_extracts_vin_field_value():
    assert tb.parse_message("telemetry/5YJ3E1EA/v/Soc", b"64", "telemetry") == ("5YJ3E1EA", "Soc", 64)
    # JSON payload with a data wrapper
    assert tb.parse_message("telemetry/5YJ3/v/ACChargingPower", b'{"data": 7.2}', "telemetry") == ("5YJ3", "ACChargingPower", 7.2)
    # wrong base / malformed -> None
    assert tb.parse_message("other/x/Soc", b"1", "telemetry") is None
    assert tb.parse_message("telemetry", b"1", "telemetry") is None
