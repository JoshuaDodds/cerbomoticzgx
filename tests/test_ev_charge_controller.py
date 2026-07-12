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
        self.calls = []

    def start_tesla_charge(self):
        self.calls.append("start"); self.is_charging = True; return True

    def stop_tesla_charge(self):
        self.calls.append("stop"); self.is_charging = False; return "ok"

    def set_tesla_charge_amps(self, amps):
        self.calls.append(("amps", amps)); return True

    def update_vehicle_status(self, force=False):
        pass


class FakeState(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def set(self, k, v):
        self[k] = v


def _charger(monkeypatch, tesla, state=None, **attrs):
    monkeypatch.setattr(ecc, "publish_message", lambda *a, **k: None)
    monkeypatch.setattr(ecc, "pushover_notification_critical", lambda *a, **k: None)
    monkeypatch.setattr(ecc.EvCharger, "is_the_sun_shining", staticmethod(lambda: attrs.get("sun", True)))
    c = ecc.EvCharger.__new__(ecc.EvCharger)
    c.tesla = tesla
    c.global_state = state if state is not None else FakeState()
    c.minimum_ess_soc = 90
    c._last_command_ts = 0.0
    c._low_surplus_since = None
    c._intent_off_edge = False
    c._intent_was_on = False
    c._charge_mode = attrs.get("charge_mode", None)
    c._stop_backoff_until = 0.0
    c._last_stop_alert_ts = 0.0
    c.ess_soc = attrs.get("ess_soc", 95)
    c.surplus_amps = attrs.get("surplus_amps", 6)
    c.charging_amps = attrs.get("charging_amps", 0)
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


def test_grid_assist_charges_full_and_ignores_surplus(monkeypatch):
    # Grid-assist ON with NO surplus is an express override: start charging from grid and do
    # NOT try to match the current to surplus.
    state = FakeState({"grid_charging_enabled": "True"})
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, state=state, surplus_amps=0, charging_amps=0)
    assert c._control_charging() is True
    assert "start" in tesla.calls
    assert not any(isinstance(x, tuple) and x[0] == "amps" for x in tesla.calls)


def test_grid_assist_does_not_stop_on_low_surplus(monkeypatch):
    # Charging under grid-assist while surplus is negative must NOT trigger a surplus-loss stop.
    state = FakeState({"grid_charging_enabled": "True"})
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
    monkeypatch.setattr(ecc, "pushover_notification_critical", lambda *a, **k: alerts.append(a))
    c._intent_off_edge = True
    c._control_charging()
    assert tesla.calls.count("stop") == 1
    assert len(alerts) == 1                # user alerted for manual intervention
    c._intent_off_edge = False
    c._control_charging()                 # still within STOP_RETRY_BACKOFF_S -> no retry
    assert tesla.calls.count("stop") == 1


def test_engagement_signal_dormant_when_idle(monkeypatch):
    tesla = FakeTesla(is_charging=False)
    c = _charger(monkeypatch, tesla, ess_soc=95, surplus_amps=0, charging_amps=0, sun=True)
    # No intent, no surplus (0A), not charging locally -> nothing should engage the API.
    assert c._local_engagement_signal() is False
    # Intent flips it on.
    c.global_state.set("grid_charging_enabled", "True")
    assert c._local_engagement_signal() is True
