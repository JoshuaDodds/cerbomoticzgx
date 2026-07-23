"""Behavioural tests for the cost-critical parts of tesla_api.

We build the TeslaApi via __new__ to skip __init__ (which starts a thread and touches
MQTT), then inject fakes for the transport + budget. The goal is to pin the money-safety
behaviours: never wake to read, single read per poll, and hard budget gating.
"""
import base64
import json
import os
import threading
import time
from pathlib import Path

import pytest

from lib import tesla_api


@pytest.fixture(autouse=True)
def _never_read_live_tesla_secrets(monkeypatch):
    """Authentication tests must never inspect or expose the operator's real tokens."""
    monkeypatch.setattr(tesla_api, "dotenv_values", lambda path: {})


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
        self.refunded = []

    def spend(self, category, n=1, critical=False):
        self.spent.append(category)
        return True if critical else self.allow

    def refund(self, category, n=1):
        self.refunded.append(category)


def _bare_api(budget, request_fn):
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)   # bypass __init__/thread/MQTT
    api._vehicle_id = "VID"
    api._budget = budget
    api._request = request_fn
    return api


def _auth_api():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._client_id = "client-id"
    api._client_secret = "client-secret"
    api._refresh_token = "refresh-old"
    api._access_token = None
    api._token_expires_at = 0
    api._token_lock = threading.Lock()
    api._auth_retry_after = 0
    api._auth_failure_code = None
    api._auth_failure_status = 0
    api._budget = _Budget(allow=True)
    return api


def _jwt_with_exp(expiry):
    def encoded(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encoded({'alg': 'none'})}.{encoded({'exp': expiry})}.signature"


def test_oauth_exchanges_use_current_fleet_auth_domain():
    assert tesla_api.TESLA_AUTH_TOKEN_URL == (
        "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    )
    for path in (
        "lib/tesla_api.py",
        "scripts/tesla_register_partner_domain.py",
        "scripts/tesla_push_telemetry_config.py",
    ):
        assert "auth.tesla.com/oauth2/v3/token" not in Path(path).read_text(
            encoding="utf-8")


def test_refresh_exchange_uses_official_payload_and_rotates_tokens(monkeypatch):
    api = _auth_api()
    calls = []
    persisted = []

    def post(url, *, data, timeout):
        calls.append((url, data, timeout))
        return _Resp(200, {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_in": 3600,
        })

    monkeypatch.setattr(tesla_api.requests, "post", post)
    api._persist_tokens = lambda: persisted.append(
        (api._access_token, api._refresh_token)) or True

    api._refresh_access_token()

    assert calls == [(
        tesla_api.TESLA_AUTH_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": "client-id",
            "refresh_token": "refresh-old",
        },
        tesla_api.TIMEOUT,
    )]
    assert api._access_token == "access-new"
    assert api._refresh_token == "refresh-new"
    assert api._token_expires_at > time.time() + 3500
    assert persisted == [("access-new", "refresh-new")]


def test_refresh_reloads_token_rotated_by_another_process(monkeypatch):
    api = _auth_api()
    calls = []
    monkeypatch.setattr(
        tesla_api, "dotenv_values",
        lambda path: {"TESLA_FLEET_REFRESH_TOKEN": "refresh-from-disk"},
    )

    def post(url, *, data, timeout):
        calls.append(data.copy())
        return _Resp(200, {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_in": 3600,
        })

    monkeypatch.setattr(tesla_api.requests, "post", post)
    api._persist_tokens = lambda: True

    api._refresh_access_token()

    assert calls[0]["refresh_token"] == "refresh-from-disk"


def test_expired_access_token_is_refreshed_once(monkeypatch):
    api = _auth_api()
    api._access_token = "expired"
    api._token_expires_at = time.time() - 1
    calls = []

    def refresh():
        calls.append("refresh")
        api._access_token = "fresh"
        api._token_expires_at = time.time() + 3600

    api._refresh_access_token = refresh

    assert api._get_access_token() == "fresh"
    assert api._get_access_token() == "fresh"
    assert calls == ["refresh"]


def test_saved_access_token_expiry_is_read_from_jwt():
    expiry = int(time.time()) + 3600
    assert tesla_api.TeslaApi._access_token_expiry(_jwt_with_exp(expiry)) == expiry
    assert tesla_api.TeslaApi._access_token_expiry("not-a-jwt") == 0


