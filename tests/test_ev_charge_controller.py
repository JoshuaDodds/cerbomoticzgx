"""Tests for the cleaned-up EV charge control logic.

Built via __new__ to skip __init__ (which constructs a TeslaApi + MQTT client). We inject
a fake Tesla that records commands, a dict-like state, and shadow the dynamic bus
properties with plain instance attributes.
"""
import json
import importlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from lib import ev_charge_controller as ecc

_TEST_CONTROLLER_STATE_PATH = None


@pytest.fixture(autouse=True)
def _isolate_controller_state(tmp_path):
    """Never let controller tests write suppression/reminder state into data/."""
    global _TEST_CONTROLLER_STATE_PATH
    _TEST_CONTROLLER_STATE_PATH = str(tmp_path / "controller-state.json")
    yield
    _TEST_CONTROLLER_STATE_PATH = None


class FakeTesla:
    def __init__(self, **kw):
        self.is_home = kw.get("is_home", True)
        self.is_plugged = kw.get("is_plugged", True)
        self.is_supercharging = kw.get("is_supercharging", False)
        self.is_full = kw.get("is_full", False)
        self.is_charging = kw.get("is_charging", False)
        self.vehicle_soc = 50
        self.vehicle_soc_setpoint = 80
        self.charge_limit_update_ts = time.time()
        self.charge_current_request_update_ts = 0
        self.charge_state_update_ts = time.time()
        self.time_until_full = "N/A"
        # Fresh by default so existing is_charging=False assertions keep behaving as "confirmed
        # off"; tests exercising staleness override this explicitly.
        self.last_update_ts = kw.get("last_update_ts", time.time())
        self.calls = []

    def start_tesla_charge(self):
        self.calls.append("start"); self.is_charging = True; return True

    def stop_tesla_charge(self):
        self.calls.append("stop"); self.is_charging = False; return "ok"

    def set_tesla_charge_amps(self, amps, installation_ceiling=None):
        call = (("amps", amps) if installation_ceiling is None
                else ("amps", amps, installation_ceiling))
        self.calls.append(call); return True

    def update_vehicle_status(self, force=False, allow_wake=False):
        self.calls.append(("update_vehicle_status", force, allow_wake))

    def set_tesla_charge_limit(self, percent):
        self.calls.append(("limit", percent)); self.vehicle_soc_setpoint = percent
        return True, "ok"

    def upsert_owned_charge_schedule(self, schedule_id, **kwargs):
        self.calls.append(("schedule", schedule_id, kwargs)); return True, "ok"

    def remove_owned_charge_schedule(self, schedule_id):
        self.calls.append(("remove_schedule", schedule_id)); return True, "ok"


class FakeState(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def set(self, k, v):
        self[k] = v


def _charger(monkeypatch, tesla, state=None, **attrs):
    monkeypatch.setattr(ecc, "publish_message", lambda *a, **k: None)
    monkeypatch.setattr(ecc, "pushover_notification", lambda *a, **k: None)
    monkeypatch.setattr(ecc, "pushover_notification_critical", lambda *a, **k: None)
    monkeypatch.setattr(ecc.EvCharger, "is_the_sun_shining", staticmethod(lambda: attrs.get("sun", True)))
    c = ecc.EvCharger.__new__(ecc.EvCharger)
    c.tesla = tesla
    c.global_state = state if state is not None else FakeState()
    c.minimum_ess_soc = 90
    c._last_command_ts = 0.0
    c._last_commanded_amps = None
    c._low_surplus_since = None
    c._intent_off_edge = False
    c._intent_was_on = False
    c._charge_mode = attrs.get("charge_mode", None)
    c._smart_owns_charge = False
    c._stop_backoff_until = 0.0
    c._last_stop_alert_ts = 0.0
    c._stop_attempt_count = 0
    c._stop_escalated = False
    c._last_status_state = None
    c.ess_soc = attrs.get("ess_soc", 95)
    c.surplus_amps = attrs.get("surplus_amps", 6)
    c.surplus_watts = attrs.get("surplus_watts", 0)
    c.charging_amps = attrs.get("charging_amps", 0)
    # Only exercised by main() (dynamic_load_reservation_adjustment) — most tests call the
    # smaller helpers directly and never touch these, but main() needs them set.
    c.load_reservation = attrs.get("load_reservation", 1000)
    c.load_reservation_is_reduced = False
    c.load_reservation_reduction_factor = 2
    return c


def _smart_settings(monkeypatch, **overrides):
    values = {
        "EV_SMART_CHARGE_ENABLED": "True",
        "EV_SMART_CHARGE_APPLY": "True",
        "TESLA_TELEMETRY_ENABLED": "True",
        "EV_CHARGER_MAX_AMPS": "24",
        "EV_PLUG_REMINDER_ENABLED": "False",
        "EV_SMART_CHARGE_CONTROLLER_STATE_PATH": _TEST_CONTROLLER_STATE_PATH,
        "HOME_ADDRESS_LAT": "52.1",
        "HOME_ADDRESS_LONG": "5.1",
    }
    values.update(overrides)
    monkeypatch.setattr(ecc, "retrieve_setting", lambda key: values.get(key))


def test_owned_schedule_id_is_stable_epoch_seconds_not_a_decorative_uint64():
    # Tesla's command proxy generates omitted schedule IDs from Unix seconds. Keep our
    # deterministic owned ID in that same representation so older vehicles accept it.
    assert ecc.SMART_OWNED_SCHEDULE_ID == 1_784_592_000
    assert 1_700_000_000 <= ecc.SMART_OWNED_SCHEDULE_ID < 2_000_000_000
    assert ecc.SMART_LEGACY_OWNED_SCHEDULE_IDS == (4_847_371_018_685_470_720,)


def _smart_plan(now, *, active=True, target_kw=16.0, generated_at=None,
                status="planned", job_status="active"):
    start = now - timedelta(minutes=1) if active else now + timedelta(minutes=15)
    end = now + timedelta(minutes=14) if active else now + timedelta(minutes=30)
    ready_by = now + timedelta(hours=5)
    return {
        "generated_at": (generated_at or now).isoformat(),
        "status": status,
        "job": {
            "id": "job-1",
            "status": job_status,
            "target_soc": 80,
            "ready_by": ready_by.isoformat(),
        },
        "target_soc": 80,
        "ready_by": ready_by.isoformat(),
        "latest_safe_start": (now + timedelta(hours=1)).isoformat(),
        "slots": [{
            "start": start.isoformat(),
            "end": end.isoformat(),
            "target_kw": target_kw,
            "energy_kwh": target_kw * 0.25,
        }],
        "blocks": [],
    }


def _run_reminder_threads_inline(monkeypatch):
    created = []

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            created.append(self)

        def start(self):
            self.target()

    monkeypatch.setattr(ecc.threading, "Thread", ImmediateThread)
    return created


def test_dormant_reason_does_not_report_unknown_startup_state_as_away(
        monkeypatch):
    tesla = FakeTesla(is_home=None, is_plugged=None)
    charger = _charger(monkeypatch, tesla, surplus_amps=0)

    assert charger._dormant_reason() == (
        "vehicle state awaiting telemetry; no charge intent"
    )


def test_dormant_reason_reports_only_explicit_vehicle_availability(
        monkeypatch):
    away = _charger(
        monkeypatch, FakeTesla(is_home=False, is_plugged=None), surplus_amps=0
    )
    unplugged = _charger(
        monkeypatch, FakeTesla(is_home=True, is_plugged=False), surplus_amps=0
    )

    assert away._dormant_reason() == "vehicle away; no charge intent"
    assert unplugged._dormant_reason() == "vehicle unplugged; no charge intent"


def test_starts_surplus_charge_when_home_plugged_and_surplus(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=6, charging_amps=0)
    active = c._control_charging()
    assert active is True
    assert ("amps", 6, 25.0) in tesla.calls    # set current to surplus
    assert "start" in tesla.calls


def test_does_not_touch_car_when_not_plugged(monkeypatch):
    tesla = FakeTesla(is_plugged=False, is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=6)
    assert c._control_charging() is False
    assert tesla.calls == []                    # never command a car that isn't plugged in


def test_intent_off_stops_charge_immediately(monkeypatch):
    # Turning grid-assist / charge request off must stop the car right away (no grace).
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, charging_amps=8)
    c._intent_off_edge = True
    assert c._control_charging() is False
    assert "stop" in tesla.calls


def test_engaged_while_charging_even_without_local_meter(monkeypatch):
    # If the local charger meter reads 0 but the car is charging (cached), we must still
    # consider ourselves engaged so we can manage/stop it.
    _smart_settings(monkeypatch, EV_SMART_CHARGE_ENABLED="False")
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    assert c._local_engagement_signal() is True


def test_known_unplugged_vehicle_does_not_engage_for_surplus(monkeypatch):
    """Pushed unplugged state must suppress irrelevant PV discovery/control ticks."""
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        TESLA_TELEMETRY_ENABLED="True",
    )
    tesla = FakeTesla(is_home=True, is_plugged=False, is_charging=False)
    c = _charger(
        monkeypatch,
        tesla,
        ess_soc=100,
        surplus_amps=6,
        charging_amps=0,
        sun=True,
    )

    assert c._local_engagement_signal() is False


def test_away_vehicle_ignores_stale_cached_charging_and_surplus(monkeypatch):
    """Location wins over stale plug/charge flags when no local current is flowing."""
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        TESLA_TELEMETRY_ENABLED="True",
    )
    tesla = FakeTesla(is_home=False, is_plugged=True, is_charging=True)
    c = _charger(
        monkeypatch,
        tesla,
        ess_soc=100,
        surplus_amps=6,
        charging_amps=0,
        sun=True,
    )

    assert c._local_engagement_signal() is False


def test_local_ev_draw_remains_a_safety_engagement_signal_when_metadata_is_stale(
        monkeypatch):
    """The ABB meter remains authoritative if Tesla location/plug state disagrees."""
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        TESLA_TELEMETRY_ENABLED="True",
    )
    tesla = FakeTesla(is_home=False, is_plugged=False, is_charging=False)
    c = _charger(
        monkeypatch,
        tesla,
        ess_soc=100,
        surplus_amps=0,
        charging_amps=8,
        sun=False,
    )

    assert c._local_engagement_signal() is True


def test_surplus_discovery_is_preserved_when_fleet_telemetry_is_disabled(
        monkeypatch):
    """Legacy REST discovery remains available for installations without telemetry."""
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        TESLA_TELEMETRY_ENABLED="False",
    )
    tesla = FakeTesla(is_home=False, is_plugged=False, is_charging=False)
    c = _charger(
        monkeypatch,
        tesla,
        ess_soc=100,
        surplus_amps=6,
        charging_amps=0,
        sun=True,
    )
    c._last_discovery_wake_ts = 0.0
    c._discovery_backoff_until = 0.0

    assert c._local_engagement_signal() is True


def test_known_unplugged_surplus_tick_stays_dormant_without_tesla_calls(
        monkeypatch):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        TESLA_TELEMETRY_ENABLED="True",
    )
    tesla = FakeTesla(is_home=True, is_plugged=False, is_charging=False)
    c = _charger(
        monkeypatch,
        tesla,
        ess_soc=100,
        surplus_amps=6,
        charging_amps=0,
        sun=True,
    )
    monkeypatch.setattr(c, "_refresh_smart_plan", lambda: None)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)

    c.main()

    assert tesla.calls == []
    assert c._last_status_state == "dormant"


def test_known_home_plugged_surplus_uses_pushed_state_without_discovery_wake(
        monkeypatch):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        TESLA_TELEMETRY_ENABLED="True",
    )
    tesla = FakeTesla(is_home=True, is_plugged=True, is_charging=False)
    c = _charger(
        monkeypatch,
        tesla,
        ess_soc=100,
        surplus_amps=6,
        charging_amps=0,
        sun=True,
    )
    monkeypatch.setattr(c, "_refresh_smart_plan", lambda: None)
    monkeypatch.setattr(c, "_control_charging", lambda: False)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)

    c.main()

    assert tesla.calls == [("update_vehicle_status", False, False)]


def test_controller_does_not_publish_shared_measured_current(monkeypatch):
    """Only the ABB event path may own Tesla/vehicle0/charging_amps."""
    c = _charger(monkeypatch, FakeTesla(), charging_amps=0)
    published = []
    monkeypatch.setattr(ecc, "publish_message", lambda *a, **k: published.append((a, k)))

    c.update_charging_amp_totals(12)

    assert c.global_state["tesla_charging_amps_total"] == 12
    assert not any("Tesla/vehicle0/charging_amps" in str(call) for call in published)


def test_stops_when_full(monkeypatch):
    tesla = FakeTesla(is_full=True, is_charging=True)
    c = _charger(monkeypatch, tesla, charging_amps=6)
    assert c._control_charging() is False
    assert "stop" in tesla.calls


def test_cooldown_blocks_immediate_restart(monkeypatch):
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=6, charging_amps=0)
    c._last_command_ts = time.time()            # a command was just issued
    assert c._control_charging() is True        # wants to charge...
    assert tesla.calls == []                     # ...but cooldown suppresses re-issuing


def test_surplus_loss_waits_grace_then_stops(monkeypatch):
    # Charging, but surplus and intent are both gone.
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, surplus_amps=0, charging_amps=8,
        charge_mode="surplus")
    # First tick: opens the grace window, does NOT stop yet (ride out a passing cloud).
    assert c._control_charging() is True
    assert tesla.calls == []
    assert c._low_surplus_since is not None
    # Grace elapsed and cooldown clear -> stop.
    c._low_surplus_since = time.time() - (ecc.SURPLUS_LOSS_GRACE_S + 1)
    c._last_command_ts = 0.0
    assert c._control_charging() is False
    assert "stop" in tesla.calls


def test_surplus_recovery_cancels_pending_stop(monkeypatch):
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, surplus_amps=0, charging_amps=8,
        charge_mode="surplus")
    c._control_charging()                        # opens grace window
    assert c._low_surplus_since is not None
    c.surplus_amps = 6                           # surplus came back
    c._control_charging()
    assert c._low_surplus_since is None          # pending stop cancelled
    assert "stop" not in tesla.calls


def test_adjusts_amps_to_track_surplus_while_charging(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=10, charging_amps=6)   # surplus rose to 10
    assert c._control_charging() is True
    assert ("amps", 10, 25.0) in tesla.calls
    assert "start" not in tesla.calls            # already charging; only adjust


def test_discovery_wake_is_rate_limited(monkeypatch):
    import time as _t
    c = _charger(monkeypatch, FakeTesla())
    c._last_discovery_wake_ts = 0.0
    c._discovery_backoff_until = 0.0
    assert c._should_discovery_wake() is True                      # never woken -> allowed
    c._last_discovery_wake_ts = _t.time()
    assert c._should_discovery_wake() is False                     # within the hourly interval
    c._last_discovery_wake_ts = _t.time() - (ecc.DISCOVERY_WAKE_INTERVAL_S + 1)
    assert c._should_discovery_wake() is True                      # interval elapsed
    c._discovery_backoff_until = _t.time() + 100                   # away-backoff active
    assert c._should_discovery_wake() is False


