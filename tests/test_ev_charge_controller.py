"""Tests for the cleaned-up EV charge control logic.

Built via __new__ to skip __init__ (which constructs a TeslaApi + MQTT client). We inject
a fake Tesla that records commands, a dict-like state, and shadow the dynamic bus
properties with plain instance attributes.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest

from lib import ev_charge_controller as ecc


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


def test_starts_surplus_charge_when_home_plugged_and_surplus(monkeypatch):
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=6, charging_amps=0)
    active = c._control_charging()
    assert active is True
    assert ("amps", 6) in tesla.calls          # set current to surplus
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
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    assert c._local_engagement_signal() is True


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
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=8)
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
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=8)
    c._control_charging()                        # opens grace window
    assert c._low_surplus_since is not None
    c.surplus_amps = 6                           # surplus came back
    c._control_charging()
    assert c._low_surplus_since is None          # pending stop cancelled
    assert "stop" not in tesla.calls


def test_adjusts_amps_to_track_surplus_while_charging(monkeypatch):
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=10, charging_amps=6)   # surplus rose to 10
    assert c._control_charging() is True
    assert ("amps", 10) in tesla.calls
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
    # Regression (decoupling): intent must follow the DEDICATED ev_charge_requested flag only.
    # Grid-assist (grid_charging_enabled) controls the house battery and must NOT touch the car;
    # our own tesla_charge_requested latch must also be ignored.
    state = FakeState({"grid_charging_enabled": "True", "tesla_charge_requested": "True",
                       "ev_charge_requested": "False"})
    c = _charger(monkeypatch, FakeTesla(is_charging=False), state=state, surplus_amps=0, charging_amps=0)
    assert c._intent_on() is False
    assert c._local_engagement_signal() is False        # grid-assist on does NOT engage the car
    state["ev_charge_requested"] = "True"
    assert c._intent_on() is True


def test_grid_assist_toggle_never_commands_the_car(monkeypatch):
    # Toggling grid-assist on with a home+plugged car and no surplus must issue NO commands:
    # EV charging is fully decoupled from grid-assist.
    state = FakeState({"grid_charging_enabled": "True", "ev_charge_requested": "False"})
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    assert c._control_charging() is False
    assert tesla.calls == []


def test_ev_charge_request_charges_full_and_ignores_surplus(monkeypatch):
    # The dedicated EV-charge request ON with NO surplus is an express override: start charging
    # at the safe full-rate request and do NOT try to match the current to surplus.
    state = FakeState({"ev_charge_requested": "True"})
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    assert c._control_charging() is True
    assert "start" in tesla.calls
    assert ("amps", 23, 24.0) in tesla.calls


def test_ev_charge_request_does_not_stop_on_low_surplus(monkeypatch):
    # Charging under an EV-charge request while surplus is negative must NOT trigger a
    # surplus-loss stop.
    state = FakeState({"ev_charge_requested": "True"})
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
    assert tesla.calls.count(("update_vehicle_status", True, True)) == 1
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


def test_disabling_apply_relinquishes_owned_charge_without_a_command(monkeypatch):
    _smart_settings(monkeypatch, EV_SMART_CHARGE_APPLY="False")
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=16)
    c._charge_mode = "smart"
    c._smart_owns_charge = True

    assert c._control_charging() is True
    assert tesla.calls == []
    assert c._smart_owns_charge is False
    assert c._charge_mode == "smart_released"


def test_smart_feature_off_preserves_legacy_surplus_path(monkeypatch):
    _smart_settings(monkeypatch, EV_SMART_CHARGE_ENABLED="False")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=6, charging_amps=0)
    c._smart_plan = _smart_plan(now)

    assert c._control_charging() is True
    assert ("amps", 6) in tesla.calls
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
    assert ("amps", 6) in tesla.calls
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


def test_applied_smart_job_does_not_adjust_external_charge_for_surplus(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(
        monkeypatch, tesla,
        ess_soc=95, surplus_amps=6, charging_amps=6, sun=True,
    )
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging(now=now) is True
    assert not any(isinstance(call, tuple) and call[0] == "amps" for call in tesla.calls)
    assert c.global_state["ev_smart_charge_controller_reason"] == "external_charge_in_progress"


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


def test_external_start_stop_suppresses_cloudy_solar_slot(monkeypatch, tmp_path):
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

    assert c._control_charging(now=now) is True
    assert tesla.calls == []

    tesla.is_charging = False
    c.charging_amps = 0
    c.surplus_amps = 6
    assert c._control_charging(now=now + timedelta(minutes=1)) is False
    assert "start" not in tesla.calls
    assert c.global_state["ev_smart_charge_controller_reason"] == "charge_block_suppressed"


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
    state = FakeState({"ev_charge_requested": "True"})
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=3.0)

    assert c._control_charging() is True
    assert "start" in tesla.calls
    # Manual grid charging restores the configured 16 kW ceiling instead of
    # inheriting a prior 1-5 A solar request. At 3x230 V that is 23 A/phase.
    assert ("amps", 23, 24.0) in tesla.calls
    assert not any(isinstance(call, tuple) and call[0] in {"schedule", "limit"}
                   for call in tesla.calls)
    assert c._charge_mode == "grid"


def test_manual_grid_override_raises_existing_five_amp_solar_session(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    state = FakeState({
        "ev_charge_requested": "True",
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
    assert tesla.calls == [("amps", 23, 24.0)]
    assert c._charge_mode == "grid"
    assert state["ev_grid_charge_current_status"] == "confirmation_pending"


def test_manual_grid_session_sends_initial_current_even_if_retained_value_matches(
        monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "ev_charge_requested": "True",
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

    assert tesla.calls.count(("amps", 23, 24.0)) == 1
    assert tesla.calls.count("start") == 1
    assert state["ev_grid_charge_current_status"] == "confirmation_pending"


def test_manual_grid_current_retries_once_after_tesla_telemetry_timeout(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "ev_charge_requested": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 5,
        "tesla_charge_current_request_updated_at": now.timestamp() - 5,
    })
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)

    assert c._control_charging(now=now) is True
    assert tesla.calls.count(("amps", 23, 24.0)) == 1
    assert tesla.calls.count("start") == 1

    c._control_charging(now=now + timedelta(seconds=59))
    assert tesla.calls.count(("amps", 23, 24.0)) == 1

    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=61))
    assert tesla.calls.count(("amps", 23, 24.0)) == 2

    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=122))
    assert tesla.calls.count(("amps", 23, 24.0)) == 2
    assert state["ev_grid_charge_current_status"] == "unconfirmed"


def test_manual_grid_delivery_below_five_does_not_fight_maxem_after_ack(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    state = FakeState({
        "ev_charge_requested": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 5,
        "tesla_charge_current_request_updated_at": now.timestamp() - 5,
    })
    c = _charger(monkeypatch, tesla, state=state, charging_amps=4)

    c._control_charging(now=now)
    assert tesla.calls.count(("amps", 23, 24.0)) == 1

    state["tesla_charge_current_request"] = 23
    state["tesla_charge_current_request_updated_at"] = (now + timedelta(seconds=5)).timestamp()
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=65))
    c._control_charging(now=now + timedelta(seconds=130))

    # Requested current was confirmed, so low ABB delivery is Maxem/ramp
    # observation only and never causes another Fleet current command.
    assert tesla.calls.count(("amps", 23, 24.0)) == 1
    assert state["ev_grid_charge_current_status"] == "delivery_limited"


def test_manual_grid_does_not_accept_an_unstamped_local_command_shadow(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    tesla.charging_amp_limit = 23
    tesla.charge_current_request_update_ts = 0
    state = FakeState({
        "ev_charge_requested": "True",
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    c = _charger(monkeypatch, tesla, state=state, charging_amps=0)

    c._control_charging(now=now)

    assert ("amps", 23, 24.0) in tesla.calls
    assert state["ev_grid_charge_current_status"] == "confirmation_pending"


def test_active_smart_block_sets_clamped_target_once_then_starts(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="20")
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 16,
    })
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=16.0)

    assert c._control_charging() is True
    assert tesla.calls.count(("amps", 16, 20.0)) == 1
    assert tesla.calls.count("start") == 1
    assert c._charge_mode == "smart"
    assert c._smart_owns_charge is True
    assert state["ev_smart_charge_controller_status"] == "starting"


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


def test_unconfirmed_charge_limit_stops_after_three_attempts(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    limit_calls = []
    tesla.vehicle_soc_setpoint = 70
    tesla.charge_limit_update_ts = now.timestamp() - 10
    tesla.set_tesla_charge_limit = lambda value: limit_calls.append(value) or (True, "ok")
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, active=False)

    for seconds in (0, 61, 122):
        c._control_charging(now=now + timedelta(seconds=seconds))

    assert limit_calls == [80, 80, 80]
    assert c.global_state["ev_smart_charge_fallback_status"] == "limit_pending"

    c._control_charging(now=now + timedelta(seconds=183))
    assert c.global_state["ev_smart_charge_fallback_status"] == "limit_unconfirmed"


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

    assert limit_calls == [80, 80, 80]
    assert not any(isinstance(call, tuple) and call[0] == "schedule"
                   for call in tesla.calls)
    assert c.global_state["ev_smart_charge_fallback_status"] == "limit_unconfirmed"


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

    c._control_charging(now=now)
    c._control_charging(now=now + timedelta(seconds=61))

    assert limit_calls == [80, 80]
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
    assert tesla.calls.count(("amps", 16, 24.0)) == 1

    # ABB delivery remains only 5 A because Maxem is throttling, but the lack of a pushed
    # requested-current acknowledgement—not delivered current—is what permits one retry.
    c.charging_amps = 5
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=61))
    assert tesla.calls.count(("amps", 16, 24.0)) == 2

    state["tesla_charge_current_request"] = 16
    state["tesla_charge_current_request_updated_at"] = (
        now + timedelta(seconds=70)).timestamp()
    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=75))

    assert tesla.calls.count(("amps", 16, 24.0)) == 2
    assert c._smart_current_pending is None


def test_rejected_current_command_is_bounded_and_never_starts(monkeypatch):
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

    assert tesla.calls.count(("amps", 16, 24.0)) == 3
    assert "start" not in tesla.calls
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "set_current_unconfirmed")


def test_third_accepted_current_command_gets_full_acknowledgement_window(monkeypatch):
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

    for seconds in (0, 61, 122):
        c._last_command_ts = 0
        c._control_charging(now=now + timedelta(seconds=seconds))
    assert tesla.calls.count(("amps", 16, 24.0)) == 3

    c._last_command_ts = 0
    c._control_charging(now=now + timedelta(seconds=150))
    assert c.global_state["ev_smart_charge_controller_reason"] == "set_current_pending"

    c._control_charging(now=now + timedelta(seconds=183))
    assert c.global_state["ev_smart_charge_controller_reason"] == (
        "set_current_unconfirmed")


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


def test_rejected_start_is_retried_in_one_minute_then_suppresses_block(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
        "tesla_charge_current_request": 16,
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

    for seconds in (0, 30, 61, 122, 183):
        c._last_command_ts = 0
        c._control_charging(now=now + timedelta(seconds=seconds))

    assert len(starts) == 3
    assert len(suppressed) == 1
    assert c._smart_start_pending is None
    assert c.global_state["ev_smart_charge_controller_status"] == "manual_override"


def test_smart_sub_five_amp_target_calls_existing_setter_only_once(monkeypatch):
    """TeslaApi itself owns the documented deliberate double-send workaround."""
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="24")
    now = datetime.now(timezone.utc)
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now, target_kw=2.8)  # floor(2800 / 690) = 4 A

    c._control_charging()

    assert tesla.calls.count(("amps", 4, 24.0)) == 1
    assert "start" in tesla.calls


def test_smart_target_amps_rejects_implausible_phase_voltage_telemetry(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="24")
    state = FakeState({
        "tesla_charger_phases": 99,
        "tesla_charger_voltage": 12,
        "tesla_charge_current_max": 24,
    })
    c = _charger(monkeypatch, FakeTesla(), state=state)

    assert c._smart_target_amps({"requested_power_kw": 16.0}) == 23


def test_positive_partial_tail_uses_one_amp_instead_of_being_treated_as_stopped(monkeypatch):
    _smart_settings(monkeypatch, EV_CHARGER_MAX_AMPS="24")
    state = FakeState({
        "tesla_charger_phases": 3,
        "tesla_charger_voltage": 230,
        "tesla_charge_current_max": 24,
    })
    c = _charger(monkeypatch, FakeTesla(), state=state)

    assert c._smart_target_amps({"requested_power_kw": 0.1}) == 1


def test_smart_controller_does_not_chase_maxem_throttled_meter(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=5)
    c._smart_plan = _smart_plan(now, target_kw=11.04)  # 16 A at 3x230 V
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._last_commanded_amps = 16
    c._last_command_ts = 0

    assert c._control_charging() is True
    assert not any(isinstance(call, tuple) and call[0] == "amps" for call in tesla.calls)


def test_external_charge_outside_smart_block_is_never_stopped(monkeypatch):
    _smart_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=True)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=12)
    c._smart_plan = _smart_plan(now, active=False)

    assert c._control_charging() is True
    assert "stop" not in tesla.calls
    assert tesla.calls == []
    assert c.global_state["ev_smart_charge_controller_status"] == "manual_override"


def test_external_start_then_stop_is_not_reversed_in_same_smart_block(
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
    assert tesla.calls == []

    # The user stops their externally-started charge. The same selected block is durably
    # suppressed, so automation cannot send current/start and reverse that manual stop.
    tesla.is_charging = False
    c.charging_amps = 0
    assert c._control_charging(now=now + timedelta(minutes=1)) is False
    assert tesla.calls == []
    assert c.global_state["ev_smart_charge_controller_reason"] == "charge_block_suppressed"


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


def test_far_fallback_cleanup_does_not_stop_manual_charge_when_owned_ids_absent(
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

    assert c._control_charging(now=now) is True

    assert "stop" not in tesla.calls
    assert c.global_state["ev_smart_charge_controller_status"] == "manual_override"


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
        (None, "cancelled", "durable_job_removed"),
        ({"id": "job-1", "status": "paused", "target_soc": 80},
         "paused", "durable_job_status_changed"),
        ({"id": "replacement", "status": "active", "target_soc": 80},
         "waiting", "durable_job_replaced"),
    ],
)
def test_durable_job_change_blocks_old_plan_and_cleans_up_owned_control(
        monkeypatch, durable_job, expected_status, expected_reason):
    _smart_settings(monkeypatch)
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


def test_user_stop_during_owned_active_block_is_not_restarted(monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(monkeypatch, EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path))
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._last_commanded_amps = 16

    assert c._control_charging(now=now) is False
    assert tesla.calls == []
    assert c._smart_owns_charge is False
    assert c.global_state["ev_smart_charge_controller_status"] == "manual_override"

    # Still inside the same slot: no current/start retry, even after process-local ownership
    # was relinquished.
    c._control_charging(now=now + timedelta(minutes=1))
    assert tesla.calls == []


def test_stopped_owned_block_suppression_survives_controller_restart(monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(monkeypatch, EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path))
    now = datetime.now(timezone.utc)
    plan = _smart_plan(now)

    first_tesla = FakeTesla(is_charging=False)
    first = _charger(monkeypatch, first_tesla, surplus_amps=0, charging_amps=0)
    first._smart_plan = plan
    first._charge_mode = "smart"
    first._smart_owns_charge = True
    first._control_charging(now=now)

    restarted_tesla = FakeTesla(is_charging=False)
    restarted = _charger(monkeypatch, restarted_tesla, surplus_amps=0, charging_amps=0)
    restarted._smart_plan = plan
    restarted._control_charging(now=now + timedelta(minutes=1))

    assert restarted_tesla.calls == []
    assert restarted.global_state["ev_smart_charge_controller_status"] == "manual_override"


def test_distinct_later_block_can_resume_after_prior_block_suppression(monkeypatch, tmp_path):
    state_path = tmp_path / "controller-state.json"
    _smart_settings(monkeypatch, EV_SMART_CHARGE_CONTROLLER_STATE_PATH=str(state_path))
    now = datetime.now(timezone.utc)
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, surplus_amps=0, charging_amps=0)
    c._smart_plan = _smart_plan(now)
    c._charge_mode = "smart"
    c._smart_owns_charge = True
    c._control_charging(now=now)  # records the first-block manual/no-power stop
    tesla.calls.clear()

    later = now + timedelta(minutes=20)
    c._smart_plan = _smart_plan(later, target_kw=6.9)
    c._last_command_ts = 0
    c._control_charging(now=later)

    assert ("amps", 10, 24.0) in tesla.calls
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