def test_init_reuses_saved_unexpired_access_token(monkeypatch):
    expiry = int(time.time()) + 3600
    saved_access = _jwt_with_exp(expiry)
    values = {
        "TESLA_FLEET_CLIENT_ID": "client-id",
        "TESLA_FLEET_CLIENT_SECRET": "client-secret",
        "TESLA_FLEET_REFRESH_TOKEN": "refresh-token",
        "TESLA_FLEET_ACCESS_TOKEN": saved_access,
        "TESLA_FLEET_API_BASE_URL": "https://fleet-api.example",
        "TESLA_FLEET_VEHICLE_ID": "VID",
    }

    class NoStartThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(tesla_api, "retrieve_setting", values.get)
    monkeypatch.setattr(tesla_api, "default_budget", lambda: _Budget())
    monkeypatch.setattr(tesla_api.threading, "Thread", NoStartThread)

    api = tesla_api.TeslaApi()

    assert api._access_token == saved_access
    assert api._token_expires_at == expiry
    api._refresh_access_token = lambda: pytest.fail("valid saved token was refreshed")
    assert api._get_access_token() == saved_access


def test_fleet_401_refreshes_token_and_retries_request_once(monkeypatch):
    api = _auth_api()
    api._base_url = "https://fleet-api.example"
    api._access_token = "access-old"
    api._token_expires_at = time.time() + 3600
    requests_seen = []

    def request(method, url, *, headers, timeout, **kwargs):
        requests_seen.append((method, url, headers.copy(), kwargs))
        return _Resp(401 if len(requests_seen) == 1 else 200, {"response": {}})

    def refresh():
        api._access_token = "access-new"
        api._token_expires_at = time.time() + 3600

    monkeypatch.setattr(tesla_api.requests, "request", request)
    api._refresh_access_token = refresh

    response = api._request("GET", "/api/1/vehicles/VID", headers={"X-Test": "yes"})

    assert response.status_code == 200
    assert [item[2]["Authorization"] for item in requests_seen] == [
        "Bearer access-old", "Bearer access-new"]
    assert [item[2]["X-Test"] for item in requests_seen] == ["yes", "yes"]
    assert api._budget.spent == ["data"]


def test_oauth_login_required_is_auth_failure_and_is_backed_off(monkeypatch):
    api = _auth_api()
    responses = []

    def post(*args, **kwargs):
        responses.append(1)
        return _Resp(401, {
            "error": "login_required",
            "error_description": "do not log refresh-old",
        })

    monkeypatch.setattr(tesla_api.requests, "post", post)

    with pytest.raises(tesla_api.TeslaAuthenticationError) as first:
        api._get_access_token()
    with pytest.raises(tesla_api.TeslaAuthenticationError) as second:
        api._get_access_token()

    assert first.value.error_code == second.value.error_code == "login_required"
    assert len(responses) == 1


def test_oauth_network_failure_is_sanitized_and_backed_off(monkeypatch):
    api = _auth_api()
    attempts = []

    def post(*args, **kwargs):
        attempts.append(1)
        raise tesla_api.requests.ConnectionError("host unavailable; refresh-old")

    monkeypatch.setattr(tesla_api.requests, "post", post)

    with pytest.raises(tesla_api.TeslaAuthenticationError) as first:
        api._get_access_token()
    with pytest.raises(tesla_api.TeslaAuthenticationError) as second:
        api._get_access_token()

    assert first.value.error_code == second.value.error_code == "auth_service_unavailable"
    assert "refresh-old" not in str(first.value)
    assert attempts == [1]


def test_command_auth_failure_is_refunded_and_never_logs_token(caplog):
    api = _bare_api(_Budget(allow=True), lambda *a, **k: None)
    api._request = lambda *a, **k: (_ for _ in ()).throw(
        tesla_api.TeslaAuthenticationError(401, "login_required"))

    assert api._command_ex("set_charge_limit", error_msg="set limit") == (
        False, "auth")
    assert api._budget.refunded == ["command"]
    assert "login_required" in caplog.text
    assert "refresh-old" not in caplog.text


