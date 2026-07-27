"""Small OAuth, discovery and guarded-control client for Daikin ONECTA.

Discovery remains the normal background operation. Characteristic PATCH support
is deliberately low-level: callers must validate unit capabilities and values
before invoking it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import dotenv_values

from lib.config_paths import secrets_path
from lib.config_retrieval import retrieve_setting


OAUTH_AUTHORIZE_URL = "https://idp.onecta.daikineurope.com/v1/oidc/authorize"
OAUTH_TOKEN_URL = "https://idp.onecta.daikineurope.com/v1/oidc/token"
API_GATEWAY_DEVICES_URL = "https://api.onecta.daikineurope.com/v1/gateway-devices"
OAUTH_SCOPE = "openid onecta:basic.integration offline_access"
DEFAULT_STATE_PATH = Path("data/onecta_oauth_state.json")
DEFAULT_REPORT_PATH = Path("data/onecta_capabilities.json")
HTTP_TIMEOUT_SECONDS = 20
OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60

_IDENTITY_KEYS = {
    "_id",
    "id",
    "embeddedId",
    "ipAddress",
    "macAddress",
    "name",
    "serialNumber",
    "sgtin",
    "ssid",
    "wifiConnectionSSID",
}
_MANAGEMENT_POINT_METADATA = {
    "embeddedId",
    "managementPointType",
    "managementPointSubType",
    "managementPointCategory",
    "name",
}
_FORECAST_RELEVANT_CAPABILITIES = {
    "consumptionData",
    "errorCode",
    "fanControl",
    "firmwareVersion",
    "isFirmwareUpdateSupported",
    "isHolidayModeActive",
    "isInErrorState",
    "isInWarningState",
    "isPowerfulModeActive",
    "modelInfo",
    "onOffMode",
    "operationMode",
    "powerfulMode",
    "sensoryData",
    "temperatureControl",
}
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class OnectaError(RuntimeError):
    """Sanitized ONECTA failure safe for logs and terminal output."""


def _required_setting(name: str) -> str:
    value = retrieve_setting(name)
    if value is None or not str(value).strip():
        raise OnectaError(f"missing required secret {name}")
    return str(value).strip()


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Write text durably with replace semantics and restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _dotenv_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def persist_secret_values(values: Mapping[str, str], path: str | Path | None = None) -> None:
    """Atomically update selected values while preserving every other secret."""
    target = Path(path or secrets_path())
    try:
        content = target.read_text(encoding="utf-8")
        original_mode = stat.S_IMODE(target.stat().st_mode)
    except OSError as error:
        raise OnectaError(f"could not read secrets file {target}") from error

    for key, value in values.items():
        pattern = re.compile(
            rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*).*$",
            re.MULTILINE,
        )
        replacement = lambda match, item=str(value): (
            f"{match.group('prefix')}{_dotenv_quote(item)}"
        )
        content, count = pattern.subn(replacement, content)
        if not count:
            separator = "" if not content or content.endswith("\n") else "\n"
            content = f"{content}{separator}{key}={_dotenv_quote(str(value))}\n"

    _atomic_write(target, content, mode=original_mode)

    cached = getattr(retrieve_setting, "_secrets", None)
    cached_path = getattr(retrieve_setting, "_secrets_path", None)
    if isinstance(cached, dict) and cached_path == str(target):
        cached.update(values)


def create_authorization_url(
    state_path: str | Path = DEFAULT_STATE_PATH,
    *,
    now: float | None = None,
) -> str:
    """Create an OAuth authorization URL and persist the short-lived state."""
    client_id = _required_setting("ONECTA_CLIENT_ID")
    redirect_uri = _required_setting("ONECTA_REDIRECT_URI")
    state = secrets.token_urlsafe(32)
    pending = {
        "state": state,
        "redirect_uri": redirect_uri,
        "created_at": float(time.time() if now is None else now),
    }
    _atomic_write(
        Path(state_path),
        json.dumps(pending, separators=(",", ":")) + "\n",
        mode=0o600,
    )
    return OAUTH_AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPE,
            "state": state,
        }
    )


def _load_pending_state(
    state_path: str | Path,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    try:
        pending = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OnectaError(
            "no valid pending authorization; run the authorize step again"
        ) from error
    if not isinstance(pending, dict):
        raise OnectaError("pending authorization state is invalid")
    created_at = pending.get("created_at")
    if not isinstance(created_at, (int, float)):
        raise OnectaError("pending authorization state has no valid timestamp")
    current_time = time.time() if now is None else now
    if current_time - float(created_at) > OAUTH_STATE_MAX_AGE_SECONDS:
        raise OnectaError("pending authorization expired; run the authorize step again")
    return pending


def parse_callback_url(
    callback_url: str,
    pending: Mapping[str, Any],
) -> str:
    """Validate redirect destination/state and return its one-time code."""
    callback = urlparse(callback_url.strip())
    expected = urlparse(str(pending.get("redirect_uri") or ""))
    if (callback.scheme, callback.netloc, callback.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise OnectaError("callback URL does not match the registered redirect URI")

    query = parse_qs(callback.query)
    if query.get("error"):
        error_code = re.sub(r"[^a-zA-Z0-9_-]", "_", query["error"][0])[:80]
        raise OnectaError(f"authorization was rejected ({error_code})")
    returned_state = (query.get("state") or [""])[0]
    expected_state = str(pending.get("state") or "")
    if not returned_state or not hmac.compare_digest(returned_state, expected_state):
        raise OnectaError("callback state did not match; authorization was not accepted")
    code = (query.get("code") or [""])[0]
    if not code:
        raise OnectaError("callback URL contains no authorization code")
    return code


def _response_json(response: Any, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise OnectaError(f"{context} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise OnectaError(f"{context} returned an unexpected response")
    return payload


def _oauth_error(response: Any, context: str) -> OnectaError:
    code = "request_failed"
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            code = re.sub(
                r"[^a-zA-Z0-9_-]", "_", str(payload["error"])
            )[:80]
    except (TypeError, ValueError):
        pass
    status = int(getattr(response, "status_code", 0) or 0)
    return OnectaError(f"{context} failed (HTTP {status}, {code})")


def _request_token(
    form: Mapping[str, str],
    *,
    http: Any = requests,
) -> dict[str, Any]:
    try:
        response = http.post(
            OAUTH_TOKEN_URL,
            data=dict(form),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise OnectaError("ONECTA authentication service is unavailable") from error
    if int(getattr(response, "status_code", 0) or 0) != 200:
        raise _oauth_error(response, "ONECTA token request")
    token = _response_json(response, "ONECTA token request")
    if not token.get("access_token"):
        raise OnectaError("ONECTA token response contains no access token")
    return token


def exchange_callback(
    callback_url: str,
    state_path: str | Path = DEFAULT_STATE_PATH,
    *,
    secret_file: str | Path | None = None,
    now: float | None = None,
    http: Any = requests,
) -> None:
    """Exchange a validated callback and durably store its token pair."""
    pending = _load_pending_state(state_path, now=now)
    code = parse_callback_url(callback_url, pending)
    token = _request_token(
        {
            "grant_type": "authorization_code",
            "client_id": _required_setting("ONECTA_CLIENT_ID"),
            "client_secret": _required_setting("ONECTA_CLIENT_SECRET"),
            "redirect_uri": str(pending["redirect_uri"]),
            "code": code,
        },
        http=http,
    )
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise OnectaError(
            "ONECTA returned no refresh token; durable authorization was not established"
        )
    persist_secret_values(
        {
            "ONECTA_ACCESS_TOKEN": str(token["access_token"]),
            "ONECTA_REFRESH_TOKEN": str(refresh_token),
        },
        path=secret_file,
    )
    Path(state_path).unlink(missing_ok=True)


def refresh_access_token(
    *,
    secret_file: str | Path | None = None,
    http: Any = requests,
) -> str:
    """Refresh and atomically persist the latest rotating token pair."""
    token = _request_token(
        {
            "grant_type": "refresh_token",
            "client_id": _required_setting("ONECTA_CLIENT_ID"),
            "client_secret": _required_setting("ONECTA_CLIENT_SECRET"),
            "redirect_uri": _required_setting("ONECTA_REDIRECT_URI"),
            "refresh_token": _required_setting("ONECTA_REFRESH_TOKEN"),
        },
        http=http,
    )
    values = {"ONECTA_ACCESS_TOKEN": str(token["access_token"])}
    if token.get("refresh_token"):
        values["ONECTA_REFRESH_TOKEN"] = str(token["refresh_token"])
    persist_secret_values(values, path=secret_file)
    return values["ONECTA_ACCESS_TOKEN"]


def _rate_limit_headers(response: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for header, key in (
        ("X-RateLimit-Limit-minute", "limit_minute"),
        ("X-RateLimit-Limit-day", "limit_day"),
        ("X-RateLimit-Remaining-minute", "remaining_minute"),
        ("X-RateLimit-Remaining-day", "remaining_day"),
        ("retry-after", "retry_after"),
        ("ratelimit-reset", "reset"),
    ):
        value = getattr(response, "headers", {}).get(header)
        if value is None:
            continue
        try:
            result[key] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def discover_gateway_devices(
    *,
    secret_file: str | Path | None = None,
    http: Any = requests,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Retrieve all gateway devices, refreshing once on authentication failure."""
    access_token = _required_setting("ONECTA_ACCESS_TOKEN")
    response = None
    for attempt in range(2):
        try:
            response = http.get(
                API_GATEWAY_DEVICES_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise OnectaError("ONECTA device service is unavailable") from error
        if response.status_code != 401 or attempt:
            break
        access_token = refresh_access_token(secret_file=secret_file, http=http)

    assert response is not None
    limits = _rate_limit_headers(response)
    if response.status_code == 429:
        raise OnectaError(
            "ONECTA rate limit reached"
            + (
                f"; retry after {limits['retry_after']} seconds"
                if limits.get("retry_after")
                else ""
            )
        )
    if response.status_code != 200:
        raise _oauth_error(response, "ONECTA gateway-device request")
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise OnectaError("ONECTA gateway-device request returned invalid JSON") from error
    if not isinstance(payload, list) or not all(
        isinstance(device, dict) for device in payload
    ):
        raise OnectaError("ONECTA gateway-device response has an unexpected shape")
    return payload, limits


def patch_characteristic(
    gateway_device_id: str,
    embedded_id: str,
    characteristic: str,
    value: Any,
    *,
    path: str | None = None,
    secret_file: str | Path | None = None,
    http: Any = requests,
) -> dict[str, int]:
    """PATCH one previously validated characteristic and return rate limits.

    Raw identifiers never appear in errors or logs. A single 401 refresh is
    allowed, matching discovery; all other failures are sanitized.
    """
    identifiers = (gateway_device_id, embedded_id, characteristic)
    if not all(isinstance(item, str) and item.strip() for item in identifiers):
        raise OnectaError("ONECTA control target is incomplete")
    if path is not None and (
        not isinstance(path, str)
        or not path.startswith("/")
        or ".." in path
    ):
        raise OnectaError("ONECTA control path is invalid")

    access_token = _required_setting("ONECTA_ACCESS_TOKEN")
    url = (
        f"{API_GATEWAY_DEVICES_URL}/{gateway_device_id}"
        f"/management-points/{embedded_id}/characteristics/{characteristic}"
    )
    body: dict[str, Any] = {"value": value}
    if path:
        body["path"] = path

    response = None
    for attempt in range(2):
        try:
            response = http.patch(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise OnectaError("ONECTA control service is unavailable") from error
        if response.status_code != 401 or attempt:
            break
        access_token = refresh_access_token(secret_file=secret_file, http=http)

    assert response is not None
    limits = _rate_limit_headers(response)
    if response.status_code == 429:
        raise OnectaError(
            "ONECTA rate limit reached"
            + (
                f"; retry after {limits['retry_after']} seconds"
                if limits.get("retry_after")
                else ""
            )
        )
    if response.status_code not in (200, 202, 204):
        raise _oauth_error(response, "ONECTA control request")
    return limits


def _looks_sensitive(value: str) -> bool:
    return bool(
        _UUID_RE.fullmatch(value)
        or _MAC_RE.fullmatch(value)
        or _IPV4_RE.fullmatch(value)
    )


def _sanitize_capability_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key in _IDENTITY_KEYS:
                sanitized[key] = "<redacted>"
            else:
                sanitized[key] = _sanitize_capability_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_capability_value(item) for item in value]
    if isinstance(value, str) and _looks_sensitive(value):
        return "<redacted>"
    return value


def _anonymous_device_key(device: Mapping[str, Any], index: int) -> str:
    raw = str(device.get("id") or device.get("_id") or index)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"unit-{index}-{digest}"


def build_capability_report(
    devices: list[dict[str, Any]],
    rate_limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build a useful report without account, room, network, or device IDs."""
    report_devices = []
    for index, device in enumerate(devices, start=1):
        management_points = []
        raw_points = device.get("managementPoints")
        if not isinstance(raw_points, list):
            raw_points = []
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            capability_names = sorted(
                key
                for key in point
                if key not in _MANAGEMENT_POINT_METADATA
                and key not in _IDENTITY_KEYS
            )
            relevant = {
                key: _sanitize_capability_value(point[key])
                for key in capability_names
                if key in _FORECAST_RELEVANT_CAPABILITIES
            }
            management_points.append(
                {
                    "type": point.get("managementPointType"),
                    "subtype": point.get("managementPointSubType"),
                    "category": point.get("managementPointCategory"),
                    "friendly_name_present": bool(
                        isinstance(point.get("name"), dict)
                        and point["name"].get("value")
                    ),
                    "capability_names": capability_names,
                    "forecast_relevant": relevant,
                }
            )
        cloud = device.get("isCloudConnectionUp")
        report_devices.append(
            {
                "unit": _anonymous_device_key(device, index),
                "type": device.get("type"),
                "device_model": device.get("deviceModel"),
                "cloud_connected": (
                    cloud.get("value") if isinstance(cloud, dict) else None
                ),
                "management_points": management_points,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "device_count": len(report_devices),
        "rate_limits": dict(rate_limits or {}),
        "devices": report_devices,
    }


def write_capability_report(
    report: Mapping[str, Any],
    path: str | Path = DEFAULT_REPORT_PATH,
) -> None:
    _atomic_write(
        Path(path),
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