def test_intent_follows_dedicated_flag_not_grid_assist(monkeypatch):
    # Intent remains dedicated; grid-assist is a required authorization gate,
    # not an alternate source of EV intent.
    state = FakeState({"grid_charging_enabled": "True", "tesla_charge_requested": "True",
                       "ev_charge_requested": "False"})
    c = _charger(monkeypatch, FakeTesla(is_charging=False), state=state, surplus_amps=0, charging_amps=0)
    assert c._intent_on() is False
    assert c._local_engagement_signal() is False        # grid-assist on does NOT engage the car
    state["ev_charge_requested"] = "True"
    assert c._intent_on() is True
    assert c._manual_grid_override_on() is True


def test_grid_assist_toggle_never_commands_the_car(monkeypatch):
    # Toggling grid-assist on with a home+plugged car and no surplus must issue NO commands:
    # it is permission for a manual override, never EV intent by itself.
    state = FakeState({"grid_charging_enabled": "True", "ev_charge_requested": "False"})
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    assert c._control_charging() is False
    assert tesla.calls == []


def test_grid_assist_alone_does_not_authorize_external_charge(monkeypatch):
    state = FakeState({
        "grid_charging_enabled": "True",
        "ev_charge_requested": "False",
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0,
        charging_amps=16, sun=False)

    assert c._control_charging() is False
    assert tesla.calls == ["stop"]


def test_ev_start_without_grid_assist_cannot_force_charge(monkeypatch):
    state = FakeState({
        "grid_charging_enabled": "False",
        "ev_charge_requested": "True",
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0,
        charging_amps=0, sun=False)

    assert c._control_charging() is False
    assert tesla.calls == []


def test_disabling_grid_assist_stops_manual_ev_override(monkeypatch):
    state = FakeState({
        "grid_charging_enabled": "False",
        "ev_charge_requested": "True",
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0,
        charging_amps=16, sun=False, charge_mode="grid")

    assert c._control_charging() is False
    assert tesla.calls == ["stop"]


@pytest.mark.parametrize(
    ("configured", "expected"),
    (
        ("1", 1),
        ("1.9", 1),
        ("5", 5),
        ("16", 16),
        ("24.9", 24),
        ("25", 25),
    ),
)
def test_ev_charge_request_charges_full_and_ignores_surplus(
        monkeypatch, configured, expected):
    # The dedicated EV-charge request ON with NO surplus is an express override: start charging
    # at the safe full-rate request and do NOT try to match the current to surplus. The
    # installation ceiling (EV_CHARGER_MAX_AMPS) is configurable anywhere from 1-25 A/phase —
    # the grid, inverters and EV charger can all sustain a full 25 A, with Maxem.io independently
    # guarding against fuse overload. EV_CHARGER_MAX_KW is pinned well above any of those
    # ceilings so the requested current always saturates to the configured max, regardless of
    # the real .env's EV_CHARGER_MAX_KW value.
    values = {"EV_CHARGER_MAX_AMPS": configured, "EV_CHARGER_MAX_KW": "100"}
    original_setting = ecc.retrieve_setting
    monkeypatch.setattr(
        ecc, "retrieve_setting", lambda key: values.get(key, original_setting(key)))
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    assert c._control_charging() is True
    assert "start" in tesla.calls
    assert ("amps", expected, float(configured)) in tesla.calls


def test_pv_surplus_uses_last_available_current_without_rewriting_config(
        monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    state = FakeState({
        "tesla_charge_current_max": 7,
        "tesla_charge_current_max_updated_at": time.time(),
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=20, charging_amps=0)

    assert c._control_charging() is True
    assert ("amps", 7, 25.0) in tesla.calls
    assert c._smart_installation_ceiling() == 25.0


def test_pv_surplus_does_not_start_when_available_current_is_zero(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    state = FakeState({"tesla_charge_current_max": 0})
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=20, charging_amps=0)

    assert c._control_charging() is False
    assert tesla.calls == []


def test_pv_surplus_keeps_last_available_current_because_stream_is_change_driven(
        monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    state = FakeState({
        "tesla_charge_current_max": 7,
        "tesla_charge_current_max_updated_at": (
            time.time() - ecc.SMART_COMMAND_ACK_TIMEOUT_S - 1
        ),
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=20, charging_amps=0)

    assert c._control_charging() is True
    assert ("amps", 7, 25.0) in tesla.calls


def test_ev_current_ceiling_is_hard_bounded_to_25_amps(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="32")
    c = _charger(monkeypatch, FakeTesla(), surplus_amps=32)

    assert c._smart_installation_ceiling() == 25.0
    assert c._surplus_target_amps() == 25


def test_ev_charge_request_does_not_stop_on_low_surplus(monkeypatch):
    # Charging under an EV-charge request while surplus is negative must NOT trigger a
    # surplus-loss stop.
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=16)
    assert c._control_charging() is True
    assert "stop" not in tesla.calls


def test_rejected_stop_backs_off_alerts_and_does_not_loop(monkeypatch):
    # A network-failed stop must not retry on the next tick, and must fire a Pushover alert.
    tesla = FakeTesla(is_charging=True)
    tesla.stop_tesla_charge = lambda: (tesla.calls.append("stop"), "network")[1]   # network failure
    c = _charger(monkeypatch, tesla, charge_mode="grid", surplus_amps=0, charging_amps=16)
    alerts = []
    monkeypatch.setattr(ecc, "pushover_notification", lambda *a, **k: alerts.append(a))
    c._intent_off_edge = True
    c._control_charging()
    assert tesla.calls.count("stop") == 1
    assert len(alerts) == 1                # user alerted for manual intervention
    c._intent_off_edge = False
    c._control_charging()                 # still within STOP_RETRY_BACKOFF_S -> no retry
    assert tesla.calls.count("stop") == 1


def test_stop_skipped_when_nothing_is_drawing(monkeypatch):
    # Local meter ~0 and not charging -> nothing to stop; never command or wake the car.
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, charging_amps=0)
    c._intent_off_edge = True
    c._stop_charge("nothing to stop", force=True)
    assert tesla.calls == []


def test_failed_stop_does_not_lie_about_meter(monkeypatch):
    # A 'network'/failed stop must NOT zero the meter or clear the charging flag, so the next
    # tick still sees the car drawing and re-issues the stop (car draining is the risk).
    tesla = FakeTesla(is_charging=True)
    tesla.stop_tesla_charge = lambda: (tesla.calls.append("stop"), "network")[1]
    monkeypatch.setattr(ecc, "pushover_notification", lambda *a, **k: None)
    c = _charger(monkeypatch, tesla, charging_amps=12)
    zeroed = {"n": 0}
    c.update_charging_amp_totals = lambda v=None: zeroed.__setitem__("n", zeroed["n"] + 1)
    c._stop_charge("stop it", force=True)
    assert tesla.calls.count("stop") == 1
    assert zeroed["n"] == 0                     # meter NOT forced to 0 on a failed stop
    assert tesla.is_charging is True            # still flagged charging -> re-stop next tick


def test_accepted_stop_waits_for_local_meter_and_does_not_send_duplicate(
        monkeypatch):
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, charging_amps=12)
    zeroed = {"n": 0}
    c.update_charging_amp_totals = (
        lambda value=None: zeroed.__setitem__("n", zeroed["n"] + 1)
    )

    # Tesla accepted the stop, but the physical meter has not settled yet.
    assert c._stop_charge("stop once", force=True) is True
    assert tesla.calls.count("stop") == 1
    assert zeroed["n"] == 0

    # A controller tick during the documented observation grace must not buy a
    # duplicate stop while the accepted command is still settling.
    assert c._stop_charge("stop once", force=True) is True
    assert tesla.calls.count("stop") == 1

    # The next real ABB zero confirms the accepted stop without another API call.
    c.charging_amps = 0
    tesla.is_charging = False
    assert c._stop_charge("stop once", force=True) is True
    assert tesla.calls.count("stop") == 1
    assert c._stop_confirmation_pending["observed_stopped"] is True

    # A stale charging flag arriving after confirmation cannot reopen the API
    # path during the accepted-command idempotency window.
    tesla.is_charging = True
    assert c._stop_charge("stop once", force=True) is True
    assert tesla.calls.count("stop") == 1


def test_real_meter_restart_breaks_accepted_stop_idempotency_guard(monkeypatch):
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, charging_amps=12)

    assert c._stop_charge("stop once", force=True) is True
    c.charging_amps = 0
    tesla.is_charging = False
    assert c._stop_charge("observe stop", force=True) is True

    # If physical current genuinely resumes, safety wins over suppression.
    c.charging_amps = 12
    tesla.is_charging = True
    assert c._stop_charge("unauthorized restart", force=True) is True
    assert tesla.calls.count("stop") == 2


def test_accepted_stop_retries_only_after_meter_confirmation_grace(monkeypatch):
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, charging_amps=12)

    assert c._stop_charge("stop until observed", force=True) is True
    assert tesla.calls.count("stop") == 1
    c._stop_confirmation_pending["sent_at"] -= (
        ecc.STOP_CONFIRMATION_GRACE_S + 1)

    assert c._stop_charge("stop until observed", force=True) is True
    assert tesla.calls.count("stop") == 2
    assert c._stop_confirmation_pending["attempts"] == 2


def test_stale_not_charging_status_does_not_skip_the_stop(monkeypatch):
    # Regression: a stale/unconfirmed tesla.is_charging=False (e.g. the last forced refresh
    # failed) must NOT be trusted as "confirmed not charging" — even when the local meter is
    # also below 1A, the stop must still be attempted, not silently skipped.
    tesla = FakeTesla(is_charging=False, last_update_ts=time.time() - (ecc.STALE_STATUS_MAX_AGE_S + 60))
    c = _charger(monkeypatch, tesla, charging_amps=0)
    c._intent_off_edge = True
    c._stop_charge("nothing to stop?", force=True)
    assert "stop" in tesla.calls


def test_fresh_not_charging_status_still_skips_the_stop(monkeypatch):
    # Sanity check the freshness gate doesn't break the original M1/H1 behavior: a genuinely
    # fresh confirmation of "not charging" plus a near-zero meter is still treated as nothing
    # to stop, so we don't wake the car for no reason.
    tesla = FakeTesla(is_charging=False, last_update_ts=time.time())
    c = _charger(monkeypatch, tesla, charging_amps=0)
    c._intent_off_edge = True
    c._stop_charge("nothing to stop", force=True)
    assert tesla.calls == []


def test_fresh_idle_abb_meter_overrides_stale_tesla_charging_flag(monkeypatch):
    """A fresh 4 W charger sample must prevent paid stop loops."""
    state = FakeState({
        "tesla_power": 4,
        "tesla_power_updated_at": time.time(),
    })
    tesla = FakeTesla(
        is_charging=True,
        last_update_ts=time.time() - (ecc.STALE_STATUS_MAX_AGE_S + 60),
    )
    c = _charger(
        monkeypatch, tesla, state=state, charging_amps=0,
        surplus_amps=0,
    )

    assert c._charging_now() is False
    assert c._stop_charge(
        "charging is outside controller-authorized conditions",
        force=True,
    ) is True
    assert tesla.calls == []


def test_confirmed_idle_stop_does_not_reopen_after_grace(monkeypatch):
    """Accepted ``not_charging`` plus ABB idle remains terminal across ticks."""
    state = FakeState({
        "tesla_power": 8000,
        "tesla_power_updated_at": time.time(),
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, state=state, charging_amps=12,
        surplus_amps=0,
    )

    assert c._stop_charge("unauthorized charge", force=True) is True
    assert tesla.calls.count("stop") == 1

    c.charging_amps = 0
    state["tesla_power"] = 4
    state["tesla_power_updated_at"] = time.time()
    tesla.is_charging = True  # deliberately stale/change-driven
    c._stop_confirmation_pending["sent_at"] -= (
        ecc.STOP_CONFIRMATION_GRACE_S + 1
    )

    # First call closes the observation lifecycle; later ticks must still trust
    # fresh ABB idle and never reopen another Tesla command.
    assert c._stop_charge("unauthorized charge", force=True) is True
    assert c._stop_charge("unauthorized charge", force=True) is True
    assert c._stop_charge("unauthorized charge", force=True) is True
    assert tesla.calls.count("stop") == 1


def test_stop_retries_are_bounded_then_escalates_critical(monkeypatch):
    # A persistently-failing stop must retry at most STOP_MAX_RETRIES times, then send a
    # CRITICAL Pushover alert and stop auto-retrying (bounds the budget-bypassing spend).
    tesla = FakeTesla(is_charging=True)
    tesla.stop_tesla_charge = lambda: (tesla.calls.append("stop"), "network")[1]
    c = _charger(monkeypatch, tesla, charging_amps=16)
    critical_alerts = []
    monkeypatch.setattr(ecc, "pushover_notification_critical", lambda *a, **k: critical_alerts.append(a))

    for _ in range(ecc.STOP_MAX_RETRIES):
        c._stop_charge("EV-charge request turned off", force=True)
        c._stop_backoff_until = 0.0   # skip the real backoff wait between attempts in the test

    assert tesla.calls.count("stop") == ecc.STOP_MAX_RETRIES
    assert c._stop_escalated is True
    assert len(critical_alerts) == 1

    # Further attempts must NOT call stop_tesla_charge again — escalated, waiting on a human.
    c._stop_charge("EV-charge request turned off", force=True)
    assert tesla.calls.count("stop") == ecc.STOP_MAX_RETRIES


def test_fresh_intent_off_edge_resets_escalation(monkeypatch):
    # A brand-new, deliberate stop request (a fresh intent-off edge) gets its own bounded
    # attempts rather than staying silently suppressed by a prior escalation.
    tesla = FakeTesla(is_charging=True)
    tesla.stop_tesla_charge = lambda: (tesla.calls.append("stop"), "network")[1]
    c = _charger(monkeypatch, tesla, charge_mode="grid", charging_amps=16)
    c._stop_attempt_count = ecc.STOP_MAX_RETRIES
    c._stop_escalated = True
    c._intent_off_edge = True
    c._fresh_stop_request = True
    c._control_charging()
    assert "stop" in tesla.calls
    assert c._stop_attempt_count == 1
    assert c._stop_escalated is False


def test_engagement_signal_dormant_when_idle(monkeypatch):
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, ess_soc=95, surplus_amps=0, charging_amps=0, sun=True)
    # No intent, no surplus (0A), not charging locally -> nothing should engage the API.
    assert c._local_engagement_signal() is False
    # Grid-assist must NOT engage the car (decoupled).
    c.global_state.set("grid_charging_enabled", "True")
    assert c._local_engagement_signal() is False
    # The dedicated EV-charge flag flips it on.
    c.global_state.set("ev_charge_requested", "True")
    assert c._local_engagement_signal() is True


def test_refresh_requested_reads_dedicated_flag(monkeypatch):
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla)
    assert c._refresh_requested() is False
    c.global_state.set("vehicle_refresh_requested", "True")
    assert c._refresh_requested() is True


def test_manual_stop_request_forces_stop_even_when_charge_intent_was_already_off(
        monkeypatch):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        EV_SMART_CHARGE_APPLY="False",
    )
    state = FakeState({
        "ev_charge_requested": False,
        "vehicle_stop_requested": True,
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0,
                 charging_amps=8, sun=False)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)

    c.main()

    assert state["vehicle_stop_requested"] is False
    assert "stop" in tesla.calls