def test_billable_fleet_401_is_not_refunded_when_refresh_then_fails(monkeypatch):
    api = _auth_api()
    api._vehicle_id = "VID"
    api._base_url = "https://fleet-api.example"
    api._access_token = "access-old"
    api._token_expires_at = time.time() + 3600
    api._refresh_access_token = lambda: (_ for _ in ()).throw(
        tesla_api.TeslaAuthenticationError(401, "login_required"))
    monkeypatch.setattr(
        tesla_api.requests, "request",
        lambda *a, **k: _Resp(401, {"error": "unauthorized"}),
    )

    assert api._command_ex("set_charge_limit") == (False, "auth")
    assert api._budget.spent == ["command"]
    assert api._budget.refunded == []


def test_rotated_tokens_are_persisted_without_losing_other_secrets(
        monkeypatch, tmp_path):
    path = tmp_path / ".secrets"
    path.write_text(
        'UNRELATED_SECRET="keep-me"\n'
        'TESLA_FLEET_ACCESS_TOKEN="access-old"\n'
        'TESLA_FLEET_REFRESH_TOKEN=refresh-old\n'
        'TESLA_FLEET_REFRESH_TOKEN="duplicate-old"\n',
        encoding="utf-8",
    )
    os.chmod(path, 0o640)
    monkeypatch.setattr(tesla_api, "secrets_path", lambda: str(path))
    monkeypatch.setattr(tesla_api.retrieve_setting, "_secrets", {
        "TESLA_FLEET_ACCESS_TOKEN": "access-old",
        "TESLA_FLEET_REFRESH_TOKEN": "refresh-old",
    }, raising=False)
    monkeypatch.setattr(tesla_api.retrieve_setting, "_secrets_path", str(path),
                        raising=False)
    api = _auth_api()
    api._access_token = "access-new"
    api._refresh_token = "refresh-new"

    assert api._persist_tokens() is True

    content = path.read_text(encoding="utf-8")
    assert 'UNRELATED_SECRET="keep-me"' in content
    assert 'TESLA_FLEET_ACCESS_TOKEN="access-new"' in content
    assert content.count('TESLA_FLEET_REFRESH_TOKEN="refresh-new"') == 2
    assert "refresh-old" not in content and "duplicate-old" not in content
    assert os.stat(path).st_mode & 0o777 == 0o640
    assert tesla_api.retrieve_setting._secrets["TESLA_FLEET_ACCESS_TOKEN"] == "access-new"
    assert tesla_api.retrieve_setting._secrets["TESLA_FLEET_REFRESH_TOKEN"] == "refresh-new"


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
    # 'could_not_wake_buses' can occur while the car IS still charging, so we must NOT assume it
    # means stopped: force a wake and retry. First stop asleep -> wake -> retry ok.
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    seq = [(False, "asleep"), (True, "ok")]
    calls = {"cmd": 0, "wake": 0, "stopped": 0}
    api._command_ex = lambda name, json_body=None, error_msg="", critical=False: (calls.__setitem__("cmd", calls["cmd"] + 1), seq.pop(0))[1]
    api.wake_vehicle = lambda skip_online_check=False, critical=False: (calls.__setitem__("wake", calls["wake"] + 1), True)[1]
    api._on_charge_stopped = lambda: calls.__setitem__("stopped", calls["stopped"] + 1)
    assert api.stop_charge_robust() == "ok"
    assert calls == {"cmd": 2, "wake": 1, "stopped": 1}


def test_start_charge_robust_wakes_and_retries_when_asleep():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    seq = [(False, "asleep"), (True, "ok")]
    calls = {"cmd": 0, "wake": 0, "started": 0}
    api._command_ex = lambda name, json_body=None, error_msg="", critical=False: (calls.__setitem__("cmd", calls["cmd"] + 1), seq.pop(0))[1]
    api.wake_vehicle = lambda skip_online_check=False, critical=False: (calls.__setitem__("wake", calls["wake"] + 1), True)[1]
    api._on_charge_started = lambda: calls.__setitem__("started", calls["started"] + 1)
    assert api.start_charge_robust() == "ok"
    assert calls == {"cmd": 2, "wake": 1, "started": 1}


