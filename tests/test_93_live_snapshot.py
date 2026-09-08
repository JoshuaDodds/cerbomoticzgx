"""Power-flow v2 — live snapshot exposes the richer per-component telemetry.

These cover frontend/live.py's snapshot() contract for the new fields the v2
power-flow cards read (grid/loads per-phase, battery temp/voltage/current/
time-to-go, inverter system-state code, EV lifetime energy + session time).
No broker is needed: snapshot() only reads the in-memory value cache, so we
inject values directly and assert the parsing/None-fallback behaviour.
"""
import time
from datetime import datetime, timedelta

import pytest

from frontend import live
from frontend.live import MqttLive

# Every key the v2 cards depend on, beyond the pre-existing power/SoC fields.
V2_FIELDS = (
    "grid_l1", "grid_l2", "grid_l3",
    "load_l1", "load_l2", "load_l3",
    "batt_temp", "batt_voltage", "batt_current", "batt_ttg",
    "system_state",
    "ev_energy_kwh", "ev_charge_time", "ev_l1_a", "ev_l2_a", "ev_l3_a",
)


def test_dashboard_mqtt_client_id_is_unique_per_runtime_instance(monkeypatch):
    monkeypatch.setattr(live.socket, "gethostname", lambda: "ESS Dev")
    monkeypatch.setattr(live.os, "getpid", lambda: 101)
    assert live.mqtt_client_id() == "cerbo-live-ess-dev-101"

    monkeypatch.setattr(live.os, "getpid", lambda: 102)
    assert live.mqtt_client_id() == "cerbo-live-ess-dev-102"


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


def test_victron_schedule_topics_are_subscribed_as_authoritative_settings():
    topics = MqttLive()._build_topics("portal-123")

    for index in range(5):
        prefix = (
            "N/portal-123/settings/0/Settings/CGwacs/BatteryLife/"
            f"Schedule/Charge/{index}/"
        )
        assert topics[f"victron_schedule_{index}_day"] == prefix + "Day"
        assert topics[f"victron_schedule_{index}_start"] == prefix + "Start"
        assert topics[f"victron_schedule_{index}_duration"] == prefix + "Duration"
        assert topics[f"victron_schedule_{index}_soc"] == prefix + "Soc"


def test_snapshot_exposes_actual_victron_schedule_including_recurring_days():
    snap = _snapshot_with({
        "victron_schedule_0_day": "1",
        "victron_schedule_0_start": "23400",
        "victron_schedule_0_duration": "5400",
        "victron_schedule_0_soc": "80",
        "victron_schedule_1_day": "7",
        "victron_schedule_1_start": "3600",
        "victron_schedule_1_duration": "1800",
        "victron_schedule_1_soc": "65",
        "victron_schedule_2_day": "-1",
    })

    schedule = snap["victron_schedule"]
    assert schedule["available"] is True
    assert schedule["slots"][0] == {
        "index": 0,
        "day": 1,
        "day_label": "Monday",
        "start_seconds": 23400,
        "start": "06:30",
        "duration": 5400,
        "target_soc": 80,
        "enabled": True,
        "complete": True,
    }
    assert schedule["slots"][1]["day_label"] == "Every day"
    assert schedule["slots"][2]["enabled"] is False
    assert schedule["slots"][2]["complete"] is True


def test_snapshot_marks_partially_received_victron_schedule_as_incomplete():
    snap = _snapshot_with({"victron_schedule_0_day": 2})

    schedule = snap["victron_schedule"]
    assert schedule["available"] is True
    assert schedule["slots"][0]["enabled"] is True
    assert schedule["slots"][0]["complete"] is False


def test_snapshot_has_no_authoritative_schedule_before_mqtt_values_arrive():
    assert _snapshot_with({})["victron_schedule"] == {
        "available": False,
        "updated_at": None,
        "slots": [],
    }


def test_schedule_refresh_reads_each_setting_from_victron():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def publish(self, topic, payload="", **_kwargs):
            self.calls.append((topic, payload))

    tracker = MqttLive()
    tracker._client = FakeClient()
    tracker._portal_id = "portal-123"
    tracker._connected = True

    assert tracker.request_victron_schedule_refresh() is True
    assert len(tracker._client.calls) == 20
    assert tracker._client.calls[0] == (
        "R/portal-123/settings/0/Settings/CGwacs/BatteryLife/"
        "Schedule/Charge/0/Day",
        "",
    )
    assert tracker._client.calls[-1] == (
        "R/portal-123/settings/0/Settings/CGwacs/BatteryLife/"
        "Schedule/Charge/4/Soc",
        "",
    )


