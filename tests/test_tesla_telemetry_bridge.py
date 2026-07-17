"""Tests for the pure translation core of the Tesla Fleet Telemetry bridge."""
from lib import tesla_telemetry_bridge as tb


def test_detailed_charge_state_charging():
    out = tb.translate("DetailedChargeState", "DetailedChargeStateCharging")
    assert out["state"]["tesla_is_plugged"] == "True"
    assert out["state"]["tesla_is_charging"] == "True"
    assert out["topics"]["Tesla/vehicle0/plugged_status"] == "Plugged"
    assert out["topics"]["Tesla/vehicle0/charging_status"] == "Charging"


def test_detailed_charge_state_disconnected():
    out = tb.translate("DetailedChargeState", "DetailedChargeStateDisconnected")
    assert out["state"]["tesla_is_plugged"] == "False"
    assert out["state"]["tesla_is_charging"] == "False"
    assert out["topics"]["Tesla/vehicle0/plugged_status"] == "Unplugged"


def test_detailed_charge_state_plugged_not_charging():
    # Stopped/Complete/NoPower -> plugged in but not charging.
    for st in ("DetailedChargeStateStopped", "DetailedChargeStateComplete", "DetailedChargeStateNoPower"):
        out = tb.translate("DetailedChargeState", st)
        assert out["state"]["tesla_is_plugged"] == "True"
        assert out["state"]["tesla_is_charging"] == "False"


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
    # Telemetry OWNS the charging_amps display topic (accurate per-phase); not scaled.
    assert tb.translate("ChargeAmps", 15)["topics"]["Tesla/vehicle0/charging_amps"] == 15.0


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


def test_stream_signal_counter_batches_to_durable_file(tmp_path, monkeypatch):
    # Durable (tesla_budget state file), not GlobalState (SQLite on tmpfs, wiped every restart
    # by main.py's GlobalStateDatabase.__init__) -- a pod restart must not lose this count.
    from lib import tesla_budget as budget_mod

    path = str(tmp_path / "budget.json")
    monkeypatch.setattr("lib.config_retrieval.retrieve_setting", lambda k: path)

    b = tb.TeslaTelemetryBridge("broker")
    for _ in range(tb._STREAM_FLUSH_EVERY - 1):
        b._count_stream_signal()
    assert budget_mod.usage_snapshot(path)["streaming"]["count"] == 0   # below threshold -> no write yet
    b._count_stream_signal()                                            # hits threshold -> flush
    assert budget_mod.usage_snapshot(path)["streaming"]["count"] == tb._STREAM_FLUSH_EVERY

    # Surviving a "restart" just means re-reading the same file -- nothing in-memory to lose.
    assert budget_mod.usage_snapshot(path)["streaming"]["count"] == tb._STREAM_FLUSH_EVERY


def test_unknown_field_is_ignored():
    assert tb.translate("SomeRandomField", 123) == {}


def test_parse_message_extracts_vin_field_value():
    assert tb.parse_message("telemetry/5YJ3E1EA/v/Soc", b"64", "telemetry") == ("5YJ3E1EA", "Soc", 64)
    # JSON payload with a data wrapper
    assert tb.parse_message("telemetry/5YJ3/v/ACChargingPower", b'{"data": 7.2}', "telemetry") == ("5YJ3", "ACChargingPower", 7.2)
    # wrong base / malformed -> None
    assert tb.parse_message("other/x/Soc", b"1", "telemetry") is None
    assert tb.parse_message("telemetry", b"1", "telemetry") is None