def test_stop_charge_robust_reports_network_failure():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._command_ex = lambda name, json_body=None, error_msg="", critical=False: (False, "network")
    api.wake_vehicle = lambda skip_online_check=False, critical=False: True
    api._on_charge_stopped = lambda: None
    assert api.stop_charge_robust() == "network"


def test_command_ex_refunds_non_billable_5xx_and_network():
    # Tesla bills only responses < 500. A 5xx or a network error must be REFUNDED so the
    # displayed usage matches the portal, even though we spent up front to enforce the cap.
    import requests as _rq
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._vehicle_id = "V"

    b1 = _Budget(allow=True)
    api._budget = b1
    api._request = lambda *a, **k: _Resp(500, {})
    assert api._command_ex("charge_stop") == (False, "network")
    assert b1.spent == ["command"] and b1.refunded == ["command"]

    b2 = _Budget(allow=True)
    api._budget = b2
    def boom(*a, **k):
        raise _rq.exceptions.ConnectionError("down")
    api._request = boom
    assert api._command_ex("charge_start")[1] == "network"
    assert b2.refunded == ["command"]


def test_command_ex_does_not_refund_billable_4xx():
    # 408 (asleep) and other 4xx ARE billed by Tesla, so no refund.
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._vehicle_id = "V"
    budget = _Budget(allow=True)
    api._budget = budget
    api._request = lambda *a, **k: _Resp(408, {"response": {"result": False, "reason": "could_not_wake_buses"}})
    assert api._command_ex("charge_stop") == (False, "asleep")
    assert budget.refunded == []


def test_command_ex_classifies_asleep_and_network():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._vehicle_id = "V"
    api._budget = _Budget(allow=True)
    api._request = lambda *a, **k: _Resp(200, {"response": {"result": False, "reason": "could_not_wake_buses"}})
    assert api._command_ex("charge_stop") == (False, "asleep")
    api._budget = _Budget(allow=True)
    api._request = lambda *a, **k: _Resp(500, {})
    assert api._command_ex("charge_stop") == (False, "network")


def test_command_ex_always_refunds_and_classifies_5xx_as_network():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api._vehicle_id = "V"
    api._budget = _Budget(allow=True)
    api._request = lambda *a, **k: _Resp(
        503, {"response": {"result": False, "reason": "vehicle unavailable"}}
    )

    assert api._command_ex("add_charge_schedule") == (False, "network")
    assert api._budget.refunded == ["command"]


def test_set_charge_limit_is_budget_gated_without_fabricating_confirmation():
    budget = _Budget(allow=True)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _Resp(200, {"response": {"result": True}})

    api = _bare_api(budget, request)
    api.vehicle_soc_setpoint = 50

    assert api.set_tesla_charge_limit(80) == (True, "ok")
    assert calls == [("POST", "/api/1/vehicles/VID/command/set_charge_limit",
                      {"json": {"percent": 80}})]
    assert budget.spent == ["command"]
    assert api.vehicle_soc_setpoint == 50


def test_set_charge_limit_treats_already_set_as_idempotent_success():
    budget = _Budget(allow=True)
    api = _bare_api(
        budget,
        lambda *a, **k: _Resp(
            200, {"response": {"result": False, "reason": "already_set"}}
        ),
    )
    api.vehicle_soc_setpoint = 80

    assert api.set_tesla_charge_limit(80) == (True, "ok")
    assert budget.spent == ["command"]


def test_set_charge_limit_rejects_invalid_value_without_spending():
    budget = _Budget(allow=True)
    calls = []
    api = _bare_api(budget, lambda *a, **k: calls.append((a, k)))

    assert api.set_tesla_charge_limit(49) == (False, "invalid")
    assert api.set_tesla_charge_limit(101) == (False, "invalid")
    assert api.set_tesla_charge_limit(80.5) == (False, "invalid")
    assert calls == []
    assert budget.spent == []


def test_bounded_integer_preserves_large_uint64_without_float_rounding():
    schedule_id = 0x434552424F455601

    assert tesla_api.TeslaApi._bounded_integer(
        schedule_id, 1, (2 ** 64) - 1) == schedule_id
    assert tesla_api.TeslaApi._bounded_integer(
        str(schedule_id), 1, (2 ** 64) - 1) == schedule_id