def test_manual_stop_uses_local_draw_when_vehicle_location_state_is_stale(
        monkeypatch):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        EV_SMART_CHARGE_APPLY="False",
    )
    state = FakeState({
        "ev_charge_requested": False,
        "vehicle_stop_requested": True,
    })
    tesla = FakeTesla(is_charging=False, is_home=False, is_plugged=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0,
                 charging_amps=8, sun=False)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)

    c.main()

    assert "stop" in tesla.calls


def test_manual_stop_stays_latched_until_a_retry_is_confirmed(monkeypatch):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        EV_SMART_CHARGE_APPLY="False",
    )
    state = FakeState({
        "ev_charge_requested": False,
        "vehicle_stop_requested": True,
    })
    tesla = FakeTesla(is_charging=True)
    outcomes = iter(("network", "ok"))

    def stop():
        result = next(outcomes)
        tesla.calls.append("stop")
        if result == "ok":
            tesla.is_charging = False
        return result

    tesla.stop_tesla_charge = stop
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0,
                 charging_amps=8, sun=False)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)

    c.main()
    assert state["vehicle_stop_requested"] is True
    assert c._stop_attempt_count == 1

    c._stop_backoff_until = 0.0
    c.main()

    assert tesla.calls.count("stop") == 2
    assert tesla.calls.count(("update_vehicle_status", True, False)) == 1
    assert tesla.calls.count(("update_vehicle_status", False, False)) == 1
    assert state["vehicle_stop_requested"] is False
    assert c._stop_attempt_count == 0


def test_refresh_request_forces_wake_and_clears_itself(monkeypatch):
    # A full main() tick with a pending refresh request, and NO other engagement signal
    # (no intent, no surplus, not charging) must: stay engaged rather than take the dormant
    # early-return (proven by update_vehicle_status being reached at all), force a wake+refresh
    # read, and clear the one-shot flag so it doesn't re-trigger next tick.
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        EV_SMART_CHARGE_APPLY="False",
    )
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, ess_soc=95, surplus_amps=0, charging_amps=0, sun=False)
    c.global_state.set("vehicle_refresh_requested", "True")
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)  # no real Timer/thread in tests
    monkeypatch.setattr(c, "_control_charging", lambda: False)

    c.main()

    assert ("update_vehicle_status", True, True) in tesla.calls
    assert c.global_state.get("vehicle_refresh_requested") is False


def test_refresh_request_does_not_recur_on_next_tick(monkeypatch):
    # After being consumed, a stale True lingering anywhere must not force a wake every tick.
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="False",
        EV_SMART_CHARGE_APPLY="False",
    )
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, ess_soc=95, surplus_amps=0, charging_amps=0, sun=False)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)
    monkeypatch.setattr(c, "_control_charging", lambda: False)
    c.global_state.set("vehicle_refresh_requested", "True")

    c.main()   # consumes + clears the flag
    tesla.calls.clear()
    c.main()   # nothing should re-engage the controller this time

    assert tesla.calls == []


def test_refresh_clear_does_not_repeat_forced_wake_with_active_smart_job(monkeypatch):
    """An applied job may keep polling, but must not repeat the manual forced wake."""
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, ess_soc=95, surplus_amps=0,
                 charging_amps=0, sun=False)
    c._smart_plan = plan
    c._smart_job = {
        "id": plan["job"]["id"],
        "status": plan["job"]["status"],
        "target_soc": plan["target_soc"],
        "ready_by": plan["ready_by"],
    }
    c._smart_job_loaded = True
    monkeypatch.setattr(c, "_refresh_smart_plan", lambda: None)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)
    monkeypatch.setattr(c, "_control_charging", lambda: False)
    c.global_state.set("vehicle_refresh_requested", "True")

    c.main()
    assert ("update_vehicle_status", True, True) in tesla.calls
    assert c.global_state.get("vehicle_refresh_requested") is False

    tesla.calls.clear()
    c.main()

    assert tesla.calls == [("update_vehicle_status", False, False)]
    assert c.global_state.get("vehicle_refresh_requested") is False


def test_smart_apply_false_issues_no_smart_commands(monkeypatch):
    """Shadow planning must never touch the car or install its fallback schedule."""
    _smart_settings(monkeypatch, EV_SMART_CHARGE_APPLY="False")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)

    assert c._control_charging() is False
    assert tesla.calls == []


@pytest.mark.parametrize("charge_mode", ("smart", "smart_solar", "smart_surplus"))
def test_disabling_apply_stops_previously_owned_smart_charge(
        monkeypatch, charge_mode):
    _smart_settings(monkeypatch, EV_SMART_CHARGE_APPLY="False")
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=16)
    c._charge_mode = charge_mode
    c._smart_owns_charge = True

    assert c._control_charging() is False
    assert tesla.calls == ["stop"]
    assert c._charge_mode is None


def test_smart_feature_off_preserves_legacy_surplus_path(monkeypatch):
    _smart_settings(monkeypatch, EV_SMART_CHARGE_ENABLED="False")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=6, charging_amps=0)
    c._smart_plan = _smart_plan(now)

    assert c._control_charging() is True
    assert ("amps", 6, 24.0) in tesla.calls
    assert "start" in tesla.calls
    assert not any(call[0] == "schedule" for call in tesla.calls if isinstance(call, tuple))


def test_shadow_smart_plan_preserves_legacy_surplus_fleet_commands(monkeypatch):
    """The preview toggle must gate only new smart control, never established PV charging."""
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_ENABLED="True",
        EV_SMART_CHARGE_APPLY="False",
    )
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, ess_soc=95, surplus_amps=6, charging_amps=0, sun=True)
    c._smart_plan = _smart_plan(now)

    assert c._local_engagement_signal() is True
    assert c._control_charging() is True
    assert ("amps", 6, 24.0) in tesla.calls
    assert "start" in tesla.calls
    assert not any(call[0] == "schedule" for call in tesla.calls if isinstance(call, tuple))


def test_applied_smart_job_uses_protected_live_surplus_between_blocks(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    c = _charger(
        monkeypatch, tesla, state=state,
        ess_soc=95, surplus_amps=6, charging_amps=0, sun=True,
    )
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging(now=now) is True
    assert ("amps", 6, 24.0) in tesla.calls
    assert "start" in tesla.calls
    assert c._charge_mode == "smart_surplus"
    assert c._smart_owns_charge is True
    assert state["ev_smart_charge_controller_reason"] == "opportunistic_solar_surplus"


def test_applied_smart_job_never_steals_surplus_below_home_battery_target(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    c = _charger(
        monkeypatch, tesla, state=state,
        ess_soc=89, surplus_amps=6, charging_amps=0, sun=True,
    )
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging(now=now) is False
    assert ("amps", 6) not in tesla.calls
    assert "start" not in tesla.calls


def test_applied_smart_job_adopts_external_charge_when_surplus_authorizes_it(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla,
        ess_soc=95, surplus_amps=6, charging_amps=6, sun=True,
    )
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging(now=now) is True
    assert c._smart_owns_charge is True
    assert c._charge_mode == "smart_surplus"
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "opportunistic_solar_surplus"
    )


def test_manual_stop_suppresses_owned_between_block_solar(monkeypatch, tmp_path):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(tmp_path / "controller-state.json"),
    )
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla,
        ess_soc=95, surplus_amps=6, charging_amps=6, sun=True,
        charge_mode="smart_surplus",
    )
    c._smart_plan = _smart_plan(now, active=False)
    c._smart_owns_charge = True
    c._intent_off_edge = True
    c._fresh_stop_request = True

    assert c._control_charging(now=now) is False
    assert "stop" in tesla.calls

    c._intent_off_edge = False
    c.charging_amps = 0
    tesla.is_charging = False
    tesla.calls.clear()
    c._last_command_ts = 0
    assert c._control_charging(now=now + timedelta(minutes=1)) is False
    assert "start" not in tesla.calls


def test_forecast_solar_slot_waits_instead_of_silently_using_grid(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla,
        ess_soc=95, surplus_amps=0, charging_amps=0, sun=True,
    )
    plan = _smart_plan(now, active=True)
    plan["slots"][0].update({
        "supply": "solar", "pv_energy_kwh": 4.0, "grid_energy_kwh": 0.0,
    })
    c._smart_plan = plan

    assert c._control_charging(now=now) is False
    assert not any(isinstance(call, tuple) and call[0] == "amps" for call in tesla.calls)
    assert "start" not in tesla.calls


def test_forecast_solar_slot_caps_request_to_live_surplus(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    c = _charger(
        monkeypatch, tesla, state=state,
        ess_soc=95, surplus_amps=6, charging_amps=0, sun=True,
    )
    plan = _smart_plan(now, active=True, target_kw=16.0)
    plan["slots"][0].update({
        "supply": "solar", "pv_energy_kwh": 4.0, "grid_energy_kwh": 0.0,
    })
    c._smart_plan = plan

    assert c._control_charging(now=now) is True
    assert ("amps", 6, 24.0) in tesla.calls
    assert "start" in tesla.calls
    assert c._charge_mode == "smart_solar"
    assert state["ev_smart_charge_controller_reason"] == "forecast_solar_surplus"


def test_external_start_during_cloudy_solar_slot_is_stopped(
        monkeypatch, tmp_path):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(tmp_path / "controller-state.json"),
    )
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla,
        ess_soc=95, surplus_amps=0, charging_amps=6, sun=True,
    )
    plan = _smart_plan(now, active=True)
    plan["slots"][0].update({
        "supply": "solar", "pv_energy_kwh": 4.0, "grid_energy_kwh": 0.0,
    })
    c._smart_plan = plan

    assert c._control_charging(now=now) is False
    assert tesla.calls == ["stop"]
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "unauthorized_charge_stopped"
    )


def test_applied_smart_job_fails_closed_when_fleet_telemetry_is_disabled(monkeypatch):
    _smart_settings(monkeypatch, TESLA_TELEMETRY_ENABLED="False")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)

    assert c._control_charging(now=now) is False
    assert tesla.calls == []
    assert c.global_state["ev_smart_charge_controller_status"] == "telemetry_required"


def test_manual_charge_request_has_priority_over_active_smart_block(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
    })
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=3.0)

    assert c._control_charging() is True
    assert "start" in tesla.calls
    # Manual grid charging restores the configured installation ceiling instead
    # of inheriting a prior 1-5 A solar request. Maxem owns delivered current.
    assert ("amps", 24, 24.0) in tesla.calls
    assert not any(isinstance(call, tuple) and call[0] in {"schedule", "limit"}
                   for call in tesla.calls)
    assert c._charge_mode == "grid"


def test_manual_grid_override_raises_existing_five_amp_solar_session(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 5,
        "tesla_charge_current_request_updated_at": now.timestamp() - 5,
    })
    c = _charger(
        monkeypatch, tesla, state=state,
        surplus_amps=5, charging_amps=5, charge_mode="surplus",
    )

    assert c._control_charging(now=now) is True
    assert tesla.calls == [("amps", 24, 24.0)]
    assert c._charge_mode == "grid"
    assert state["ev_grid_charge_current_status"] == "confirmation_pending"


def test_manual_grid_session_sends_initial_current_even_if_retained_value_matches(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        # This may be a retained value from before the car slept or the service
        # restarted. A new manual session must send once before trusting it.
        "tesla_charge_current_request": 23,
        "tesla_charge_current_request_updated_at": now.timestamp() - 5,
    })
    c = _charger(monkeypatch, tesla, state=state, charging_amps=0)

    c._control_charging(now=now)

    assert tesla.calls.count(("amps", 24, 24.0)) == 1
    assert tesla.calls.count("start") == 1
    assert state["ev_grid_charge_current_status"] == "confirmation_pending"


def test_manual_grid_current_retries_until_tesla_telemetry_confirms(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 5,
        "tesla_charge_current_request_updated_at": now.timestamp() - 5,
    })
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)

    assert c._control_charging(now=now) is True
    assert tesla.calls.count(("amps", 24, 24.0)) == 1
    assert tesla.calls.count("start") == 1

    c._control_charging(now=now + timedelta(seconds=59))
    assert tesla.calls.count(("amps", 24, 24.0)) == 1

    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=61))
    assert tesla.calls.count(("amps", 24, 24.0)) == 2

    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=122))
    assert tesla.calls.count(("amps", 24, 24.0)) == 3
    assert state["ev_grid_charge_current_status"] == "confirmation_pending"


def test_manual_grid_delivery_below_five_does_not_fight_maxem_after_ack(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 5,
        "tesla_charge_current_request_updated_at": now.timestamp() - 5,
    })
    c = _charger(monkeypatch, tesla, state=state, charging_amps=4)

    c._control_charging(now=now)
    assert tesla.calls.count(("amps", 24, 24.0)) == 1

    state["tesla_charge_current_request"] = 24
    state["tesla_charge_current_request_updated_at"] = (now + timedelta(seconds=5)).timestamp()
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=65))
    c._control_charging(now=now + timedelta(seconds=130))

    # Requested current was confirmed, so low ABB delivery is Maxem/ramp
    # observation only and never causes another Fleet current command.
    assert tesla.calls.count(("amps", 24, 24.0)) == 1
    assert state["ev_grid_charge_current_status"] == "delivery_limited"


def test_manual_grid_does_not_accept_an_unstamped_local_command_shadow(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.charging_amp_limit = 23
    tesla.charge_current_request_update_ts = 0
    state = FakeState({
        "ev_charge_requested": "True",
        "grid_charging_enabled": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    c = _charger(monkeypatch, tesla, state=state, charging_amps=0)

    c._control_charging(now=now)

    assert ("amps", 24, 24.0) in tesla.calls
    assert state["ev_grid_charge_current_status"] == "confirmation_pending"


def test_active_smart_block_ignores_ephemeral_maxem_ceiling_then_starts(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="20")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        # Tesla documents this as the currently available current. Maxem can
        # temporarily lower it, so it must not become our desired Fleet request.
        "tesla_charge_current_max": 5,
    })
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=16.0)

    assert c._control_charging() is True
    assert tesla.calls.count(("amps", 20, 20.0)) == 1
    assert tesla.calls.count("start") == 1
    assert c._charge_mode == "smart"
    assert c._smart_owns_charge is True
    assert state["ev_smart_charge_controller_status"] == "starting"


def test_partial_grid_smart_block_still_requests_full_installation_current(
        monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 236,
    })
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=2.0)

    assert c._control_charging(now=now) is True
    assert ("amps", 25, 25.0) in tesla.calls
    assert "start" in tesla.calls


def test_smart_session_sends_initial_current_even_if_retained_value_matches(
        monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "tesla_charge_current_request": 25,
        "tesla_charge_current_request_updated_at": now.timestamp() - 600,
    })
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=2.0)

    assert c._control_charging(now=now) is True
    assert tesla.calls.count(("amps", 25, 25.0)) == 1


