import importlib.util
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


def _load_tibber_api(monkeypatch, tmp_path):
    tibber_stub = types.ModuleType("tibber")
    tibber_stub.Account = lambda _token: types.SimpleNamespace(homes=[
        types.SimpleNamespace(current_subscription=types.SimpleNamespace(price_info=types.SimpleNamespace()))
    ])
    monkeypatch.setitem(sys.modules, "tibber", tibber_stub)

    mqtt_stub = types.ModuleType("lib.clients.mqtt_client_factory")

    class _ClientFactory:
        def get_client(self):
            return types.SimpleNamespace(publish=lambda *args, **kwargs: None)

    mqtt_stub.VictronClient = _ClientFactory
    monkeypatch.setitem(sys.modules, "lib.clients.mqtt_client_factory", mqtt_stub)

    gql_stub = types.ModuleType("gql")
    gql_transport_stub = types.ModuleType("gql.transport")
    gql_exceptions_stub = types.ModuleType("gql.transport.exceptions")

    class _TransportClosed(Exception):
        pass

    class _TransportQueryError(Exception):
        pass

    gql_exceptions_stub.TransportClosed = _TransportClosed
    gql_exceptions_stub.TransportQueryError = _TransportQueryError
    monkeypatch.setitem(sys.modules, "gql", gql_stub)
    monkeypatch.setitem(sys.modules, "gql.transport", gql_transport_stub)
    monkeypatch.setitem(sys.modules, "gql.transport.exceptions", gql_exceptions_stub)

    websockets_stub = types.ModuleType("websockets")
    websockets_exceptions_stub = types.ModuleType("websockets.exceptions")

    class _ConnectionClosedError(Exception):
        pass

    websockets_exceptions_stub.ConnectionClosedError = _ConnectionClosedError
    monkeypatch.setitem(sys.modules, "websockets", websockets_stub)
    monkeypatch.setitem(sys.modules, "websockets.exceptions", websockets_exceptions_stub)

    deferred_workers = []
    real_thread = threading.Thread

    class _DeferredThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            deferred_workers.append(self.target)

    # The module starts account discovery at import time. Defer that worker until its
    # settings and sleep dependencies have been replaced, eliminating a teardown race with
    # pytest's temporary environment variables.
    monkeypatch.setattr(threading, "Thread", _DeferredThread)
    spec = importlib.util.spec_from_file_location(
        "tibber_api_under_test",
        Path(__file__).resolve().parents[1] / "lib" / "tibber_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(threading, "Thread", real_thread)

    cache_path = tmp_path / "prices.json"

    def _setting(name):
        return {
            "TIBBER_ACCESS_TOKEN": "token",
            "TIBBER_PRICE_RESOLUTION": "QUARTER_HOURLY",
            "TIBBER_PRICE_CACHE_PATH": str(cache_path),
            "TIMEZONE": "UTC",
        }.get(name)

    monkeypatch.setattr(module, "retrieve_setting", _setting)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    for worker in deferred_workers:
        worker()
    module._PRICE_CACHE.clear()
    return module, cache_path


def _points(start=None, count=4, step_min=15, price=0.20):
    base = start or (datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15))
    return [
        {
            "start": (base + timedelta(minutes=step_min * i)).isoformat(),
            "total": price + (i * 0.001),
            "level": "NORMAL",
        }
        for i in range(count)
    ]


def _response(points):
    return {
        "data": {
            "viewer": {
                "homes": [
                    {
                        "currentSubscription": {
                            "priceInfo": {
                                "today": [
                                    {"startsAt": p["start"], "total": p["total"], "level": p["level"]}
                                    for p in points
                                ],
                                "tomorrow": [],
                            }
                        }
                    }
                ]
            }
        }
    }


def test_quarter_hour_fetch_retries_and_caches(monkeypatch, tmp_path):
    module, cache_path = _load_tibber_api(monkeypatch, tmp_path)
    points = _points()
    calls = {"count": 0}
    monkeypatch.setattr(module, "_next_day_prices_expected", lambda: False)

    class _Resp:
        status_code = 200

        def json(self):
            return _response(points)

    def _post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.Timeout("temporary timeout")
        return _Resp()

    monkeypatch.setattr(module.requests, "post", _post)

    result = module.get_all_price_points()

    assert calls["count"] == 2
    assert result == points
    assert cache_path.with_name("prices_QUARTER_HOURLY.json").exists()


def test_quarter_hour_failure_uses_cached_prices_before_hourly(monkeypatch, tmp_path):
    module, _cache_path = _load_tibber_api(monkeypatch, tmp_path)
    cached = _points()
    module._cache_price_points("QUARTER_HOURLY", cached)
    calls = []

    def _fetch(resolution, *args, **kwargs):
        calls.append(resolution)
        return []

    monkeypatch.setattr(module, "_fetch_price_points_graphql", _fetch)

    result = module.get_all_price_points()

    assert calls == ["QUARTER_HOURLY"]
    assert result == cached


def test_stale_quarter_hour_cache_falls_back_to_hourly(monkeypatch, tmp_path):
    module, _cache_path = _load_tibber_api(monkeypatch, tmp_path)
    stale = _points(
        start=datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=2),
        count=4,
    )
    hourly = _points(step_min=60, price=0.25)
    module._cache_price_points("QUARTER_HOURLY", stale)
    calls = []

    def _fetch(resolution, *args, **kwargs):
        calls.append(resolution)
        return hourly if resolution == "HOURLY" else []

    monkeypatch.setattr(module, "_fetch_price_points_graphql", _fetch)

    result = module.get_all_price_points()

    assert calls == ["QUARTER_HOURLY", "HOURLY"]
    assert result == hourly


def test_after_publish_today_only_quarter_hour_tries_hourly_tomorrow(monkeypatch, tmp_path):
    module, _cache_path = _load_tibber_api(monkeypatch, tmp_path)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    today_only = _points(start=today_start, count=96, step_min=15, price=0.20)
    tomorrow = _points(start=tomorrow_start, count=24, step_min=60, price=0.30)
    hourly = _points(start=today_start, count=24, step_min=60, price=0.22) + tomorrow
    calls = []

    monkeypatch.setattr(module, "_next_day_prices_expected", lambda: True)

    def _fetch(resolution, *args, **kwargs):
        calls.append(resolution)
        return today_only if resolution == "QUARTER_HOURLY" else hourly

    monkeypatch.setattr(module, "_fetch_price_points_graphql", _fetch)

    result = module.get_all_price_points()

    assert calls == ["QUARTER_HOURLY", "HOURLY"]
    assert result == hourly


def test_after_publish_today_only_result_logs_truthful_warning(monkeypatch, tmp_path, caplog):
    module, _cache_path = _load_tibber_api(monkeypatch, tmp_path)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_only = _points(start=today_start, count=96, step_min=15, price=0.20)
    calls = []

    monkeypatch.setattr(module, "_next_day_prices_expected", lambda: True)

    def _fetch(resolution, *args, **kwargs):
        calls.append(resolution)
        return today_only if resolution == "QUARTER_HOURLY" else []

    monkeypatch.setattr(module, "_fetch_price_points_graphql", _fetch)
    caplog.set_level("WARNING")

    result = module.get_all_price_points()

    assert calls == ["QUARTER_HOURLY", "HOURLY"]
    assert result == today_only
    assert "next-day quarter-hourly prices still unavailable" in caplog.text