def test_upsert_owned_charge_schedule_sends_complete_direct_fleet_payload():
    budget = _Budget(allow=True)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _Resp(200, {"response": {"result": True}})

    api = _bare_api(budget, request)

    result = api.upsert_owned_charge_schedule(
        1_725_000_123,
        start_time=315,
        end_time=600,
        days_of_week=4,
        latitude=52.123,
        longitude=5.456,
        one_time=True,
    )

    assert result == (True, "ok")
    assert calls == [(
        "POST",
        "/api/1/vehicles/VID/command/add_charge_schedule",
        {"json": {
            "id": 1_725_000_123,
            "days_of_week": "TUES",
            "start_enabled": True,
            "start_time": 315,
            "end_enabled": True,
            "end_time": 600,
            "one_time": True,
            "enabled": True,
            "lat": 52.123,
            "lon": 5.456,
        }},
    )]
    assert budget.spent == ["command"]


def test_upsert_owned_charge_schedule_supports_start_only_fallback():
    calls = []
    api = _bare_api(
        _Budget(allow=True),
        lambda method, path, **kwargs: (
            calls.append(kwargs["json"])
            or _Resp(200, {"response": {"result": True}})
        ),
    )

    assert api.upsert_owned_charge_schedule(
        1_725_000_123,
        start_time=315,
        end_time=None,
        days_of_week=4,
        latitude=52.123,
        longitude=5.456,
    ) == (True, "ok")
    assert calls[0]["end_enabled"] is False
    assert calls[0]["end_time"] == 0


def test_owned_charge_schedule_renders_weekday_bitmap_for_fleet_wire_format():
    calls = []
    api = _bare_api(
        _Budget(allow=True),
        lambda method, path, **kwargs: (
            calls.append(kwargs["json"])
            or _Resp(200, {"response": {"result": True}})
        ),
    )

    assert api.upsert_owned_charge_schedule(
        1_784_592_000,
        start_time=315,
        end_time=600,
        days_of_week=127,
        latitude=52.123,
        longitude=5.456,
    ) == (True, "ok")
    assert calls[0]["days_of_week"] == "SUN,MON,TUES,WED,THURS,FRI,SAT"


def test_owned_charge_schedule_validation_never_spends_or_sends():
    budget = _Budget(allow=True)
    calls = []
    api = _bare_api(budget, lambda *a, **k: calls.append((a, k)))

    common = dict(
        start_time=315,
        end_time=600,
        days_of_week=4,
        latitude=52.123,
        longitude=5.456,
    )
    assert api.upsert_owned_charge_schedule(0, **common) == (False, "invalid")
    assert api.upsert_owned_charge_schedule(123, **{**common, "start_time": 1440}) == (False, "invalid")
    assert api.upsert_owned_charge_schedule(123, **{**common, "days_of_week": 0}) == (False, "invalid")
    assert api.upsert_owned_charge_schedule(123, **{**common, "latitude": 91}) == (False, "invalid")
    assert calls == []
    assert budget.spent == []


def test_remove_owned_charge_schedule_targets_only_exact_id_without_read():
    budget = _Budget(allow=True)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _Resp(200, {"response": {"result": True}})

    api = _bare_api(budget, request)

    assert api.remove_owned_charge_schedule(1_725_000_123) == (True, "ok")
    assert calls == [(
        "POST",
        "/api/1/vehicles/VID/command/remove_charge_schedule",
        {"json": {"id": 1_725_000_123}},
    )]
    assert budget.spent == ["command"]


def test_remove_owned_charge_schedule_is_idempotent_when_already_absent():
    api = _bare_api(
        _Budget(allow=True),
        lambda *a, **k: _Resp(
            200, {"response": {"result": False, "reason": "schedule_not_found"}}
        ),
    )

    assert api.remove_owned_charge_schedule(1_725_000_123) == (
        True, "schedule_not_found")


def test_charge_schedule_command_surfaces_unsupported_capability():
    api = _bare_api(
        _Budget(allow=True),
        lambda *a, **k: _Resp(
            200, {"response": {"result": False, "reason": "not_supported"}}
        ),
    )

    assert api.remove_owned_charge_schedule(123) == (False, "unsupported")


