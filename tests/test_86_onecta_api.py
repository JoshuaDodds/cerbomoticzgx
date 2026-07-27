import json
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from lib import onecta_api as api


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttp:
    def __init__(self, *, posts=None, gets=None, patches=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.patches = list(patches or [])
        self.post_calls = []
        self.get_calls = []
        self.patch_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        result = self.posts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        result = self.gets.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def patch(self, url, **kwargs):
        self.patch_calls.append((url, kwargs))
        result = self.patches.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _settings(monkeypatch, values):
    monkeypatch.setattr(
        api,
        "retrieve_setting",
        lambda key: values.get(key),
    )


def test_authorize_builds_scoped_url_and_private_pending_state(tmp_path, monkeypatch):
    _settings(
        monkeypatch,
        {
            "ONECTA_CLIENT_ID": "client-id",
            "ONECTA_REDIRECT_URI": "https://ess.example/onecta/oauth/callback",
        },
    )
    state_path = tmp_path / "state.json"

    url = api.create_authorization_url(state_path, now=1234)

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    pending = json.loads(state_path.read_text())
    assert parsed.scheme == "https"
    assert parsed.netloc == "idp.onecta.daikineurope.com"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://ess.example/onecta/oauth/callback"]
    assert query["scope"] == [api.OAUTH_SCOPE]
    assert query["state"] == [pending["state"]]
    assert pending["created_at"] == 1234
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_parse_callback_requires_exact_redirect_and_state():
    pending = {
        "redirect_uri": "https://ess.example/onecta/oauth/callback",
        "state": "expected-state",
    }
    callback = (
        "https://ess.example/onecta/oauth/callback"
        "?code=one-time-code&state=expected-state"
    )

    assert api.parse_callback_url(callback, pending) == "one-time-code"

    with pytest.raises(api.OnectaError, match="state did not match"):
        api.parse_callback_url(callback.replace("expected-state", "wrong"), pending)
    with pytest.raises(api.OnectaError, match="does not match"):
        api.parse_callback_url(callback.replace("ess.example", "evil.example"), pending)
    with pytest.raises(api.OnectaError, match="was rejected"):
        api.parse_callback_url(
            "https://ess.example/onecta/oauth/callback"
            "?error=access_denied&state=expected-state",
            pending,
        )


def test_exchange_rejects_expired_state_without_network_call(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "state": "state",
                "redirect_uri": "https://ess.example/callback",
                "created_at": 10,
            }
        )
    )
    http = FakeHttp()

    with pytest.raises(api.OnectaError, match="expired"):
        api.exchange_callback(
            "https://ess.example/callback?code=code&state=state",
            state_path,
            now=10 + api.OAUTH_STATE_MAX_AGE_SECONDS + 1,
            http=http,
        )

    assert http.post_calls == []


def test_exchange_persists_tokens_without_losing_other_secrets(tmp_path, monkeypatch):
    secret_file = tmp_path / ".secrets"
    secret_file.write_text('UNCHANGED="keep-me"\nONECTA_ACCESS_TOKEN="old"\n')
    secret_file.chmod(0o600)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "state": "expected",
                "redirect_uri": "https://ess.example/callback",
                "created_at": 100,
            }
        )
    )
    _settings(
        monkeypatch,
        {
            "ONECTA_CLIENT_ID": "client-id",
            "ONECTA_CLIENT_SECRET": "client-secret",
        },
    )
    http = FakeHttp(
        posts=[
            FakeResponse(
                payload={
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                }
            )
        ]
    )

    api.exchange_callback(
        "https://ess.example/callback?code=one-time&state=expected",
        state_path,
        secret_file=secret_file,
        now=101,
        http=http,
    )

    stored = secret_file.read_text()
    assert 'UNCHANGED="keep-me"' in stored
    assert 'ONECTA_ACCESS_TOKEN="access-new"' in stored
    assert 'ONECTA_REFRESH_TOKEN="refresh-new"' in stored
    assert "client-secret" not in stored
    assert not state_path.exists()
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    form = http.post_calls[0][1]["data"]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "one-time"
    assert form["client_secret"] == "client-secret"