def test_charge_limit_retries_within_one_minute_until_telemetry_confirms(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    limit_calls = []
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    tesla.set_tesla_charge_limit = lambda value: limit_calls.append(value) or (True, "ok")
    state = FakeState()
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)
    assert limit_calls == [80]
    assert not any(isinstance(call, tuple) and call[0] == "schedule"
                   for call in tesla.calls)

    # A newer mismatching event is useful evidence, but it must not collapse the
    # acknowledgement window into an immediate command-spending retry.
    state["tesla_soc_setpoint"] = 70
    state["tesla_soc_setpoint_updated_at"] = (
        now + timedelta(seconds=10)).timestamp()
    c._control_charging(now=now + timedelta(seconds=10))
    assert limit_calls == [80]

    c._control_charging(now=now + timedelta(seconds=59))
    assert limit_calls == [80]

    c._control_charging(now=now + timedelta(seconds=61))
    assert limit_calls == [80, 80]

    state["tesla_soc_setpoint"] = 80
    state["tesla_soc_setpoint_updated_at"] = (now + timedelta(seconds=70)).timestamp()
    c._control_charging(now=now + timedelta(seconds=75))

    assert limit_calls == [80, 80]
    assert c._smart_limit_pending is None
    assert c._smart_limit_signature == 80
    assert any(isinstance(call, tuple) and call[0] == "schedule"
               for call in tesla.calls)


def test_known_matching_charge_limit_ends_pending_command_without_retry(monkeypatch):
    """ChargeLimitSoc is idempotent state, not an edge acknowledgement.

    If the source of truth already reports the requested limit, an older source
    timestamp must not turn an already-satisfied setting into an endless paid
    command/wake loop.
    """
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    state = FakeState()
    limit_calls = []
    tesla.set_tesla_charge_limit = lambda value: (
        limit_calls.append(value) or (True, "ok"))
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)
    state["tesla_soc_setpoint"] = 80
    state["tesla_soc_setpoint_updated_at"] = now.timestamp() - 1
    c._control_charging(now=now + timedelta(seconds=30))

    assert c._smart_limit_pending is None
    assert c._smart_limit_signature == 80
    assert limit_calls == [80]

    c._control_charging(now=now + timedelta(seconds=61))
    assert limit_calls == [80]


def test_known_matching_charge_limit_never_sends_initial_command(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({"tesla_soc_setpoint": 80})
    tesla = FakeTesla(is_charging=False)
    calls = []
    tesla.set_tesla_charge_limit = lambda value: (
        calls.append(value) or (True, "ok"))
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)

    assert calls == []
    assert c._smart_limit_pending is None
    assert c._smart_limit_signature == 80


def test_already_set_charge_limit_response_ends_command_lifecycle(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    calls = []
    tesla.set_tesla_charge_limit = lambda value: (
        calls.append(value) or (True, "already_set"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)
    c._control_charging(now=now + timedelta(seconds=61))

    assert calls == [80]
    assert c._smart_limit_pending is None
    assert c._smart_limit_signature == 80


def test_charge_limit_ack_window_starts_after_blocking_delivery_returns(
        monkeypatch):
    """Wake/settle latency must not consume the post-command acknowledgement window."""
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    calls = []
    monotonic = {"now": 100.0}
    monkeypatch.setattr(
        ecc.time, "monotonic", lambda: monotonic["now"])

    def delayed_delivery(value):
        calls.append(value)
        monotonic["now"] += 32.0
        return True, "ok"

    tesla.set_tesla_charge_limit = delayed_delivery
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)
    assert c._smart_limit_pending["sent_at"] == pytest.approx(
        now.timestamp() + 32.0)

    # Sixty-three seconds after the lifecycle began is only 31 seconds after
    # Tesla's final response; spending another command here caused the live bug.
    c._control_charging(now=now + timedelta(seconds=63))
    assert calls == [80]

    c._control_charging(now=now + timedelta(seconds=93))
    assert calls == [80, 80]


def test_main_defers_remote_work_until_telemetry_bridge_is_hydrated(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({"tesla_telemetry_bridge_status": "AWAITING_SOURCE"})
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0,
        charging_amps=0, sun=False)
    c._smart_plan = _smart_plan(now, active=False)
    monkeypatch.setattr(c, "_refresh_smart_plan", lambda: None)
    scheduled = []
    monkeypatch.setattr(c, "_reschedule", scheduled.append)

    c.main()

    assert tesla.calls == []
    assert scheduled == [5.0]
    assert c._last_status_state == "waiting"


def test_main_allows_retained_replay_to_settle_after_bridge_sync(monkeypatch):
    _smart_settings(monkeypatch)
    now_ts = time.time()
    state = FakeState({
        "tesla_telemetry_bridge_status": "SYNCHRONIZED",
        "tesla_telemetry_bridge_updated_at": now_ts,
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0,
        charging_amps=0, sun=False)
    c._smart_plan = _smart_plan(datetime.now(timezone.utc), active=False)
    monkeypatch.setattr(c, "_refresh_smart_plan", lambda: None)
    monkeypatch.setattr(ecc.time, "time", lambda: now_ts + 0.5)
    scheduled = []
    monkeypatch.setattr(c, "_reschedule", scheduled.append)

    c.main()

    assert tesla.calls == []
    assert scheduled == [5.0]


def test_far_deadline_reconciles_charge_limit_before_deferring_owned_schedule(
        monkeypatch):
    """A calendar-unrepresentable fallback must not postpone the job's SoC limit."""
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    limit_calls = []
    tesla.set_tesla_charge_limit = lambda value: (
        limit_calls.append(value) or (True, "ok"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    deadline = now + timedelta(days=20)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = (deadline - timedelta(hours=7)).isoformat()
    c._smart_plan = plan

    c._control_charging(now=now)

    assert limit_calls == [80]
    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert not any(isinstance(call, tuple) and call[0] == "schedule"
                   for call in tesla.calls)


def test_far_deadline_stops_limit_commands_after_fresh_telemetry_confirmation(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    state = FakeState()
    limit_calls = []
    tesla.set_tesla_charge_limit = lambda value: (
        limit_calls.append(value) or (True, "ok"))
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    deadline = now + timedelta(days=20)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = (deadline - timedelta(hours=7)).isoformat()
    c._smart_plan = plan

    c._control_charging(now=now)
    state["tesla_soc_setpoint"] = 80
    state["tesla_soc_setpoint_updated_at"] = (
        now + timedelta(seconds=20)).timestamp()
    c._control_charging(now=now + timedelta(seconds=30))
    c._control_charging(now=now + timedelta(seconds=95))

    assert limit_calls == [80]
    assert c._smart_limit_pending is None
    assert c._smart_limit_signature == 80


def test_charge_limit_target_edit_replaces_pending_target_immediately(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    limit_calls = []
    tesla.set_tesla_charge_limit = lambda value: (
        limit_calls.append(value) or (True, "ok"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)
    edited = _smart_plan(now + timedelta(seconds=10), active=False)
    edited["target_soc"] = 85
    edited["job"]["target_soc"] = 85
    c._smart_plan = edited
    c._control_charging(now=now + timedelta(seconds=10))

    assert limit_calls == [80, 85]
    assert c._smart_limit_pending["target"] == 85
    assert c._smart_limit_pending["attempts"] == 1


def test_durable_gui_job_reconciles_limit_without_plan_or_chargeable_vehicle(
        monkeypatch):
    """The GUI's durable job is sufficient; planning/home/plug state must not gate the limit."""
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_home=False, is_plugged=False, is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    state = FakeState({
        "tesla_soc_setpoint": 70,
        "tesla_soc_setpoint_updated_at": now.timestamp() - 10,
    })
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0,
        charging_amps=0, sun=False)
    c._smart_plan = None
    c._smart_job_loaded = True
    c._smart_job = {
        "id": "gui-job",
        "status": "active",
        "target_soc": 85,
        "ready_by": (now + timedelta(days=9)).isoformat(),
    }

    assert c._local_engagement_signal() is True
    assert c._control_charging(now=now) is False

    assert ("limit", 85) in tesla.calls
    assert c._smart_limit_pending["target"] == 85

    state["tesla_soc_setpoint"] = 85
    state["tesla_soc_setpoint_updated_at"] = (
        now + timedelta(seconds=10)).timestamp()
    c._control_charging(now=now + timedelta(seconds=20))

    assert c._smart_limit_pending is None
    assert c._smart_limit_signature == 85
    assert c._local_engagement_signal() is False


def test_unconfirmed_charge_limit_keeps_retrying_until_telemetry_confirms(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    limit_calls = []
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    tesla.set_tesla_charge_limit = lambda value: limit_calls.append(value) or (True, "ok")
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    for seconds in (0, 61, 122, 183):
        c._control_charging(now=now + timedelta(seconds=seconds))

    assert limit_calls == [80, 80, 80, 80]
    assert c.global_state["ev_smart_charge_fallback_status"] == "limit_pending"

    c.global_state["tesla_soc_setpoint"] = 80
    c.global_state["tesla_soc_setpoint_updated_at"] = (
        now + timedelta(seconds=190)).timestamp()
    c._control_charging(now=now + timedelta(seconds=195))
    c._control_charging(now=now + timedelta(seconds=260))

    assert limit_calls == [80, 80, 80, 80]
    assert c._smart_limit_pending is None
    assert c._smart_limit_signature == 80


def test_rejected_charge_limit_retries_in_one_minute_and_never_installs_schedule(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    limit_calls = []
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    tesla.set_tesla_charge_limit = lambda value: (
        limit_calls.append(value) or (False, "network"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    for seconds in (0, 59, 61, 122, 183):
        c._control_charging(now=now + timedelta(seconds=seconds))

    assert limit_calls == [80, 80, 80, 80]
    assert not any(isinstance(call, tuple) and call[0] == "schedule"
                   for call in tesla.calls)
    assert c._smart_limit_pending["attempts"] == 4
    assert c.global_state["ev_smart_charge_fallback_status"] == "limit_network"


def test_schedule_backoff_does_not_delay_charge_limit_acknowledgement_retry(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    limit_calls = []
    schedule_calls = []
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    tesla.set_tesla_charge_limit = lambda value: (
        limit_calls.append(value) or (True, "ok"))
    tesla.upsert_owned_charge_schedule = lambda *args, **kwargs: (
        schedule_calls.append((args, kwargs)) or (False, "network"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    state = c.global_state
    c._control_charging(now=now)
    state["tesla_soc_setpoint"] = 80
    state["tesla_soc_setpoint_updated_at"] = (
        now + timedelta(seconds=10)).timestamp()
    c._control_charging(now=now + timedelta(seconds=20))
    c._control_charging(now=now + timedelta(seconds=81))

    assert limit_calls == [80]
    assert len(schedule_calls) == 2


def test_active_block_does_not_start_when_charge_limit_command_is_rejected(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 95
    tesla.charge_limit_update_ts = now.timestamp() - 10
    tesla.set_tesla_charge_limit = lambda value: (
        tesla.calls.append(("limit_rejected", value)) or (False, "auth"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)

    assert c._control_charging(now=now) is False
    assert ("limit_rejected", 80) in tesla.calls
    assert "start" not in tesla.calls
    assert not any(isinstance(call, tuple) and call[0] == "amps"
                   for call in tesla.calls)
    assert c.global_state["ev_smart_charge_controller_reason"] == "limit_auth"


def test_current_command_retries_from_requested_current_not_maxem_delivery(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=11.04)

    c._control_charging(now=now)
    assert tesla.calls.count(("amps", 24, 24.0)) == 1

    # A newer Maxem-reduced request does not trigger an immediate command fight.
    c.charging_amps = 5
    state["tesla_charge_current_request"] = 5
    state["tesla_charge_current_request_updated_at"] = (
        now + timedelta(seconds=10)).timestamp()
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=10))
    assert tesla.calls.count(("amps", 24, 24.0)) == 1

    # ABB delivery remains only 5 A because Maxem is throttling, but the lack of a pushed
    # requested-current acknowledgement—not delivered current—permits a guarded retry after 60 s.
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=61))
    assert tesla.calls.count(("amps", 24, 24.0)) == 2

    state["tesla_charge_current_request"] = 24
    state["tesla_charge_current_request_updated_at"] = (
        now + timedelta(seconds=70)).timestamp()
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=75))

    assert tesla.calls.count(("amps", 24, 24.0)) == 2
    assert c._smart_current_pending is None


def test_smart_current_and_start_confirmation_are_auditable(monkeypatch, caplog):
    caplog.set_level("INFO")
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc)
    state = FakeState()
    tesla = FakeTesla(is_charging=False)
    tesla.charge_state_update_ts = now.timestamp() - 60
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=2.0)

    c._control_charging(now=now)
    state.update({
        "tesla_charge_current_request": 25,
        "tesla_charge_current_request_updated_at": (
            now + timedelta(seconds=5)).timestamp(),
        "tesla_is_charging": "True",
        "tesla_charge_state_updated_at": (
            now + timedelta(seconds=6)).timestamp(),
    })
    c.charging_amps = 8
    c._control_charging(now=now + timedelta(seconds=10))

    assert (
        "EvCharger [Tesla API]: confirmed set_charging_amps - "
        "ChargeCurrentRequest=25 A/phase after attempt 1."
        in caplog.text
    )
    assert (
        "EvCharger [Tesla API]: confirmed charge_start - charging observed "
        "after attempt 1."
        in caplog.text
    )


def test_accepted_full_rate_command_releases_to_maxem_when_delivery_exceeds_floor(
        monkeypatch, caplog):
    caplog.set_level("INFO")
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc)
    state = FakeState({
        # Maxem may immediately replace Tesla's requested-current value, so the
        # exact 25 A command acknowledgement need not survive on the stream.
        "tesla_charge_current_request": 6,
        "tesla_charge_current_request_updated_at": (
            now + timedelta(seconds=5)).timestamp(),
    })
    c = _charger(
        monkeypatch,
        FakeTesla(is_charging=True),
        state=state,
        surplus_amps=0,
        charging_amps=8,
    )
    c._smart_current_pending = {
        "target": 25,
        "sent_at": now.timestamp(),
        "attempts": 1,
        "accepted": True,
    }

    status, should_command = c._smart_current_ack(
        25, (now + timedelta(seconds=10)).timestamp())

    assert status == "confirmed"
    assert should_command is False
    assert c._smart_current_pending is None
    assert c._last_commanded_amps == 25
    assert "delivery rose above 5 A; releasing current control to Maxem" in caplog.text


def test_rejected_full_rate_command_is_not_confirmed_by_existing_delivery(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc)
    c = _charger(
        monkeypatch,
        FakeTesla(is_charging=True),
        state=FakeState(),
        surplus_amps=0,
        charging_amps=8,
    )
    c._smart_current_pending = {
        "target": 25,
        "sent_at": now.timestamp(),
        "attempts": 1,
        "accepted": False,
    }

    status, should_command = c._smart_current_ack(
        25, (now + timedelta(seconds=10)).timestamp())

    assert status == "pending"
    assert should_command is False
    assert c._smart_current_pending is not None


def test_fresh_delivery_after_blocked_current_command_closes_stale_retry(
        monkeypatch, caplog):
    caplog.set_level("INFO")
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_power": 5500,
        "tesla_power_updated_at": (
            now + timedelta(seconds=5)).timestamp(),
    })
    c = _charger(
        monkeypatch,
        FakeTesla(is_charging=True),
        state=state,
        surplus_amps=0,
        charging_amps=8,
    )
    c._smart_current_pending = {
        "target": 25,
        "sent_at": now.timestamp(),
        "attempts": 1,
        "accepted": False,
    }

    status, should_command = c._smart_current_ack(
        25, (now + timedelta(seconds=10)).timestamp())

    assert status == "confirmed"
    assert should_command is False
    assert c._smart_current_pending is None
    assert c._last_commanded_amps == 25
    assert "fresh ABB delivery rose above 5 A" in caplog.text


def test_rejected_current_command_retries_throughout_block_and_never_starts(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 8,
        "tesla_charge_current_request_updated_at": now.timestamp() - 10,
    })
    tesla = FakeTesla(is_charging=False)
    tesla.set_tesla_charge_amps = lambda amps, installation_ceiling=None: (
        tesla.calls.append(("amps", amps, installation_ceiling)) or False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=11.04)

    for seconds in (0, 30, 61, 122, 183):
        c._last_command_ts = 0
        c._control_charging(now=now + timedelta(seconds=seconds))

    assert tesla.calls.count(("amps", 24, 24.0)) == 4
    assert "start" not in tesla.calls
    assert c.global_state["ev_smart_charge_controller_status"] == "at_risk"
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "set_current_rejected_retrying")


def test_second_accepted_current_command_gets_full_acknowledgement_window(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=5)
    c._smart_plan = _smart_plan(now, target_kw=11.04)
    c._charge_mode = "smart"
    c._smart_owns_charge = True

    for seconds in (0, 61):
        c._last_command_ts = 0
        c._control_charging(now=now + timedelta(seconds=seconds))
    assert tesla.calls.count(("amps", 24, 24.0)) == 2

    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=90))
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "set_current_pending_at_risk")

    c._control_charging(now=now + timedelta(seconds=122))
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "set_current_retry_due_at_risk")