def test_smart_charge_command_retries_once_only_after_asleep_response():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    sequence = [(False, "asleep"), (True, "ok")]
    calls = {"command": 0, "wake": 0}

    def command(*args, **kwargs):
        calls["command"] += 1
        return sequence.pop(0)

    api._command_ex = command
    api.wake_vehicle = lambda skip_online_check=False: (
        calls.__setitem__("wake", calls["wake"] + 1) or True
    )

    assert api.set_tesla_charge_limit(80) == (True, "ok")
    assert calls == {"command": 2, "wake": 1}


def test_smart_charge_command_does_not_wake_for_generic_vehicle_error():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    calls = {"command": 0, "wake": 0}

    def command(*args, **kwargs):
        calls["command"] += 1
        return False, "error"

    api._command_ex = command
    api.wake_vehicle = lambda skip_online_check=False: (
        calls.__setitem__("wake", calls["wake"] + 1) or True
    )

    assert api.upsert_owned_charge_schedule(
        1_784_592_000,
        start_time=315,
        end_time=600,
        days_of_week=4,
        latitude=52.123,
        longitude=5.456,
    ) == (False, "error")
    assert calls == {"command": 1, "wake": 0}


def test_sub_five_amp_workaround_always_sends_twice_and_reports_real_result():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    outcomes = [False, True]
    calls = []
    api.set_charge = lambda amps, error: calls.append(amps) or outcomes.pop(0)

    assert api.set_tesla_charge_amps(3) is True
    assert calls == [3, 3]


def test_sub_five_amp_workaround_reports_failure_when_both_commands_fail():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    calls = []
    api.set_charge = lambda amps, error: calls.append(amps) or False

    assert api.set_tesla_charge_amps(3) is False
    assert calls == [3, 3]


def test_five_amp_request_is_sent_once():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    calls = []
    api.set_charge = lambda amps, error: calls.append(amps) or True

    assert api.set_tesla_charge_amps(5) is True
    assert calls == [5]


def test_charge_amp_ceiling_accepts_explicit_smart_evse_limit():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    calls = []
    api.set_charge = lambda amps, error: calls.append(amps) or True

    assert api.set_tesla_charge_amps(30, installation_ceiling=24) is True
    assert calls == [24]


def test_charge_amp_ceiling_preserves_legacy_18a_default():
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    calls = []
    api.set_charge = lambda amps, error: calls.append(amps) or True

    assert api.set_tesla_charge_amps(30) is True
    assert calls == [18]


def test_update_vehicle_status_reads_telemetry_and_skips_rest(monkeypatch):
    # In telemetry (push) mode we must read the bridge-maintained STATE keys and NEVER make a
    # billable vehicle_data call.
    monkeypatch.setattr(tesla_api, "retrieve_setting",
                        lambda name: "true" if name == "TESLA_TELEMETRY_ENABLED" else None)
    store = {"tesla_soc": "62", "tesla_soc_setpoint": "80", "tesla_is_charging": "True",
             "tesla_is_plugged": "True", "tesla_is_home": "True",
             "tesla_charge_current_request": "12", "tesla_time_to_full": "1 hr 5 min"}
    monkeypatch.setattr(tesla_api, "STATE",
                        type("S", (), {"get": staticmethod(lambda k: store.get(k))})())
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    calls = {"n": 0}
    api.get_vehicle_data = lambda allow_wake=False: calls.__setitem__("n", calls["n"] + 1)

    api.update_vehicle_status()
    assert calls["n"] == 0                         # never hit the billable REST path
    assert api.vehicle_soc == 62.0
    assert api.is_plugged is True and api.is_charging is True and api.is_home is True
    assert api.is_full is False                    # 62 < 80
    assert api.charging_amp_limit == 12.0
    assert api.time_until_full == "1 hr 5 min"