def test_local_ev_meter_overrides_stale_charging_status_at_idle_power():
    snap = _snapshot_with({
        "ev_w": "4",
        "veh_is_charging": "True",
        "veh_charging_status": "Charging",
        "veh_eta": "15 hr 48 min",
    })

    assert snap["veh_is_charging"] is False
    assert snap["veh_charging_status"] == "Idle"
    assert snap["veh_eta"] == "N/A"


def test_vehicle_status_is_not_overridden_without_local_meter_evidence():
    snap = _snapshot_with({
        "veh_is_charging": "True",
        "veh_charging_status": "Charging",
        "veh_eta": "1 hr 5 min",
    })

    assert snap["veh_is_charging"] == "True"
    assert snap["veh_charging_status"] == "Charging"
    assert snap["veh_eta"] == "1 hr 5 min"


def test_explicit_idle_vehicle_never_exposes_stale_eta_without_meter_sample():
    snap = _snapshot_with({
        "veh_is_charging": "False",
        "veh_charging_status": "Idle",
        "veh_eta": "4 hr 12 min",
    })

    assert snap["veh_is_charging"] == "False"
    assert snap["veh_charging_status"] == "Idle"
    assert snap["veh_eta"] == "N/A"


def test_vehicle_charging_status_remains_when_ev_meter_shows_real_draw():
    snap = _snapshot_with({
        "ev_w": "2380",
        "veh_is_charging": "True",
        "veh_charging_status": "Charging",
    })

    assert snap["veh_is_charging"] == "True"
    assert snap["veh_charging_status"] == "Charging"


def test_snapshot_exposes_vehicle_telemetry_connection_status():
    snap = _snapshot_with({"veh_telemetry_status": "DISCONNECTED"})

    assert snap["veh_telemetry_status"] == "DISCONNECTED"


def test_snapshot_exposes_vehicle_update_age_for_local_timestamp():
    updated_at = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(time.time() - 60),
    )

    snap = _snapshot_with({"veh_last_update": updated_at})

    assert 59 <= snap["veh_last_update_age_s"] <= 62


def test_snapshot_vehicle_update_age_is_none_when_timestamp_is_unavailable():
    assert _snapshot_with({})["veh_last_update_age_s"] is None
    assert _snapshot_with({"veh_last_update": "not-a-date"})[
        "veh_last_update_age_s"
    ] is None


def test_snapshot_exposes_tibber_day_totals_and_house_only_consumption():
    snap = _snapshot_with({
        "day_import_kwh": "61.676",
        "day_import_cost": "9.732679",
        "day_export_kwh": "0.097",
        "day_export_reward": "0.019341",
        "day_energy_last_update": "2026-07-26 18:30:38",
        "load_actual_today_wh": "30659.79",
        "ev_actual_today_kwh": "12.27",
    })

    assert snap["day_import_kwh"] == pytest.approx(61.676)
    assert snap["day_import_cost"] == pytest.approx(9.732679)
    assert snap["day_export_kwh"] == pytest.approx(0.097)
    assert snap["day_export_reward"] == pytest.approx(0.019341)
    assert snap["day_energy_last_update"] == "2026-07-26 18:30:38"
    assert snap["house_day_kwh"] == pytest.approx(18.38979)
    assert snap["house_day_energy_quality"] == "authoritative_anchor"


def test_snapshot_does_not_claim_house_only_total_without_ev_day_meter():
    snap = _snapshot_with({"load_actual_today_wh": "30659.79"})

    assert snap["house_day_kwh"] is None
    assert snap["house_day_energy_quality"] == "unavailable"


def test_house_day_total_integrates_live_non_ev_power_between_vrm_anchors():
    tracker = MqttLive()
    now = datetime(2026, 7, 26, 18, 30)

    tracker._record_value(
        "ev_actual_today_kwh", 2.0, now=now, monotonic_now=0.0
    )
    tracker._record_value(
        "load_actual_today_wh", 10000.0, now=now, monotonic_now=0.0
    )
    tracker._record_value("load_w", 2000.0, now=now, monotonic_now=1.0)
    tracker._record_value("ev_w", 500.0, now=now, monotonic_now=1.0)
    tracker._record_value(
        "load_w",
        2000.0,
        now=now + timedelta(seconds=60),
        monotonic_now=61.0,
    )
    tracker._connected = True

    snap = tracker.snapshot()
    assert snap["house_day_kwh"] == pytest.approx(8.025)
    assert snap["house_day_energy_quality"] == "live_integrated"