def test_exchange_requires_refresh_token_for_durable_auth(tmp_path, monkeypatch):
    secret_file = tmp_path / ".secrets"
    secret_file.write_text('UNCHANGED="keep-me"\n')
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "state": "expected",
                "redirect_uri": "https://ess.example/callback",
                "created_at": 100,
            }
        )
    )
    _settings(
        monkeypatch,
        {
            "ONECTA_CLIENT_ID": "client-id",
            "ONECTA_CLIENT_SECRET": "client-secret",
        },
    )
    http = FakeHttp(posts=[FakeResponse(payload={"access_token": "access-only"})])

    with pytest.raises(api.OnectaError, match="no refresh token"):
        api.exchange_callback(
            "https://ess.example/callback?code=code&state=expected",
            state_path,
            secret_file=secret_file,
            now=101,
            http=http,
        )

    assert secret_file.read_text() == 'UNCHANGED="keep-me"\n'
    assert state_path.exists()


def test_oauth_failure_is_sanitized_and_never_includes_response_description(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "state": "expected",
                "redirect_uri": "https://ess.example/callback",
                "created_at": 100,
            }
        )
    )
    _settings(
        monkeypatch,
        {
            "ONECTA_CLIENT_ID": "client-id",
            "ONECTA_CLIENT_SECRET": "client-secret",
        },
    )
    http = FakeHttp(
        posts=[
            FakeResponse(
                status_code=401,
                payload={
                    "error": "invalid_grant",
                    "error_description": "contains-sensitive-details",
                },
            )
        ]
    )

    with pytest.raises(api.OnectaError) as raised:
        api.exchange_callback(
            "https://ess.example/callback?code=code&state=expected",
            state_path,
            now=101,
            http=http,
        )

    message = str(raised.value)
    assert "invalid_grant" in message
    assert "contains-sensitive-details" not in message
    assert "client-secret" not in message


def test_discovery_refreshes_once_on_401_and_reports_rate_limit(tmp_path, monkeypatch):
    secret_file = tmp_path / ".secrets"
    secret_file.write_text('UNCHANGED="yes"\n')
    secret_file.chmod(0o600)
    settings = {
        "ONECTA_ACCESS_TOKEN": "access-old",
        "ONECTA_REFRESH_TOKEN": "refresh-old",
        "ONECTA_CLIENT_ID": "client-id",
        "ONECTA_CLIENT_SECRET": "client-secret",
        "ONECTA_REDIRECT_URI": "https://ess.example/callback",
    }
    _settings(monkeypatch, settings)
    monkeypatch.setattr(
        api,
        "persist_secret_values",
        lambda values, path=None: settings.update(values),
    )
    http = FakeHttp(
        posts=[
            FakeResponse(
                payload={
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                }
            )
        ],
        gets=[
            FakeResponse(status_code=401, payload={"error": "expired"}),
            FakeResponse(
                payload=[{"id": "device-id", "managementPoints": []}],
                headers={
                    "X-RateLimit-Limit-day": "200",
                    "X-RateLimit-Remaining-day": "199",
                },
            ),
        ],
    )

    devices, limits = api.discover_gateway_devices(
        secret_file=secret_file,
        http=http,
    )

    assert len(devices) == 1
    assert limits == {"limit_day": 200, "remaining_day": 199}
    assert len(http.get_calls) == 2
    assert http.get_calls[0][1]["headers"]["Authorization"] == "Bearer access-old"
    assert http.get_calls[1][1]["headers"]["Authorization"] == "Bearer access-new"
    assert http.post_calls[0][1]["data"]["grant_type"] == "refresh_token"
    assert settings["ONECTA_REFRESH_TOKEN"] == "refresh-new"


def test_discovery_network_error_is_sanitized(monkeypatch):
    _settings(monkeypatch, {"ONECTA_ACCESS_TOKEN": "access"})
    http = FakeHttp(gets=[requests.ConnectionError("private network detail")])

    with pytest.raises(api.OnectaError) as raised:
        api.discover_gateway_devices(http=http)

    assert str(raised.value) == "ONECTA device service is unavailable"
    assert "private network detail" not in str(raised.value)


def test_patch_characteristic_uses_official_path_body_and_rate_headers(monkeypatch):
    _settings(monkeypatch, {"ONECTA_ACCESS_TOKEN": "access"})
    http = FakeHttp(
        patches=[
            FakeResponse(
                status_code=204,
                headers={"X-RateLimit-Remaining-day": "151"},
            )
        ]
    )

    limits = api.patch_characteristic(
        "private-gateway",
        "climateControl",
        "temperatureControl",
        23.5,
        path="/operationModes/cooling/setpoints/roomTemperature",
        http=http,
    )

    url, options = http.patch_calls[0]
    assert url == (
        api.API_GATEWAY_DEVICES_URL
        + "/private-gateway/management-points/climateControl"
        + "/characteristics/temperatureControl"
    )
    assert options["json"] == {
        "value": 23.5,
        "path": "/operationModes/cooling/setpoints/roomTemperature",
    }
    assert options["headers"]["Authorization"] == "Bearer access"
    assert limits == {"remaining_day": 151}