def test_accepted_start_waits_for_power_flow_before_bounded_retry(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    starts = []
    tesla.start_tesla_charge = lambda: starts.append(1) or "ok"
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)

    assert c._control_charging(now=now) is True
    assert len(starts) == 1

    assert c._control_charging(now=now + timedelta(seconds=30)) is True
    assert len(starts) == 1
    assert c._smart_owns_charge is True

    c._last_command_ts = 0
    assert c._control_charging(now=now + timedelta(seconds=61)) is True
    assert len(starts) == 2
    assert c._smart_owns_charge is True
    assert c.global_state["ev_smart_charge_controller_reason"] == "start_confirmation_retry"


def test_accepted_api_start_does_not_manufacture_physical_confirmation(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.charge_state_update_ts = now.timestamp() - 60
    state = FakeState()
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=2.0)

    c._control_charging(now=now)
    assert tesla.calls.count("start") == 1
    assert tesla.is_charging is True  # API wrapper's optimistic local shadow

    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=61))

    assert tesla.calls.count("start") == 2
    assert state["ev_smart_charge_controller_reason"] == "start_confirmation_retry"


def test_rejected_start_remains_at_risk_and_retries_throughout_block(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 24,
        "tesla_charge_current_request_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=False)
    starts = []
    tesla.start_tesla_charge = lambda: starts.append(1) or False
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=11.04)
    suppressed = []
    c._suppress_smart_block = lambda smart, at: suppressed.append(at)
    c._smart_block_is_suppressed = lambda smart: bool(suppressed)

    for seconds in (0, 30, 61, 122, 183, 244):
        c._last_command_ts = 0
        c._control_charging(now=now + timedelta(seconds=seconds))

    assert len(starts) == 5
    assert suppressed == []
    assert c._smart_start_pending["attempts"] == 5
    assert c.global_state["ev_smart_charge_controller_status"] == "at_risk"
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "start_not_confirmed_retrying"
    )


def test_owned_fallback_start_inside_its_window_is_adopted_and_set_to_full_rate(
        monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    observed = datetime.now(timezone.utc)
    now = observed.replace(
        minute=(observed.minute // 15) * 15, second=0, microsecond=0)
    state = FakeState({
        "tesla_charge_started_ts": now.timestamp() + 1,
        "tesla_charge_current_request": 6,
        "tesla_charge_current_request_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=6,
    )
    plan = _smart_plan(now, active=True, target_kw=2.0)
    plan["latest_safe_start"] = now.isoformat()
    plan["required_ac_kwh"] = 80.0
    plan["expected_delivery_kw"] = 16.0
    plan["completion_buffer_minutes"] = 0
    c._smart_plan = plan
    c._smart_schedule_signature = ("installed",)

    assert c._control_charging(now=now + timedelta(seconds=5)) is True
    assert c._smart_owns_charge is True
    assert c._charge_mode == "smart"
    assert ("amps", 25, 25.0) in tesla.calls
    assert state["ev_smart_charge_controller_reason"] != "external_charge_in_progress"


def test_owned_fallback_ownership_survives_controller_restart(
        monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(
        monkeypatch,
        EV_CHARGER_MAX_AMPS="25",
        EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path),
    )
    observed = datetime.now(timezone.utc)
    now = observed.replace(
        minute=(observed.minute // 15) * 15, second=0, microsecond=0)
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "sent": {},
        "suppressed_blocks": {},
        "owned_fallback": {
            "job_id": "job-1",
            "signature": ["job-1", now.date().isoformat(), 0, 0, 0, 52.1, 5.1],
        },
    }))
    state = FakeState({
        "tesla_charge_started_ts": now.timestamp() + 1,
        "tesla_charge_current_request": 6,
        "tesla_charge_current_request_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=6,
    )
    plan = _smart_plan(now, active=True, target_kw=2.0)
    plan["latest_safe_start"] = now.isoformat()
    plan["required_ac_kwh"] = 80.0
    plan["expected_delivery_kw"] = 16.0
    plan["completion_buffer_minutes"] = 0
    c._smart_plan = plan

    assert c._control_charging(now=now + timedelta(seconds=5)) is True
    assert c._smart_owns_charge is True
    assert ("amps", 25, 25.0) in tesla.calls


def test_partial_smart_block_uses_full_current_not_a_sub_five_request(monkeypatch):
    """Partial energy changes duration, not the initial grid-charge current."""
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="24")
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=2.8)

    c._control_charging()

    assert tesla.calls.count(("amps", 24, 24.0)) == 1
    assert "start" in tesla.calls


def test_distinct_smart_block_sends_and_verifies_a_fresh_current_command(monkeypatch):
    """A confirmed prior block must never make the next block skip its max-current command."""
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="25")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._last_commanded_amps = 25
    c._smart_current_pending = None

    # Observe one between-block tick; it resets command acknowledgement ownership.
    c._smart_plan = _smart_plan(now, active=False)
    c._control_charging(now=now)
    assert c._last_commanded_amps is None

    # The following distinct block issues both a new ceiling and a start.
    c._smart_plan = _smart_plan(now + timedelta(minutes=15), active=True)
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(minutes=15))

    assert ("amps", 25, 25.0) in tesla.calls
    assert "start" in tesla.calls


def test_smart_target_amps_rejects_implausible_phase_voltage_telemetry(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="24")
    state = FakeState({
        "tesla_charger_phases": 99,
        "tesla_charger_voltage": 12,
        "tesla_charge_current_max": 24,
    })
    c = _charger(monkeypatch, FakeTesla(), state=state)

    assert c._smart_target_amps({"requested_power_kw": 16.0}) == 24


def test_smart_target_amps_does_not_follow_dynamic_available_current(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="24")
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 5,
    })
    c = _charger(monkeypatch, FakeTesla(), state=state)

    assert c._smart_target_amps({"requested_power_kw": 16.0}) == 24
    assert c._smart_target_amps({"target_amps": 24}) == 24


def test_positive_partial_tail_uses_full_grid_current(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="24")
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    c = _charger(monkeypatch, FakeTesla(), state=state)

    assert c._smart_target_amps({"requested_power_kw": 0.1}) == 24


def test_smart_controller_does_not_chase_maxem_throttled_meter(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=5)
    c._smart_plan = _smart_plan(now, target_kw=11.04)  # 16 A at 3x230 V
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._last_commanded_amps = 24
    c._last_command_ts = 0

    assert c._control_charging() is True
    assert not any(isinstance(call, tuple) and call[0] == "amps" for call in tesla.calls)


def test_smart_controller_does_not_chase_maxem_available_current_oscillation(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 24,
        "tesla_charge_current_request_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=16)
    c._smart_plan = _smart_plan(now, target_kw=11.04)
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._last_commanded_amps = 24
    c._last_command_ts = 0

    assert c._control_charging(now=now) is True

    # Maxem temporarily reduces both the available and requested current.
    state["tesla_charge_current_max"] = 5
    state["tesla_charge_current_request"] = 5
    state["tesla_charge_current_request_updated_at"] = (
        now + timedelta(seconds=10)).timestamp()
    c.charging_amps = 5
    c._last_command_ts = 0
    assert c._control_charging(now=now + timedelta(seconds=10)) is True

    # It later releases the site constraint. Neither edge is a new command intent.
    state["tesla_charge_current_max"] = 24
    state["tesla_charge_current_request"] = 24
    state["tesla_charge_current_request_updated_at"] = (
        now + timedelta(seconds=20)).timestamp()
    c.charging_amps = 16
    c._last_command_ts = 0
    assert c._control_charging(now=now + timedelta(seconds=20)) is True

    assert not any(isinstance(call, tuple) and call[0] == "amps"
                   for call in tesla.calls)


def test_external_charge_outside_smart_block_is_stopped(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=12)
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging() is False
    assert tesla.calls == ["stop"]
    assert c.global_state["ev_smart_charge_controller_status"] == "waiting"
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "unauthorized_charge_stopped"
    )


def test_plug_triggered_charge_outside_smart_block_is_stopped(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_plugged_transition_ts": now.timestamp(),
        "tesla_charge_started_ts": now.timestamp() + 1,
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch,
        tesla,
        state=state,
        surplus_amps=0,
        charging_amps=23,
    )
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging(now=now + timedelta(seconds=5)) is False
    assert tesla.calls == ["stop"]
    assert state["ev_smart_charge_controller_status"] == "waiting"
    assert state["ev_smart_charge_controller_reason"] == "unauthorized_charge_stopped"


def test_plug_triggered_charge_inside_smart_block_is_adopted(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_plugged_transition_ts": now.timestamp(),
        "tesla_charge_started_ts": now.timestamp() + 1,
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 23,
        "tesla_charge_current_request_updated_at": now.timestamp() + 1,
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch,
        tesla,
        state=state,
        surplus_amps=0,
        charging_amps=23,
    )
    c._smart_plan = _smart_plan(now, active=True)

    assert c._control_charging(now=now + timedelta(seconds=5)) is True
    assert "stop" not in tesla.calls
    assert not any(
        isinstance(call, tuple) and call[0] == "schedule"
        for call in tesla.calls
    )
    assert c._smart_owns_charge is True
    assert c._charge_mode == "smart"


def test_later_tesla_app_start_while_still_plugged_is_stopped(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_plugged_transition_ts": (
            now - timedelta(minutes=1)
        ).timestamp(),
        "tesla_charge_started_ts": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch,
        tesla,
        state=state,
        surplus_amps=0,
        charging_amps=23,
    )
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging(now=now + timedelta(seconds=5)) is False
    assert tesla.calls == ["stop"]
    assert state["ev_smart_charge_controller_status"] == "waiting"
    assert state["ev_smart_charge_controller_reason"] == (
        "unauthorized_charge_stopped"
    )


def test_plug_triggered_charge_during_unavailable_solar_slot_is_stopped(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_plugged_transition_ts": now.timestamp(),
        "tesla_charge_started_ts": now.timestamp() + 1,
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch,
        tesla,
        state=state,
        ess_soc=95,
        surplus_amps=0,
        charging_amps=23,
        sun=True,
    )
    c._smart_plan = _smart_plan(now, active=True)
    c._smart_plan["slots"][0].update({
        "supply": "solar",
        "grid_energy_kwh": 0,
    })

    assert c._control_charging(now=now + timedelta(seconds=5)) is False
    assert tesla.calls == ["stop"]
    assert state["ev_smart_charge_controller_reason"] == "unauthorized_charge_stopped"


def test_tesla_app_stop_is_reversed_inside_authorized_smart_block(
        monkeypatch, tmp_path):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(tmp_path / "controller-state.json"),
    )
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=12)
    c._smart_plan = _smart_plan(now, active=True)

    assert c._control_charging(now=now) is True
    assert c._smart_owns_charge is True
    tesla.calls.clear()

    # A Tesla-app stop does not outrank the active controller block.
    tesla.is_charging = False
    c.charging_amps = 0
    c._last_command_ts = 0
    assert c._control_charging(now=now + timedelta(minutes=1)) is True
    assert "start" in tesla.calls


def test_owned_smart_charge_stops_after_block_transition(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=16)
    c._smart_plan = _smart_plan(now, active=False)
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._last_command_ts = 0

    assert c._control_charging() is False
    assert "stop" in tesla.calls
    assert c._smart_owns_charge is False