def test_house_day_total_skips_long_mqtt_gaps_and_reanchors_to_vrm():
    tracker = MqttLive()
    now = datetime(2026, 7, 26, 18, 30)

    tracker._record_value(
        "ev_actual_today_kwh", 2.0, now=now, monotonic_now=0.0
    )
    tracker._record_value(
        "load_actual_today_wh", 10000.0, now=now, monotonic_now=0.0
    )
    tracker._record_value("load_w", 4000.0, now=now, monotonic_now=1.0)
    tracker._record_value("ev_w", 1000.0, now=now, monotonic_now=1.0)
    tracker._record_value(
        "load_w",
        4000.0,
        now=now + timedelta(minutes=10),
        monotonic_now=601.0,
    )
    tracker._connected = True

    # A disconnected ten-minute interval must not be invented from one old power
    # sample. The authoritative 8 kWh anchor therefore remains unchanged.
    assert tracker.snapshot()["house_day_kwh"] == pytest.approx(8.0)

    tracker._record_value(
        "ev_actual_today_kwh",
        2.25,
        now=now + timedelta(minutes=15),
        monotonic_now=901.0,
    )
    tracker._record_value(
        "load_actual_today_wh",
        11250.0,
        now=now + timedelta(minutes=15),
        monotonic_now=901.0,
    )
    snap = tracker.snapshot()
    assert snap["house_day_kwh"] == pytest.approx(9.0)
    assert snap["house_day_energy_quality"] == "authoritative_anchor"


def test_house_day_total_resets_instead_of_integrating_across_midnight():
    tracker = MqttLive()
    before_midnight = datetime(2026, 7, 26, 23, 59, 30)
    after_midnight = datetime(2026, 7, 27, 0, 0, 15)

    tracker._record_value(
        "ev_actual_today_kwh", 5.0, now=before_midnight, monotonic_now=0.0
    )
    tracker._record_value(
        "load_actual_today_wh",
        25000.0,
        now=before_midnight,
        monotonic_now=0.0,
    )
    tracker._record_value(
        "load_w", 2000.0, now=before_midnight, monotonic_now=1.0
    )
    tracker._record_value(
        "ev_w", 0.0, now=before_midnight, monotonic_now=1.0
    )
    tracker._record_value(
        "load_actual_today_wh",
        100.0,
        now=after_midnight,
        monotonic_now=46.0,
    )
    tracker._record_value(
        "ev_actual_today_kwh",
        0.0,
        now=after_midnight,
        monotonic_now=46.0,
    )
    tracker._connected = True

    assert tracker.snapshot()["house_day_kwh"] == pytest.approx(0.1)


def test_house_day_total_stays_unknown_after_rollover_until_new_anchors_arrive():
    tracker = MqttLive()
    before_midnight = datetime(2026, 7, 26, 23, 59, 30)
    after_midnight = datetime(2026, 7, 27, 0, 0, 1)

    tracker._record_value(
        "ev_actual_today_kwh", 5.0, now=before_midnight, monotonic_now=0.0
    )
    tracker._record_value(
        "load_actual_today_wh",
        25000.0,
        now=before_midnight,
        monotonic_now=0.0,
    )
    # An unrelated live update crosses midnight before either daily counter has
    # reset. The stale values remain in the generic cache but must not be used as
    # a fallback for the new day.
    tracker._record_value(
        "soc", 80.0, now=after_midnight, monotonic_now=31.0
    )
    tracker._connected = True

    snap = tracker.snapshot()
    assert snap["house_day_kwh"] is None
    assert snap["house_day_energy_quality"] == "unavailable"


def test_rollover_guard_does_not_hide_a_valid_zero_import_day_after_midnight():
    assert live._midnight_counter_mismatch(
        25000.0,
        0.0,
        now=datetime(2026, 7, 27, 0, 10),
    )
    assert not live._midnight_counter_mismatch(
        25000.0,
        0.0,
        now=datetime(2026, 7, 27, 12, 0),
    )