def test_patch_characteristic_refreshes_once_and_sanitizes_failures(monkeypatch):
    settings = {
        "ONECTA_ACCESS_TOKEN": "old",
        "ONECTA_REFRESH_TOKEN": "refresh",
        "ONECTA_CLIENT_ID": "client",
        "ONECTA_CLIENT_SECRET": "secret",
        "ONECTA_REDIRECT_URI": "https://ess.example/callback",
    }
    _settings(monkeypatch, settings)
    monkeypatch.setattr(
        api,
        "persist_secret_values",
        lambda values, path=None: settings.update(values),
    )
    http = FakeHttp(
        posts=[FakeResponse(payload={"access_token": "new"})],
        patches=[
            FakeResponse(status_code=401, payload={"error": "expired"}),
            FakeResponse(status_code=204),
        ],
    )

    api.patch_characteristic(
        "private-gateway",
        "climateControl",
        "onOffMode",
        "on",
        http=http,
    )

    assert len(http.patch_calls) == 2
    assert http.patch_calls[0][1]["headers"]["Authorization"] == "Bearer old"
    assert http.patch_calls[1][1]["headers"]["Authorization"] == "Bearer new"

    failing = FakeHttp(
        patches=[
            FakeResponse(
                status_code=422,
                payload={
                    "error": "invalid_value",
                    "detail": "private-gateway",
                },
            )
        ]
    )
    with pytest.raises(api.OnectaError) as raised:
        api.patch_characteristic(
            "private-gateway",
            "climateControl",
            "onOffMode",
            "on",
            http=failing,
        )
    assert "invalid_value" in str(raised.value)
    assert "private-gateway" not in str(raised.value)


def test_capability_report_retains_energy_data_but_redacts_identifiers():
    devices = [
        {
            "id": "13995b32-fc6e-43ed-918e-5d2b01095ccb",
            "type": "dx23",
            "deviceModel": "dx23",
            "isCloudConnectionUp": {"value": True},
            "managementPoints": [
                {
                    "embeddedId": "gateway",
                    "managementPointType": "gateway",
                    "name": {"value": "Private room name"},
                    "ipAddress": {"value": "192.168.1.2"},
                    "macAddress": {"value": "AA:BB:CC:DD:EE:FF"},
                    "modelInfo": {"value": "BRP069A4x", "settable": False},
                },
                {
                    "embeddedId": "climateControl",
                    "managementPointType": "climateControl",
                    "managementPointSubType": "mainZone",
                    "name": {"value": "Bedroom"},
                    "onOffMode": {"value": "on", "settable": True},
                    "sensoryData": {
                        "value": {
                            "roomTemperature": {"value": 22.5},
                            "outdoorTemperature": {"value": 28},
                        }
                    },
                    "consumptionData": {
                        "value": {
                            "electrical": {
                                "unit": "kWh",
                                "cooling": {"d": [0.0, 0.2, 0.4]},
                            }
                        }
                    },
                    "schedule": {"value": {"secret-location": "Bedroom"}},
                },
            ],
        }
    ]

    report = api.build_capability_report(devices, {"remaining_day": 199})
    serialized = json.dumps(report)

    assert report["device_count"] == 1
    assert report["devices"][0]["unit"].startswith("unit-1-")
    assert report["devices"][0]["cloud_connected"] is True
    point = report["devices"][0]["management_points"][1]
    assert point["friendly_name_present"] is True
    assert point["forecast_relevant"]["onOffMode"]["value"] == "on"
    assert point["forecast_relevant"]["consumptionData"]["value"]["electrical"][
        "cooling"
    ]["d"] == [0.0, 0.2, 0.4]
    assert "schedule" in point["capability_names"]
    assert "schedule" not in point["forecast_relevant"]
    assert "Bedroom" not in serialized
    assert "Private room name" not in serialized
    assert "192.168.1.2" not in serialized
    assert "AA:BB:CC:DD:EE:FF" not in serialized
    assert "13995b32-fc6e-43ed-918e-5d2b01095ccb" not in serialized


def test_capability_report_is_written_owner_only(tmp_path):
    report_path = tmp_path / "report.json"
    api.write_capability_report({"device_count": 0}, report_path)

    assert json.loads(report_path.read_text()) == {"device_count": 0}
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
