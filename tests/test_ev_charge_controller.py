"""Tests for the cleaned-up EV charge control logic.

Built via __new__ to skip __init__ (which constructs a TeslaApi + MQTT client). We inject
a fake Tesla that records commands, a dict-like state, and shadow the dynamic bus
properties with plain instance attributes.
"""
import time

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
        self.time_until_full = "N/A"
        # Fresh by default so existing is_charging=False assertions keep behaving as "confirmed
        # off"; tests exercising staleness override this explicitly.
        self.last_update_ts = kw.get("last_update_ts", time.time())
        self.calls = []

    def start_tesla_charge(self):
        self.calls.append("start"); self.is_charging = True; return True

    def stop_tesla_charge(self):
        self.calls.append("stop"); self.is_charging = False; return "ok"

    def set_tesla_charge_amps(self, amps):
        self.calls.append(("amps", amps)); return True

    def update_vehicle_status(self, force=False, allow_wake=False):
        self.calls.append(("update_vehicle_status", force, allow_wake))


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
    # at the car's own rate and do NOT try to match the current to surplus.
    state = FakeState({"ev_charge_requested": "True"})
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    assert c._control_charging() is True
    assert "start" in tesla.calls
    assert not any(isinstance(x, tuple) and x[0] == "amps" for x in tesla.calls)


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


def test_refresh_request_forces_wake_and_clears_itself(monkeypatch):
    # A full main() tick with a pending refresh request, and NO other engagement signal
    # (no intent, no surplus, not charging) must: stay engaged rather than take the dormant
    # early-return (proven by update_vehicle_status being reached at all), force a wake+refresh
    # read, and clear the one-shot flag so it doesn't re-trigger next tick.
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
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, ess_soc=95, surplus_amps=0, charging_amps=0, sun=False)
    monkeypatch.setattr(c, "_reschedule", lambda *a, **k: None)
    monkeypatch.setattr(c, "_control_charging", lambda: False)
    c.global_state.set("vehicle_refresh_requested", "True")

    c.main()   # consumes + clears the flag
    tesla.calls.clear()
    c.main()   # nothing should re-engage the controller this time

    assert tesla.calls == []
