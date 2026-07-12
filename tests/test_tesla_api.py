"""Behavioural tests for the cost-critical parts of tesla_api.

We build the TeslaApi via __new__ to skip __init__ (which starts a thread and touches
MQTT), then inject fakes for the transport + budget. The goal is to pin the money-safety
behaviours: never wake to read, single read per poll, and hard budget gating.
"""
from lib import tesla_api


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Budget:
    def __init__(self, allow=True):
        self.allow = allow
        self.spent = []

    def spend(self, category, n=1):
        self.spent.append(category)
        return self.allow


def _bare_api(budget, request_fn):
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)   # bypass __init__/thread/MQTT
    api._vehicle_id = "VID"
    api._budget = budget
    api._request = request_fn
    return api


def test_get_vehicle_data_does_not_wake_when_asleep():
    # A sleeping car returns 408; a plain read must NOT wake it (wake = $0.02).
    budget = _Budget(allow=True)
    api = _bare_api(budget, lambda *a, **k: _Resp(status_code=408))
    woke = {"n": 0}
    api.wake_vehicle = lambda: woke.__setitem__("n", woke["n"] + 1) or True

    assert api.get_vehicle_data(allow_wake=False) is None
    assert woke["n"] == 0                     # never woke to read
    assert budget.spent == ["data"]           # one gated data attempt, nothing more


def test_get_vehicle_data_blocked_by_budget_makes_no_request():
    budget = _Budget(allow=False)
    calls = {"n": 0}

    def req(*a, **k):
        calls["n"] += 1
        return _Resp(200, {"response": {"charge_state": {}}})

    api = _bare_api(budget, req)
    assert api.get_vehicle_data() is None
    assert calls["n"] == 0                     # guard blocked before any HTTP call
    assert budget.spent == ["data"]


def test_get_vehicle_data_returns_payload_when_online():
    budget = _Budget(allow=True)
    payload = {"response": {"charge_state": {"battery_level": 55}}}
    api = _bare_api(budget, lambda *a, **k: _Resp(200, payload))
    assert api.get_vehicle_data() == payload["response"]


def test_command_blocked_by_budget_returns_false_without_request():
    budget = _Budget(allow=False)
    calls = {"n": 0}

    def req(*a, **k):
        calls["n"] += 1
        return _Resp(200, {"response": {"result": True}})

    api = _bare_api(budget, req)
    assert api._command("charge_start", "err") is False
    assert calls["n"] == 0
    assert budget.spent == ["command"]


def test_stop_charge_robust_wakes_and_retries_when_asleep():
    # First stop is rejected because the bus is asleep -> force a wake and retry -> ok.
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    seq = [(False, "asleep"), (True, "ok")]
    calls = {"cmd": 0, "wake": 0, "stopped": 0}
    api._command_ex = lambda name, json_body=None, error_msg="": (calls.__setitem__("cmd", calls["cmd"] + 1), seq.pop(0))[1]
    api.wake_vehicle = lambda skip_online_check=False: (calls.__setitem__("wake", calls["wake"] + 1), True)[1]
    api._on_charge_stopped = lambda: calls.__setitem__("stopped", calls["stopped"] + 1)
    assert api.stop_charge_robust() == "ok"
    assert calls == {"cmd": 2, "wake": 1, "stopped": 1}


def test_stop_charge_robust_reports_network_failure():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._command_ex = lambda name, json_body=None, error_msg="": (False, "network")
    api.wake_vehicle = lambda skip_online_check=False: True
    api._on_charge_stopped = lambda: None
    assert api.stop_charge_robust() == "network"


def test_command_ex_classifies_asleep_and_network():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._vehicle_id = "V"
    api._budget = _Budget(allow=True)
    api._request = lambda *a, **k: _Resp(200, {"response": {"result": False, "reason": "could_not_wake_buses"}})
    assert api._command_ex("charge_stop") == (False, "asleep")
    api._budget = _Budget(allow=True)
    api._request = lambda *a, **k: _Resp(500, {})
    assert api._command_ex("charge_stop") == (False, "network")


def test_poll_interval_shorter_while_charging(monkeypatch):
    monkeypatch.setattr(tesla_api, "retrieve_setting", lambda name: None)   # use defaults
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.is_charging = False
    assert api._poll_interval_seconds() == tesla_api.DEFAULT_POLL_INTERVAL_MIN * 60
    api.is_charging = True
    assert api._poll_interval_seconds() == tesla_api.DEFAULT_POLL_INTERVAL_CHARGING_MIN * 60


def test_poll_interval_backs_off_when_asleep(monkeypatch):
    monkeypatch.setattr(tesla_api, "retrieve_setting", lambda name: None)
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.is_charging = False
    api._asleep = True
    assert api._poll_interval_seconds() == tesla_api.DEFAULT_POLL_INTERVAL_ASLEEP_MIN * 60


def test_update_vehicle_status_throttles_after_asleep(monkeypatch):
    # Regression: the old throttle only advanced on success, so an asleep car was re-polled
    # every tick. It must now back off after a no-data (asleep) read.
    monkeypatch.setattr(tesla_api, "retrieve_setting", lambda name: None)
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.is_charging = False
    api._asleep = False
    api._last_read_attempt_ts = 0
    api.last_update_ts = 0
    api.last_update_ts_hr = 0
    calls = {"n": 0}
    api.get_vehicle_data = lambda allow_wake=False: calls.__setitem__("n", calls["n"] + 1)

    api.update_vehicle_status()          # first call: due -> one read, marks asleep
    assert calls["n"] == 1
    assert api._asleep is True
    api.update_vehicle_status()          # immediately after: asleep interval not elapsed -> no read
    assert calls["n"] == 1