def test_telemetry_refresh_never_exposes_eta_when_not_charging(monkeypatch):
    monkeypatch.setattr(
        tesla_api, "retrieve_setting",
        lambda name: "true" if name == "TESLA_TELEMETRY_ENABLED" else None,
    )
    store = {
        "tesla_is_charging": "False",
        "tesla_is_plugged": "True",
        "tesla_time_to_full": "4 hr 12 min",
        "tesla_charge_state_updated_at": "1234.5",
    }
    published = []

    class State:
        @staticmethod
        def get(key):
            return store.get(key)

        @staticmethod
        def set(key, value):
            store[key] = value

    monkeypatch.setattr(tesla_api, "STATE", State())
    monkeypatch.setattr(
        tesla_api,
        "publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)

    api.update_vehicle_status()

    assert api.is_charging is False
    assert api.charging_status == "Idle"
    assert api.time_until_full == "N/A"
    assert store["tesla_time_to_full"] == "N/A"
    assert published == [(
        "Tesla/vehicle0/time_until_full",
        {"payload": '{"value": "N/A"}', "qos": 0, "retain": True},
    )]


def test_rest_not_charging_transition_clears_previous_eta(monkeypatch):
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.time_until_full = "2 hr 30 min"
    api.update_mqtt_and_domoticz = lambda: None

    charging = api.is_vehicle_charging({
        "charge_state": {"charging_state": "Stopped"},
    })

    assert charging is False
    assert api.charging_status == "Idle"
    assert api.time_until_full == "N/A"


def test_confirmed_stop_refreshes_retained_charge_state_in_telemetry_mode(
        monkeypatch):
    monkeypatch.setattr(
        tesla_api, "retrieve_setting",
        lambda name: "true" if name == "TESLA_TELEMETRY_ENABLED" else None,
    )
    state_updates = []
    published = []
    monkeypatch.setattr(
        tesla_api, "STATE",
        type("S", (), {"set": staticmethod(
            lambda key, value: state_updates.append((key, value)))})(),
    )
    monkeypatch.setattr(
        tesla_api, "publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.is_charging = True
    api.time_until_full = "1 hr"
    api.charging_status = "Charging"
    api.charging_amp_limit = 16
    api._domoticz_vehicle_status = lambda: None

    api._on_charge_stopped()

    assert ("tesla_is_charging", "False") in state_updates
    assert ("tesla_time_to_full", "N/A") in state_updates
    assert any(topic == "Tesla/vehicle0/is_charging"
               and kwargs.get("retain") is True
               and '"False"' in kwargs.get("payload", "")
               for topic, kwargs in published)
    assert any(topic == "Tesla/vehicle0/charging_status"
               and '"Idle"' in kwargs.get("payload", "")
               for topic, kwargs in published)


def test_wake_vehicle_skips_billable_read_when_telemetry_is_fresh(monkeypatch):
    # audit M3: a recently-refreshed telemetry stream is proof enough the car is online, so
    # wake_vehicle() must not spend a billable state read to confirm it.
    import time as _time
    monkeypatch.setattr(tesla_api, "retrieve_setting",
                        lambda name: "true" if name == "TESLA_TELEMETRY_ENABLED" else None)
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.last_update_ts = _time.time()
    reads = {"n": 0}
    api._get_vehicle_state = lambda: (reads.__setitem__("n", reads["n"] + 1), "online")[1]
    assert api.wake_vehicle() is True
    assert reads["n"] == 0                        # no billable pre-command state read


def test_wake_vehicle_falls_back_to_real_check_when_telemetry_is_stale(monkeypatch):
    # If the telemetry refresh itself is stale (bridge/MQTT dropped), M3's shortcut must NOT
    # apply — fall back to the real (billable) online check rather than assuming online.
    import time as _time
    monkeypatch.setattr(tesla_api, "retrieve_setting",
                        lambda name: "true" if name == "TESLA_TELEMETRY_ENABLED" else None)
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.last_update_ts = _time.time() - (tesla_api.TELEMETRY_ONLINE_MAX_AGE_S + 60)
    reads = {"n": 0}
    api._get_vehicle_state = lambda: (reads.__setitem__("n", reads["n"] + 1), "online")[1]
    assert api.wake_vehicle() is True
    assert reads["n"] == 1                         # fell back to the real check


def test_wake_vehicle_uses_real_check_in_polling_mode(monkeypatch):
    # Legacy REST polling mode must be unaffected: always use the real online check.
    import time as _time
    monkeypatch.setattr(tesla_api, "retrieve_setting", lambda name: None)
    api = tesla_api.TeslaApi.__new__(tesla_api.TeslaApi)
    api.last_update_ts = _time.time()
    reads = {"n": 0}
    api._get_vehicle_state = lambda: (reads.__setitem__("n", reads["n"] + 1), "online")[1]
    assert api.wake_vehicle() is True
    assert reads["n"] == 1


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
