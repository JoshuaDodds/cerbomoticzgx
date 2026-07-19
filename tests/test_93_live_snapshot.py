"""Power-flow v2 — live snapshot exposes the richer per-component telemetry.

These cover frontend/live.py's snapshot() contract for the new fields the v2
power-flow cards read (grid/loads per-phase, battery temp/voltage/current/
time-to-go, inverter system-state code, EV lifetime energy + session time).
No broker is needed: snapshot() only reads the in-memory value cache, so we
inject values directly and assert the parsing/None-fallback behaviour.
"""
from frontend.live import MqttLive

# Every key the v2 cards depend on, beyond the pre-existing power/SoC fields.
V2_FIELDS = (
    "grid_l1", "grid_l2", "grid_l3",
    "load_l1", "load_l2", "load_l3",
    "batt_temp", "batt_voltage", "batt_current", "batt_ttg",
    "system_state",
    "ev_energy_kwh", "ev_charge_time", "ev_l1_a", "ev_l2_a", "ev_l3_a",
)


def _snapshot_with(values):
    m = MqttLive()
    m._values = dict(values)
    m._connected = True
    return m.snapshot()


def test_snapshot_exposes_v2_fields_when_present():
    snap = _snapshot_with({
        "grid_l1": -2056, "grid_l2": -714, "grid_l3": -1445,
        "load_l1": 131, "load_l2": 1458, "load_l3": 742,
        "batt_temp": 36, "batt_voltage": 52.85, "batt_current": -122,
        "batt_ttg": 24840, "system_state": 256,
        "ev_energy_kwh": 18420.5, "ev_charge_time": 0,
        "ev_l1_a": 13.1, "ev_l2_a": 13.0, "ev_l3_a": 13.2,
    })
    # Grid + AC-loads per-phase (signs preserved as the meter reports them).
    assert snap["grid_l1"] == -2056 and snap["grid_l3"] == -1445
    assert snap["load_l2"] == 1458
    # Battery detail.
    assert snap["batt_temp"] == 36
    assert snap["batt_voltage"] == 52.85
    assert snap["batt_current"] == -122
    assert snap["batt_ttg"] == 24840
    # Inverter/charger system-state code (UI maps 256 -> "Discharging").
    assert snap["system_state"] == 256
    # EV session detail.
    assert snap["ev_energy_kwh"] == 18420.5
    assert snap["ev_charge_time"] == 0
    assert snap["ev_l1_a"] == 13.1
    assert snap["ev_l2_a"] == 13.0
    assert snap["ev_l3_a"] == 13.2


def test_snapshot_v2_fields_default_to_none_when_absent():
    snap = _snapshot_with({})            # nothing published yet
    for key in V2_FIELDS:
        assert key in snap, f"{key} missing from snapshot()"
        assert snap[key] is None, f"{key} should be None until its topic publishes"


def test_snapshot_coerces_string_payloads_to_float():
    # MQTT payloads can arrive as bare strings; snapshot() must coerce them so the
    # UI always gets numbers (or None), never a string that breaks formatting.
    snap = _snapshot_with({"grid_l1": "-2056.0", "batt_voltage": "52.85", "batt_ttg": "24840"})
    assert snap["grid_l1"] == -2056.0
    assert snap["batt_voltage"] == 52.85
    assert snap["batt_ttg"] == 24840.0


def test_snapshot_bad_values_become_none_not_exceptions():
    # A non-numeric payload on a numeric topic must degrade to None, not raise.
    snap = _snapshot_with({"batt_current": "n/a", "system_state": None})
    assert snap["batt_current"] is None
    assert snap["system_state"] is None