def test_stale_smart_plan_never_starts_charge(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(
        now, generated_at=now - timedelta(seconds=ecc.SMART_PLAN_MAX_AGE_S + 1))

    assert c._control_charging() is False
    assert tesla.calls == []
    assert c.global_state["ev_smart_charge_controller_status"] == "stale_plan"


def test_stale_smart_plan_stops_only_a_process_owned_charge(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=16)
    c._smart_plan = _smart_plan(
        now, generated_at=now - timedelta(seconds=ecc.SMART_PLAN_MAX_AGE_S + 1))
    c._charge_mode = "smart"
    c._smart_owns_charge = True

    assert c._control_charging() is False
    assert "stop" in tesla.calls


def test_future_dated_smart_plan_fails_closed(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, generated_at=now + timedelta(minutes=10))

    assert c._control_charging(now=now) is False
    assert tesla.calls == []
    assert c.global_state["ev_smart_charge_controller_status"] == "stale_plan"


def test_paused_job_removes_only_owned_schedule_once_and_suppresses_surplus(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=12, charging_amps=0)
    c._smart_plan = _smart_plan(now, status="paused", job_status="paused")

    c._control_charging(now=now)
    c._control_charging(now=now + timedelta(seconds=20))

    assert tesla.calls.count(("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID)) == 1
    assert tesla.calls.count(
        ("remove_schedule", ecc.SMART_LEGACY_OWNED_SCHEDULE_IDS[0])) == 1
    assert "start" not in tesla.calls
    assert not any(isinstance(call, tuple) and call[0] == "amps" for call in tesla.calls)


def test_legacy_owned_schedule_cleanup_is_durable_across_jobs_and_restart(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    legacy_id = ecc.SMART_LEGACY_OWNED_SCHEDULE_IDS[0]

    first_tesla = FakeTesla(is_charging=False)
    first = _charger(
        monkeypatch, first_tesla, surplus_amps=0, charging_amps=0)
    first._smart_plan = _smart_plan(
        now, status="paused", job_status="paused")

    first._control_charging(now=now)

    assert first_tesla.calls.count(("remove_schedule", legacy_id)) == 1

    second_tesla = FakeTesla(is_charging=False)
    second = _charger(
        monkeypatch, second_tesla, surplus_amps=0, charging_amps=0)
    second_plan = _smart_plan(
        now + timedelta(minutes=1), status="paused", job_status="paused")
    second_plan["job"]["id"] = "job-2"
    second._smart_plan = second_plan

    second._control_charging(now=now + timedelta(minutes=1))

    assert ("remove_schedule", legacy_id) not in second_tesla.calls
    assert (
        "remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID
    ) in second_tesla.calls


@pytest.mark.parametrize(
    ("plan_status", "reason"),
    [
        ("completed", "target_soc_reached"),
        ("expired", "ready_by_elapsed"),
    ],
)
def test_terminal_job_removes_tesla_schedule_before_deleting_artifacts(
        monkeypatch, plan_status, reason):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, status=plan_status)
    plan["active"] = False
    plan["reason"] = reason
    c._smart_plan = plan
    c._smart_job = {
        "id": "job-1",
        "status": "active",
        "target_soc": 80,
        "ready_by": plan["ready_by"],
    }
    c._smart_job_loaded = True
    c._smart_schedule_signature = ("installed",)
    cleared = []
    def clear_after_tesla(job_id, **kwargs):
        assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
        cleared.append((job_id, kwargs))
        return True
    monkeypatch.setattr(
        ecc, "clear_job_artifacts",
        clear_after_tesla,
    )

    assert c._control_charging(now=now) is False

    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert cleared == [("job-1", {
        "job_path": None,
        "plan_path": None,
    })]
    assert c._smart_plan is None
    assert c.global_state["ev_smart_charge_controller_status"] == "idle"


def test_run_now_replaces_existing_owned_schedule_before_install(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=True)
    plan["job"]["execution_mode"] = "run_now"
    plan["execution_mode"] = "run_now"
    c._smart_plan = plan
    c._smart_job = {
        **plan["job"],
        "execution_mode": "run_now",
    }
    c._smart_job_loaded = True
    c._smart_schedule_signature = ("old-window",)

    smart = c._smart_plan_context(now=now)
    assert c._reconcile_smart_fallback(smart) == "confirmed"

    remove_index = tesla.calls.index(
        ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID))
    schedule_index = next(
        index for index, call in enumerate(tesla.calls)
        if isinstance(call, tuple) and call[0] == "schedule")
    assert remove_index < schedule_index


def test_run_now_schedule_start_does_not_move_forward_every_controller_tick(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 26, 11, 34, 50, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=True)
    plan["job"]["execution_mode"] = "run_now"
    plan["job"]["run_now_requested_at"] = now.isoformat()
    plan["execution_mode"] = "run_now"
    plan["blocks"] = [{
        "start": (now - timedelta(seconds=1)).isoformat(),
        "end": (now + timedelta(minutes=34, seconds=42)).isoformat(),
    }]
    c._smart_plan = plan
    c._smart_job = dict(plan["job"])
    c._smart_job_loaded = True

    assert c._reconcile_smart_fallback(
        c._smart_plan_context(now=now)) == "confirmed"
    assert c._reconcile_smart_fallback(
        c._smart_plan_context(now=now + timedelta(minutes=1))) == "confirmed"
    assert c._reconcile_smart_fallback(
        c._smart_plan_context(now=now + timedelta(minutes=2))) == "confirmed"

    schedules = [
        call for call in tesla.calls
        if isinstance(call, tuple) and call[0] == "schedule"
    ]
    assert len(schedules) == 1
    assert schedules[0][2]["start_time"] == 13 * 60 + 35
    assert schedules[0][2]["end_time"] == 18 * 60 + 35


def test_run_now_schedule_is_not_rewritten_when_optimizer_moves_live_block(
        monkeypatch):
    """A replan may move its estimate, but must not re-trigger Tesla's schedule."""
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 26, 11, 34, 50, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=23)
    plan = _smart_plan(now, active=True)
    plan["job"]["execution_mode"] = "run_now"
    plan["job"]["run_now_requested_at"] = now.isoformat()
    plan["execution_mode"] = "run_now"
    plan["blocks"] = [{
        "start": (now - timedelta(seconds=1)).isoformat(),
        "end": (now + timedelta(minutes=34, seconds=42)).isoformat(),
    }]
    c._smart_plan = plan
    c._smart_job = dict(plan["job"])
    c._smart_job_loaded = True

    assert c._reconcile_smart_fallback(
        c._smart_plan_context(now=now)) == "confirmed"

    replan_now = now + timedelta(minutes=15)
    plan["generated_at"] = replan_now.isoformat()
    plan["blocks"] = [{
        "start": (replan_now - timedelta(seconds=1)).isoformat(),
        "end": (replan_now + timedelta(minutes=37, seconds=42)).isoformat(),
    }]
    c._smart_plan = plan

    assert c._reconcile_smart_fallback(
        c._smart_plan_context(now=replan_now)) == "confirmed"

    schedules = [
        call for call in tesla.calls
        if isinstance(call, tuple) and call[0] == "schedule"
    ]
    assert len(schedules) == 1
    assert schedules[0][2]["start_time"] == 13 * 60 + 35
    assert schedules[0][2]["end_time"] == 18 * 60 + 35


def test_fresh_tesla_complete_state_finishes_run_now_at_matching_limit(
        monkeypatch):
    _smart_settings(monkeypatch)
    requested_at = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    now = requested_at + timedelta(minutes=20)
    state = FakeState({
        "tesla_detailed_charge_state": "complete",
        "tesla_charge_state_updated_at": now.timestamp(),
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc = 79.0
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(requested_at, active=True)
    plan["generated_at"] = now.isoformat()
    plan["job"]["execution_mode"] = "run_now"
    plan["job"]["run_now_requested_at"] = requested_at.isoformat()
    plan["execution_mode"] = "run_now"
    c._smart_plan = plan
    c._smart_job = dict(plan["job"])
    c._smart_job_loaded = True

    context = c._smart_plan_context(now=now)

    assert context["terminal"] is True
    assert context["terminal_status"] == "completed"


@pytest.mark.parametrize(
    ("state_updated_at", "observed_limit"),
    [
        ("before_request", 80),
        ("after_request", 70),
    ],
)
def test_tesla_complete_state_cannot_finish_wrong_or_stale_run_now(
        monkeypatch, state_updated_at, observed_limit):
    _smart_settings(monkeypatch)
    requested_at = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    now = requested_at + timedelta(minutes=5)
    completed_at = (
        requested_at - timedelta(seconds=1)
        if state_updated_at == "before_request" else now
    )
    state = FakeState({
        "tesla_detailed_charge_state": "complete",
        "tesla_charge_state_updated_at": completed_at.timestamp(),
        "tesla_soc_setpoint": observed_limit,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc = 79.0
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(requested_at, active=True)
    plan["generated_at"] = now.isoformat()
    plan["job"]["execution_mode"] = "run_now"
    plan["job"]["run_now_requested_at"] = requested_at.isoformat()
    plan["execution_mode"] = "run_now"
    c._smart_plan = plan
    c._smart_job = dict(plan["job"])
    c._smart_job_loaded = True

    context = c._smart_plan_context(now=now)

    assert context["terminal"] is False
    assert context["terminal_status"] is None


def test_run_now_owned_window_remains_active_across_local_midnight(monkeypatch):
    _smart_settings(monkeypatch)
    c = _charger(
        monkeypatch, FakeTesla(is_charging=True),
        surplus_amps=0, charging_amps=8)
    local_now = ecc.EvCharger.tz.localize(
        datetime(2026, 7, 27, 0, 10))
    local_end = ecc.EvCharger.tz.localize(
        datetime(2026, 7, 27, 0, 30))
    signature = (
        "job-1", "2026-07-26", 23 * 60 + 45, 30, 1, 52.1, 5.1)

    assert c._owned_schedule_covers_active_window(
        signature,
        job_id="job-1",
        local_now=local_now,
        local_end=local_end,
        latitude=52.1,
        longitude=5.1,
    ) is True


def test_single_contiguous_plan_installs_the_same_window_shown_in_ui(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    plan["blocks"] = [{
        "start": "2026-07-26T15:15:00+02:00",
        "end": "2026-07-26T15:54:32.575000+02:00",
    }]
    c._smart_plan = plan
    c._smart_job = dict(plan["job"])
    c._smart_job_loaded = True

    assert c._reconcile_smart_fallback(
        c._smart_plan_context(now=now)) == "confirmed"

    schedule = next(
        call for call in tesla.calls
        if isinstance(call, tuple) and call[0] == "schedule"
    )
    assert schedule[2]["start_time"] == 15 * 60 + 15
    # Tesla schedules have minute precision. Round upward so the visible
    # 15:54:32 energy obligation is not truncated by 32 seconds.
    assert schedule[2]["end_time"] == 15 * 60 + 55


def test_cold_restart_restores_matching_owned_schedule_without_resending(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_soc_setpoint": 80,
        "tesla_soc_setpoint_updated_at": now.timestamp(),
    })
    plan = _smart_plan(now, active=False)
    plan["blocks"] = [{
        "start": "2026-07-26T15:15:00+02:00",
        "end": "2026-07-26T15:55:00+02:00",
    }]

    first_tesla = FakeTesla(is_charging=False)
    first = _charger(
        monkeypatch, first_tesla, state=state, surplus_amps=0, charging_amps=0)
    first._smart_plan = plan
    first._smart_job = dict(plan["job"])
    first._smart_job_loaded = True
    assert first._reconcile_smart_fallback(
        first._smart_plan_context(now=now)) == "confirmed"

    restarted_tesla = FakeTesla(is_charging=False)
    restarted = _charger(
        monkeypatch,
        restarted_tesla,
        state=state,
        surplus_amps=0,
        charging_amps=0,
    )
    restarted._smart_plan = plan
    restarted._smart_job = dict(plan["job"])
    restarted._smart_job_loaded = True
    assert restarted._reconcile_smart_fallback(
        restarted._smart_plan_context(
            now=now + timedelta(minutes=1))) == "confirmed"

    assert not any(
        isinstance(call, tuple) and call[0] == "schedule"
        for call in restarted_tesla.calls
    )


def test_run_now_terminal_cleanup_disables_grid_assist_and_confirms_safe_current(
        monkeypatch):
    _smart_settings(monkeypatch)
    setpoints = []
    victron = importlib.import_module("lib.victron_integration")
    monkeypatch.setattr(
        victron,
        "ac_power_setpoint",
        lambda **kwargs: setpoints.append(kwargs),
    )
    now = datetime.now(timezone.utc)
    state = FakeState({
        "grid_charging_enabled": True,
        "ai_grid_assist": "on",
        "tesla_charge_current_request": 24,
        "tesla_charge_current_request_updated_at": now.timestamp(),
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(
        monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, status="completed")
    plan["active"] = False
    plan["job"]["execution_mode"] = "run_now"
    plan["execution_mode"] = "run_now"
    c._smart_plan = plan
    c._smart_job = {**plan["job"], "execution_mode": "run_now"}
    c._smart_job_loaded = True
    c._smart_schedule_signature = ("installed",)
    cleared = []
    monkeypatch.setattr(
        ecc, "clear_job_artifacts",
        lambda *args, **kwargs: cleared.append((args, kwargs)) or True,
    )

    assert c._control_charging(now=now) is False
    assert state["grid_charging_enabled"] is False
    assert state["ai_grid_assist"] == "off"
    assert setpoints == [{
        "watts": "0.0",
        "override_ess_net_mettering": False,
        "silent": False,
    }]
    assert ("amps", 5, 24.0) in tesla.calls
    assert cleared == []

    state["tesla_charge_current_request"] = 5
    state["tesla_charge_current_request_updated_at"] = (
        now + timedelta(seconds=1)).timestamp()
    assert c._control_charging(now=now + timedelta(seconds=2)) is False
    assert cleared
    assert c._smart_plan is None


def test_run_now_cleanup_retains_job_and_retries_if_grid_release_fails(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "grid_charging_enabled": True,
        "ai_grid_assist": "on",
    })
    c = _charger(
        monkeypatch, FakeTesla(is_charging=False),
        state=state, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, status="completed")
    plan["active"] = False
    plan["job"]["execution_mode"] = "run_now"
    plan["job"]["run_now_requested_at"] = now.isoformat()
    plan["execution_mode"] = "run_now"
    c._smart_plan = plan
    c._smart_job = dict(plan["job"])
    c._smart_job_loaded = True
    cleared = []
    monkeypatch.setattr(
        ecc, "clear_job_artifacts",
        lambda *args, **kwargs: cleared.append(True) or True,
    )
    attempts = []

    def fail_then_succeed(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError("Victron broker unavailable")

    victron = importlib.import_module("lib.victron_integration")
    monkeypatch.setattr(
        victron, "ac_power_setpoint", fail_then_succeed)

    assert c._control_charging(now=now) is False
    assert state["grid_charging_enabled"] is True
    assert cleared == []

    state["tesla_charge_current_request"] = 5
    state["tesla_charge_current_request_updated_at"] = (
        now + timedelta(seconds=1)).timestamp()
    assert c._control_charging(now=now + timedelta(seconds=2)) is False
    assert len(attempts) == 2
    assert state["grid_charging_enabled"] is False


def test_run_now_cleanup_preserves_grid_assist_that_user_already_owned(
        monkeypatch):
    _smart_settings(monkeypatch)
    setpoints = []
    victron = importlib.import_module("lib.victron_integration")
    monkeypatch.setattr(
        victron,
        "ac_power_setpoint",
        lambda **kwargs: setpoints.append(kwargs),
    )
    now = datetime.now(timezone.utc)
    state = FakeState({
        "grid_charging_enabled": True,
        "ai_grid_assist": "on",
        "tesla_charge_current_request": 5,
        "tesla_charge_current_request_updated_at": now.timestamp(),
    })
    c = _charger(
        monkeypatch, FakeTesla(is_charging=False),
        state=state, surplus_amps=0, charging_amps=0,
    )
    plan = _smart_plan(now, status="completed")
    plan["active"] = False
    plan["job"]["execution_mode"] = "run_now"
    plan["job"]["run_now_grid_assist_owned"] = False
    plan["execution_mode"] = "run_now"
    c._smart_plan = plan
    c._smart_job = dict(plan["job"])
    c._smart_job_loaded = True
    c._smart_schedule_signature = ("installed",)
    monkeypatch.setattr(
        ecc, "clear_job_artifacts", lambda *args, **kwargs: True)

    assert c._control_charging(now=now) is False
    assert state["grid_charging_enabled"] is True
    assert state["ai_grid_assist"] == "on"
    assert setpoints == []


def test_run_now_becomes_terminal_at_charge_window_end_not_buffered_deadline(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    plan["job"]["execution_mode"] = "run_now"
    plan["execution_mode"] = "run_now"
    plan["slots"][0]["start"] = (now - timedelta(minutes=30)).isoformat()
    plan["slots"][0]["end"] = (now - timedelta(seconds=1)).isoformat()
    plan["ready_by"] = (now + timedelta(minutes=30)).isoformat()
    plan["job"]["ready_by"] = plan["ready_by"]
    c._smart_plan = plan
    c._smart_job = {**plan["job"], "execution_mode": "run_now"}
    c._smart_job_loaded = True

    assert c._smart_plan_context(now=now)["terminal_status"] == "expired"


def test_terminal_job_retains_artifacts_when_tesla_schedule_removal_fails(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.remove_owned_charge_schedule = lambda schedule_id: (
        tesla.calls.append(("remove_schedule", schedule_id)) or (False, "network"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, status="completed")
    plan["active"] = False
    c._smart_plan = plan
    c._smart_job = {
        "id": "job-1",
        "status": "active",
        "target_soc": 80,
        "ready_by": plan["ready_by"],
    }
    c._smart_job_loaded = True
    c._smart_schedule_signature = ("installed",)
    cleared = []
    monkeypatch.setattr(
        ecc, "clear_job_artifacts",
        lambda *args, **kwargs: cleared.append((args, kwargs)) or True,
    )

    assert c._control_charging(now=now) is False
    assert cleared == []
    assert c._smart_plan is plan
    assert c.global_state["ev_smart_charge_fallback_status"] == "remove_network"


def test_ready_by_expiry_makes_a_previously_planned_snapshot_terminal(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now)
    plan["ready_by"] = (now - timedelta(seconds=1)).isoformat()
    plan["job"]["ready_by"] = plan["ready_by"]
    c._smart_plan = plan
    c._smart_job = {
        "id": "job-1",
        "status": "active",
        "target_soc": 80,
        "ready_by": plan["ready_by"],
    }
    c._smart_job_loaded = True
    monkeypatch.setattr(ecc, "clear_job_artifacts", lambda *args, **kwargs: True)

    assert c._control_charging(now=now) is False
    assert c.global_state["ev_smart_charge_controller_status"] == "idle"
    assert c.global_state["ev_smart_charge_controller_reason"] == "expired_cleaned_up"


def test_live_target_soc_completes_a_still_planned_snapshot(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc = 80
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now)
    c._smart_plan = plan
    c._smart_job = {
        "id": "job-1",
        "status": "active",
        "target_soc": 80,
        "ready_by": plan["ready_by"],
    }
    c._smart_job_loaded = True
    monkeypatch.setattr(ecc, "clear_job_artifacts", lambda *args, **kwargs: True)

    assert c._control_charging(now=now) is False
    assert c.global_state["ev_smart_charge_controller_status"] == "idle"
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "completed_cleaned_up"
    )


def test_terminal_cleanup_removes_schedule_even_after_apply_and_telemetry_disabled(
        monkeypatch):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_APPLY="False",
        TESLA_TELEMETRY_ENABLED="False",
    )
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, status="completed")
    plan["active"] = False
    c._smart_plan = plan
    c._smart_job = {
        "id": "job-1",
        "status": "active",
        "target_soc": 80,
        "ready_by": plan["ready_by"],
    }
    c._smart_job_loaded = True
    monkeypatch.setattr(ecc, "clear_job_artifacts", lambda *args, **kwargs: True)

    assert c._local_engagement_signal() is True
    assert c._control_charging(now=now) is False
    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert c.global_state["ev_smart_charge_controller_status"] == "idle"


@pytest.mark.parametrize("failure_category", ["network", "failed", "budget"])
def test_failed_schedule_removal_backs_off_instead_of_hot_looping(
        monkeypatch, failure_category):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)

    def reject_remove(schedule_id):
        tesla.calls.append(("remove_schedule", schedule_id))
        return False, failure_category

    tesla.remove_owned_charge_schedule = reject_remove
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, status="paused", job_status="paused")
    c._smart_schedule_signature = ("installed",)

    c._control_charging(now=now)
    c._control_charging(now=now + timedelta(seconds=30))

    cleanup_id = ecc.SMART_LEGACY_OWNED_SCHEDULE_IDS[0]
    assert tesla.calls.count(("remove_schedule", cleanup_id)) == 1
    assert c.global_state["ev_smart_charge_fallback_status"] == "retry_backoff"

    c._control_charging(
        now=now + timedelta(seconds=ecc.SMART_SCHEDULE_RETRY_S + 1))
    assert tesla.calls.count(("remove_schedule", cleanup_id)) == 2


def test_failed_schedule_install_is_bounded_after_three_one_minute_attempts(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.upsert_owned_charge_schedule = lambda *args, **kwargs: (
        tesla.calls.append(("schedule_failed", args, kwargs)) or (False, "network"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    for seconds in (0, 30, 61, 122, 183, 244):
        c._control_charging(now=now + timedelta(seconds=seconds))

    assert len([call for call in tesla.calls if call[0] == "schedule_failed"]) == 3
    assert c.global_state["ev_smart_charge_fallback_status"] == "schedule_unconfirmed"


def test_failed_schedule_removal_is_bounded_after_three_one_minute_attempts(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.remove_owned_charge_schedule = lambda schedule_id: (
        tesla.calls.append(("remove_schedule", schedule_id)) or (False, "network"))
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, status="paused", job_status="paused")
    c._smart_schedule_signature = ("installed",)

    for seconds in (0, 30, 61, 122, 183, 244):
        c._control_charging(now=now + timedelta(seconds=seconds))

    cleanup_id = ecc.SMART_LEGACY_OWNED_SCHEDULE_IDS[0]
    assert tesla.calls.count(("remove_schedule", cleanup_id)) == 3
    assert c.global_state["ev_smart_charge_fallback_status"] == "remove_unconfirmed"


def test_fallback_schedule_reconciles_once_with_sunday_bit_one(monkeypatch):
    _smart_settings(monkeypatch)
    # 2026-07-19 is a Sunday.
    now = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.vehicle_soc_setpoint = 70
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)
    c._control_charging(now=now + timedelta(seconds=20))

    schedules = [call for call in tesla.calls
                 if isinstance(call, tuple) and call[0] == "schedule"]
    assert len(schedules) == 1
    assert schedules[0][2]["days_of_week"] == 1
    assert schedules[0][2]["latitude"] == 52.1
    assert schedules[0][2]["longitude"] == 5.1
    assert tesla.calls.count(("limit", 80)) == 1


def test_unsupported_fallback_schedule_degrades_to_live_control(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.upsert_owned_charge_schedule = lambda *a, **k: (
        tesla.calls.append(("schedule_unsupported", a, k)), (False, "unsupported"))[1]
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    c._control_charging(now=now)
    c._control_charging(now=now + timedelta(minutes=1))

    assert len([x for x in tesla.calls if isinstance(x, tuple)
                and x[0] == "schedule_unsupported"]) == 1
    assert c.global_state["ev_smart_charge_fallback_status"] == "unsupported"


def test_past_fallback_start_during_active_slot_moves_to_next_minute(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 19, 20, 0, 30, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=True)
    plan["latest_safe_start"] = (now - timedelta(hours=2)).isoformat()
    c._smart_plan = plan

    c._control_charging(now=now)

    schedule = next(call for call in tesla.calls
                    if isinstance(call, tuple) and call[0] == "schedule")
    assert schedule[2]["start_time"] == 22 * 60 + 1
    assert c.global_state["ev_smart_charge_fallback_status"] == "confirmed"


def test_past_fallback_start_uses_earliest_future_selected_slot(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    plan["latest_safe_start"] = (now - timedelta(hours=2)).isoformat()
    c._smart_plan = plan

    c._control_charging(now=now)

    schedule = next(call for call in tesla.calls
                    if isinstance(call, tuple) and call[0] == "schedule")
    assert schedule[2]["start_time"] == 22 * 60 + 15


def test_fallback_uses_local_continuous_window_not_sparse_plan_or_utc_hours(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    # The durable UI deadline is UTC (07:00 Europe/Amsterdam), while daily pacing's
    # capacity-safe timestamp is already local and much earlier than a Tesla fallback needs.
    deadline = datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = "2026-07-28T05:00:00+02:00"
    plan["required_ac_kwh"] = 65.1
    plan["expected_delivery_kw"] = 11.04
    plan["completion_buffer_minutes"] = 30
    c._smart_plan = plan

    c._control_charging(now=now)

    # The exact fallback is Wed 00:30–07:00, but it is over seven days away and Tesla
    # schedules have no calendar date. Remove any owned fallback and wait rather than
    # approximating it with a window that Tesla may consider active immediately.
    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert not any(isinstance(call, tuple) and call[0] == "schedule"
                   for call in tesla.calls)
    assert c.global_state["ev_smart_charge_fallback_status"] == (
        "fallback_waiting_for_representable_date_removed")


def test_fallback_tightens_to_exact_local_window_once_date_is_representable(
        monkeypatch):
    _smart_settings(monkeypatch)
    # Wed Jul 29 00:30 is now the next Wednesday occurrence, not tomorrow.
    now = datetime(2026, 7, 21, 23, 0, tzinfo=timezone.utc)  # Wed 01:00 local
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    deadline = datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = "2026-07-28T05:00:00+02:00"
    plan["required_ac_kwh"] = 65.1
    plan["expected_delivery_kw"] = 11.04
    plan["completion_buffer_minutes"] = 30
    c._smart_plan = plan

    c._control_charging(now=now)

    schedule = next(call for call in tesla.calls
                    if isinstance(call, tuple) and call[0] == "schedule")
    assert schedule[2]["days_of_week"] == 8  # Wednesday
    assert schedule[2]["start_time"] == 30
    assert schedule[2]["end_time"] == 7 * 60


def test_far_deadline_never_installs_a_one_time_schedule_on_the_wrong_week(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=False)
    deadline = now + timedelta(days=20)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = (deadline - timedelta(hours=7)).isoformat()
    plan["required_ac_kwh"] = 65.1
    plan["expected_delivery_kw"] = 11.04
    plan["completion_buffer_minutes"] = 30
    c._smart_plan = plan

    c._control_charging(now=now)

    assert not any(isinstance(call, tuple) and call[0] == "schedule"
                   for call in tesla.calls)
    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert c.global_state["ev_smart_charge_fallback_status"] == (
        "fallback_waiting_for_representable_date_removed")


def test_removing_invalid_owned_fallback_stops_charge_it_started(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    # The old controller overwrote "confirmed" when it misclassified the resulting charge as
    # external. A successful deletion of our exact ID is independent ownership evidence.
    state = FakeState({"ev_smart_charge_fallback_status": "deferred_manual_override"})
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=8)
    plan = _smart_plan(now, active=False)
    deadline = now + timedelta(days=20)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = (deadline - timedelta(hours=7)).isoformat()
    plan["required_ac_kwh"] = 65.1
    plan["expected_delivery_kw"] = 11.04
    plan["completion_buffer_minutes"] = 30
    c._smart_plan = plan

    assert c._control_charging(now=now) is False

    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert "stop" in tesla.calls


def test_far_fallback_cleanup_stops_unauthorized_charge_when_owned_ids_absent(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    state = FakeState({"ev_smart_charge_fallback_status": "deferred_manual_override"})
    tesla = FakeTesla(is_charging=True)
    tesla.remove_owned_charge_schedule = lambda schedule_id: (
        tesla.calls.append(("remove_schedule", schedule_id))
        or (True, "schedule_not_found"))
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=8)
    plan = _smart_plan(now, active=False)
    deadline = now + timedelta(days=20)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = (deadline - timedelta(hours=7)).isoformat()
    plan["required_ac_kwh"] = 65.1
    plan["expected_delivery_kw"] = 11.04
    plan["completion_buffer_minutes"] = 30
    c._smart_plan = plan

    assert c._control_charging(now=now) is False

    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert "stop" in tesla.calls
    assert c.global_state["ev_smart_charge_controller_status"] == "waiting"


def test_invalid_fallback_cleanup_keeps_stop_ownership_until_retry_succeeds(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    state = FakeState({"ev_smart_charge_fallback_status": "confirmed"})
    tesla = FakeTesla(is_charging=True)
    outcomes = iter(("network", "ok"))

    def stop():
        result = next(outcomes)
        tesla.calls.append("stop")
        if result == "ok":
            tesla.is_charging = False
        return result

    tesla.stop_tesla_charge = stop
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=8)
    plan = _smart_plan(now, active=False)
    deadline = now + timedelta(days=20)
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    plan["latest_safe_start"] = (deadline - timedelta(hours=7)).isoformat()
    plan["required_ac_kwh"] = 65.1
    plan["expected_delivery_kw"] = 11.04
    plan["completion_buffer_minutes"] = 30
    c._smart_plan = plan

    c._control_charging(now=now)
    assert c._smart_cleanup_requires_stop is True
    assert tesla.calls.count("stop") == 1

    c._stop_backoff_until = 0.0
    c._control_charging(now=now + timedelta(minutes=1))

    assert tesla.calls.count("stop") == 2
    assert c._smart_cleanup_requires_stop is False


def test_no_future_fallback_window_fails_closed_with_visible_status(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime(2026, 7, 19, 20, 0, 30, tzinfo=timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    plan = _smart_plan(now, active=True)
    deadline = now + timedelta(seconds=20)
    plan["latest_safe_start"] = (now - timedelta(hours=2)).isoformat()
    plan["ready_by"] = deadline.isoformat()
    plan["job"]["ready_by"] = deadline.isoformat()
    c._smart_plan = plan

    c._control_charging(now=now)

    assert not any(isinstance(call, tuple) and call[0] in {"schedule", "limit"}
                   for call in tesla.calls)
    assert c.global_state["ev_smart_charge_fallback_status"] == "no_future_window"


@pytest.mark.parametrize(
    ("durable_job", "expected_status", "expected_reason"),
    [
        (None, "idle", "cancelled_cleaned_up"),
        ({"id": "job-1", "status": "paused", "target_soc": 80},
         "paused", "durable_job_status_changed"),
        ({"id": "replacement", "status": "active", "target_soc": 80},
         "waiting", "durable_job_replaced"),
    ],
)
def test_durable_job_change_blocks_old_plan_and_cleans_up_owned_control(
        monkeypatch, tmp_path, durable_job, expected_status, expected_reason):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_JOB_PATH=str(tmp_path / "job.json"),
        EV_SMART_CHARGE_PLAN_PATH=str(tmp_path / "plan.json"),
    )
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now)
    # The broker did not publish a replacement plan after the durable UI/script change.
    plan["generated_at"] = (
        now - timedelta(seconds=ecc.SMART_PLAN_MAX_AGE_S + 1)).isoformat()
    if durable_job is not None:
        durable_job = {
            **durable_job,
            "ready_by": plan["ready_by"],
        }
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=16)
    c._smart_plan = plan
    c._smart_job = durable_job
    c._smart_job_loaded = True
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._smart_schedule_signature = ("installed",)

    assert c._control_charging(now=now) is False

    assert "stop" in tesla.calls
    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert not any(isinstance(call, tuple) and call[0] == "amps" for call in tesla.calls)
    assert c.global_state["ev_smart_charge_controller_status"] == expected_status
    assert c.global_state["ev_smart_charge_controller_reason"] == expected_reason

    c._control_charging(now=now + timedelta(seconds=20))
    assert tesla.calls.count(("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID)) == 1


def test_same_job_edit_waits_for_matching_replan(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = plan
    c._smart_job = {
        "id": "job-1",
        "status": "active",
        "target_soc": 90,
        "ready_by": plan["ready_by"],
    }
    c._smart_job_loaded = True
    c._smart_schedule_signature = ("installed",)

    assert c._control_charging(now=now) is False
    assert ("remove_schedule", ecc.SMART_OWNED_SCHEDULE_ID) in tesla.calls
    assert "start" not in tesla.calls
    assert c.global_state["ev_smart_charge_controller_status"] == "waiting"
    assert c.global_state["ev_smart_charge_controller_reason"] == "durable_job_edited"


def test_cold_restart_with_no_job_and_idle_plan_does_not_remove_schedule(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = {
        "generated_at": now.isoformat(),
        "status": "idle",
        "job": None,
        "slots": [],
    }
    c._smart_job = None
    c._smart_job_loaded = True

    assert c._control_charging(now=now) is False
    assert tesla.calls == []


def test_refresh_loads_plan_and_durable_job_from_configured_paths(monkeypatch):
    _smart_settings(
        monkeypatch,
        EV_SMART_CHARGE_PLAN_PATH="/tmp/plan-under-test.json",
        EV_SMART_CHARGE_JOB_PATH="/tmp/job-under-test.json",
    )
    c = _charger(monkeypatch, FakeTesla(), surplus_amps=0, charging_amps=0)
    seen = []
    monkeypatch.setattr(
        ecc, "load_plan_snapshot",
        lambda **kwargs: (seen.append(("plan", kwargs["path"])), {"job": None})[1],
    )
    monkeypatch.setattr(
        ecc, "load_job",
        lambda **kwargs: (seen.append(("job", kwargs["path"])), {"id": "job-1"})[1],
    )

    c._refresh_smart_plan()

    assert seen == [
        ("plan", "/tmp/plan-under-test.json"),
        ("job", "/tmp/job-under-test.json"),
    ]
    assert c._smart_job == {"id": "job-1"}
    assert c._smart_job_loaded is True


def test_tesla_app_stop_during_owned_active_block_is_restarted(
        monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(monkeypatch, EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path))
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._last_commanded_amps = 24

    assert c._control_charging(now=now) is True
    assert ("amps", 24, 24.0) in tesla.calls
    assert "start" in tesla.calls
    assert c._smart_owns_charge is True


def test_our_gui_stop_block_suppression_survives_controller_restart(
        monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(monkeypatch, EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path))
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now)

    first_tesla = FakeTesla(is_charging=False)
    first = _charger(monkeypatch, first_tesla, surplus_amps=0, charging_amps=0)
    first._smart_plan = plan
    first._charge_mode = "smart"
    first._smart_owns_charge = True
    first._intent_off_edge = True
    first._fresh_stop_request = True
    first._control_charging(now=now)

    restarted_tesla = FakeTesla(is_charging=False)
    restarted = _charger(monkeypatch, restarted_tesla, surplus_amps=0, charging_amps=0)
    restarted._smart_plan = plan
    restarted._control_charging(now=now + timedelta(minutes=1))

    assert restarted_tesla.calls == []
    assert restarted.global_state["ev_smart_charge_controller_status"] == "manual_override"


def test_our_gui_stop_suppresses_active_block_without_process_ownership(
        monkeypatch, tmp_path):
    """An explicit dashboard Stop wins even after ownership state was lost."""
    state_path = tmp_path / "controller-state.json"
    _smart_settings(monkeypatch, EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path))
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=16)
    c._smart_plan = _smart_plan(now)
    c._smart_owns_charge = False
    c._charge_mode = None
    c._intent_off_edge = True
    c._fresh_stop_request = True

    c._control_charging(now=now)

    assert "stop" in tesla.calls
    assert c._smart_block_is_suppressed(c._smart_plan_context(now=now)) is True


def test_distinct_later_block_can_resume_after_our_gui_stop_suppression(
        monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(monkeypatch, EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path))
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._intent_off_edge = True
    c._fresh_stop_request = True
    c._control_charging(now=now)
    c._intent_off_edge = False
    c._fresh_stop_request = False
    tesla.calls.clear()

    later = now + timedelta(minutes=20)
    c._smart_plan = _smart_plan(later, target_kw=6.9)
    c._last_command_ts = 0
    c._control_charging(now=later)

    assert ("amps", 24, 24.0) in tesla.calls
    assert "start" in tesla.calls


def test_unplugged_job_sends_one_durable_noncritical_reminder(monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(
        monkeypatch,
        EV_PLUG_REMINDER_ENABLED="True",
        EV_PLUG_REMINDER_LEAD_MINUTES="45",
        EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path),
    )
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now)
    plan["plug_in_by"] = (now + timedelta(minutes=30)).isoformat()
    state = FakeState({"tesla_is_plugged": "False"})
    first_tesla = FakeTesla(is_plugged=False)
    first = _charger(monkeypatch, first_tesla, state=state, surplus_amps=0, charging_amps=0)
    first._smart_plan = plan
    second_tesla = FakeTesla(is_plugged=False)
    second = _charger(monkeypatch, second_tesla, state=state, surplus_amps=0, charging_amps=0)
    second._smart_plan = plan
    workers = _run_reminder_threads_inline(monkeypatch)
    notifications = []
    monkeypatch.setattr(ecc, "pushover_notification",
                        lambda *args, **kwargs: notifications.append((args, kwargs)))

    first._control_charging(now=now)

    # Simulate a controller/service restart: durable claim suppresses the duplicate.
    second._control_charging(now=now + timedelta(minutes=1))

    assert len(notifications) == 1
    assert len(workers) == 1 and workers[0].daemon is True
    assert "80%" in notifications[0][0][1]
    assert first_tesla.calls == second_tesla.calls == []


def test_plug_reminder_never_sends_when_pushed_state_is_plugged(monkeypatch, tmp_path):
    _smart_settings(
        monkeypatch,
        EV_PLUG_REMINDER_ENABLED="True",
        EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(tmp_path / "controller-state.json"),
    )
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now, active=False)
    plan["plug_in_by"] = (now + timedelta(minutes=20)).isoformat()
    state = FakeState({"tesla_is_plugged": "True"})
    c = _charger(monkeypatch, FakeTesla(is_plugged=True), state=state,
                 surplus_amps=0, charging_amps=0)
    c._smart_plan = plan
    _run_reminder_threads_inline(monkeypatch)
    notifications = []
    monkeypatch.setattr(ecc, "pushover_notification",
                        lambda *args, **kwargs: notifications.append(args))

    c._control_charging(now=now)

    assert notifications == []


def test_plug_reminder_failure_never_escapes_into_control(monkeypatch, tmp_path):
    _smart_settings(
        monkeypatch,
        EV_PLUG_REMINDER_ENABLED="True",
        EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(tmp_path / "controller-state.json"),
    )
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now)
    plan["plug_in_by"] = (now + timedelta(minutes=5)).isoformat()
    state = FakeState({"tesla_is_plugged": False})
    tesla = FakeTesla(is_plugged=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = plan
    _run_reminder_threads_inline(monkeypatch)
    monkeypatch.setattr(
        ecc, "pushover_notification",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pushover offline")),
    )

    assert c._control_charging(now=now) is False
    assert tesla.calls == []


def _pv_surplus_forecast(now, *, net_kw=4.0, minutes=60, generated_at=None):
    """Build a quarter-hour ESS forecast with a constant PV surplus after house load."""
    slot_start = now.replace(
        minute=(now.minute // 15) * 15, second=0, microsecond=0)
    slot_hours = 0.25
    rows = []
    for offset in range(0, minutes, 15):
        rows.append({
            "time": (slot_start + timedelta(minutes=offset)).isoformat(),
            "pv": (net_kw + 1.0) * slot_hours,
            "non_ev_load_kwh": 1.0 * slot_hours,
        })
    return {
        "generated_at": (generated_at or now).isoformat(),
        "slot_duration_h": slot_hours,
        "schedule": rows,
    }


def _pv_reminder_settings(monkeypatch, tmp_path, **overrides):
    values = {
        "EV_PV_SURPLUS_REMINDER_ENABLED": "True",
        "EV_PV_SURPLUS_REMINDER_FORECAST_MINUTES": "45",
        "EV_PV_SURPLUS_REMINDER_CONFIRM_MINUTES": "5",
        "TESLA_TELEMETRY_ENABLED": "True",
        "EV_SMART_CHARGE_CONTROLLER_STATE_PATH": str(
            tmp_path / "controller-state.json"),
        "AI_PLAN_EXPORT_PATH": "/not/read/in/unit-tests.json",
    }
    values.update(overrides)
    monkeypatch.setattr(ecc, "retrieve_setting", lambda key: values.get(key))


def test_pv_surplus_reminder_is_normal_priority_durable_and_cost_free(
        monkeypatch, tmp_path):
    _pv_reminder_settings(monkeypatch, tmp_path)
    now = datetime(2026, 7, 26, 12, 7, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_is_home": "True",
        "tesla_is_plugged": "False",
        "tesla_soc": 55,
        "tesla_soc_setpoint": 80,
        "pv_power_updated_at": now.timestamp(),
    })
    plan = _pv_surplus_forecast(now)
    notifications = []
    monkeypatch.setattr(
        ecc.EvCharger,
        "_load_ess_plan_for_surplus_reminder",
        lambda self: plan,
    )
    _run_reminder_threads_inline(monkeypatch)

    first_tesla = FakeTesla(is_home=True, is_plugged=False)
    first = _charger(
        monkeypatch, first_tesla, state=state,
        ess_soc=95, surplus_amps=5, surplus_watts=3450)
    first._pv_surplus_reminder_candidate_since = (
        now.timestamp() - 5 * 60 - 1)

    # A process restart on the same day must not duplicate the gentle nudge.
    second_tesla = FakeTesla(is_home=True, is_plugged=False)
    second = _charger(
        monkeypatch, second_tesla, state=state,
        ess_soc=95, surplus_amps=5, surplus_watts=3450)
    second._pv_surplus_reminder_candidate_since = now.timestamp() - 600
    monkeypatch.setattr(
        ecc, "pushover_notification",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )
    first._maybe_send_pv_surplus_reminder(now=now)
    second._maybe_send_pv_surplus_reminder(now=now + timedelta(minutes=1))

    assert len(notifications) == 1
    assert "solar" in notifications[0][0][0].lower()
    assert "55%" in notifications[0][0][1]
    assert "80%" in notifications[0][0][1]
    assert "45" in notifications[0][0][1]
    assert first_tesla.calls == second_tesla.calls == []


@pytest.mark.parametrize(
    "state_updates",
    [
        {"tesla_is_home": "False"},
        {"tesla_is_plugged": "True"},
        {"tesla_soc": 80},
        {"tesla_soc": None},
        {"tesla_soc_setpoint": None},
    ],
)
def test_pv_surplus_reminder_requires_explicit_vehicle_eligibility(
        monkeypatch, tmp_path, state_updates):
    _pv_reminder_settings(monkeypatch, tmp_path)
    now = datetime(2026, 7, 26, 12, 7, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_is_home": "True",
        "tesla_is_plugged": "False",
        "tesla_soc": 55,
        "tesla_soc_setpoint": 80,
        "pv_power_updated_at": now.timestamp(),
        **state_updates,
    })
    c = _charger(
        monkeypatch, FakeTesla(), state=state,
        ess_soc=95, surplus_amps=5, surplus_watts=3450)
    c._pv_surplus_reminder_candidate_since = now.timestamp() - 600
    monkeypatch.setattr(
        c, "_load_ess_plan_for_surplus_reminder",
        lambda: _pv_surplus_forecast(now))
    notifications = []
    monkeypatch.setattr(
        ecc, "pushover_notification", lambda *args: notifications.append(args))
    _run_reminder_threads_inline(monkeypatch)

    c._maybe_send_pv_surplus_reminder(now=now)

    assert notifications == []


def test_pv_surplus_reminder_requires_stable_live_surplus_and_resets(
        monkeypatch, tmp_path):
    _pv_reminder_settings(monkeypatch, tmp_path)
    now = datetime(2026, 7, 26, 12, 7, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_is_home": "True",
        "tesla_is_plugged": "False",
        "tesla_soc": 55,
        "tesla_soc_setpoint": 80,
        "pv_power_updated_at": now.timestamp(),
    })
    c = _charger(
        monkeypatch, FakeTesla(), state=state,
        ess_soc=95, surplus_amps=5, surplus_watts=3450)
    monkeypatch.setattr(
        c, "_load_ess_plan_for_surplus_reminder",
        lambda: _pv_surplus_forecast(now))
    notifications = []
    monkeypatch.setattr(
        ecc, "pushover_notification", lambda *args: notifications.append(args))
    _run_reminder_threads_inline(monkeypatch)

    c._maybe_send_pv_surplus_reminder(now=now)
    assert c._pv_surplus_reminder_candidate_since == now.timestamp()
    assert notifications == []

    almost_ready = now + timedelta(minutes=4, seconds=59)
    state["pv_power_updated_at"] = almost_ready.timestamp()
    c._maybe_send_pv_surplus_reminder(now=almost_ready)
    assert notifications == []

    c.surplus_amps = 0
    c._maybe_send_pv_surplus_reminder(now=now + timedelta(minutes=5))
    assert c._pv_surplus_reminder_candidate_since is None


def test_pv_surplus_reminder_rejects_retained_stale_pv_power(
        monkeypatch, tmp_path):
    _pv_reminder_settings(monkeypatch, tmp_path)
    now = datetime(2026, 7, 26, 12, 7, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_is_home": "True",
        "tesla_is_plugged": "False",
        "tesla_soc": 55,
        "tesla_soc_setpoint": 80,
        "pv_power_updated_at": (
            now - timedelta(seconds=ecc.PV_SURPLUS_REMINDER_LIVE_FRESH_S + 1)
        ).timestamp(),
    })
    c = _charger(
        monkeypatch, FakeTesla(), state=state,
        ess_soc=95, surplus_amps=5, surplus_watts=3450)
    c._pv_surplus_reminder_candidate_since = now.timestamp() - 600
    notifications = []
    monkeypatch.setattr(
        ecc, "pushover_notification", lambda *args: notifications.append(args))
    _run_reminder_threads_inline(monkeypatch)

    c._maybe_send_pv_surplus_reminder(now=now)

    assert c._pv_surplus_reminder_candidate_since is None
    assert notifications == []


@pytest.mark.parametrize("forecast_case", ("weak", "short", "stale"))
def test_pv_surplus_reminder_requires_fresh_continuous_45_minute_forecast(
        monkeypatch, tmp_path, forecast_case):
    _pv_reminder_settings(monkeypatch, tmp_path)
    now = datetime(2026, 7, 26, 12, 7, tzinfo=timezone.utc)
    state = FakeState({
        "tesla_is_home": "True",
        "tesla_is_plugged": "False",
        "tesla_soc": 55,
        "tesla_soc_setpoint": 80,
        "pv_power_updated_at": now.timestamp(),
    })
    c = _charger(
        monkeypatch, FakeTesla(), state=state,
        ess_soc=95, surplus_amps=5, surplus_watts=3450)
    c._pv_surplus_reminder_candidate_since = now.timestamp() - 600
    if forecast_case == "weak":
        plan = _pv_surplus_forecast(now, net_kw=0.5)
    elif forecast_case == "short":
        plan = _pv_surplus_forecast(now, minutes=30)
    else:
        plan = _pv_surplus_forecast(
            now, generated_at=now - timedelta(minutes=21))
    monkeypatch.setattr(
        c, "_load_ess_plan_for_surplus_reminder", lambda: plan)
    notifications = []
    monkeypatch.setattr(
        ecc, "pushover_notification", lambda *args: notifications.append(args))
    _run_reminder_threads_inline(monkeypatch)

    c._maybe_send_pv_surplus_reminder(now=now)

    assert notifications == []
