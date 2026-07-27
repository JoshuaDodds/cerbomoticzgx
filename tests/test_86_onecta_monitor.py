import json
import threading
import time
from datetime import datetime, timezone

import pytest

from lib import onecta_monitor as monitor


@pytest.fixture(autouse=True)
def _isolate_mqtt(monkeypatch):
    """Never allow a unit-test snapshot to reach the configured MQTT broker."""
    published = []
    monkeypatch.setattr(
        monitor,
        "publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    return published


def _capability(value, **extra):
    return {"value": value, "settable": False, **extra}


def _device(
    device_id,
    *,
    cooling_today,
    cooling_yesterday=0.0,
    heating_today=0.0,
    heating_yesterday=0.0,
    on=True,
    mode="cooling",
    room_temp=24.5,
    outdoor_temp=21.0,
    source_ts="2026-07-27T09:00:00Z",
):
    def day_values(yesterday, today):
        return [yesterday] + [0.0] * 11 + [today] + [0.0] * 11

    return {
        "id": device_id,
        "deviceModel": "dx4",
        "timestamp": source_ts,
        "isCloudConnectionUp": _capability(True),
        "managementPoints": [
            {
                "embeddedId": "climateControl",
                "managementPointType": "climateControl",
                "managementPointSubType": "mainZone",
                "name": _capability("Mock Unit"),
                "onOffMode": _capability("on" if on else "off"),
                "operationMode": _capability(mode),
                "sensoryData": _capability(
                    {
                        "roomTemperature": _capability(room_temp),
                        "outdoorTemperature": _capability(outdoor_temp),
                    }
                ),
                "temperatureControl": _capability(
                    {
                        "operationModes": {
                            "cooling": {
                                "setpoints": {
                                    "roomTemperature": _capability(25.5),
                                }
                            },
                            "heating": {
                                "setpoints": {
                                    "roomTemperature": _capability(20.0),
                                }
                            },
                        }
                    }
                ),
                "consumptionData": _capability(
                    {
                        "electrical": {
                            "unit": "kWh",
                            "cooling": {
                                "d": day_values(
                                    cooling_yesterday, cooling_today
                                ),
                                "w": [0.0] * 14,
                                "m": [0.0] * 24,
                            },
                            "heating": {
                                "d": day_values(
                                    heating_yesterday, heating_today
                                ),
                                "w": [0.0] * 14,
                                "m": [0.0] * 24,
                            },
                        }
                    }
                ),
            }
        ],
    }


def test_daily_consumption_uses_last_twelve_two_hour_buckets_only():
    snapshot = monitor.normalize_hvac_snapshot(
        [
            _device(
                "one",
                cooling_today=0.4,
                cooling_yesterday=1.6,
                heating_today=0.2,
                heating_yesterday=0.8,
            )
        ],
        fetched_at=datetime(2026, 7, 27, 9, 1, tzinfo=timezone.utc),
    )

    unit = snapshot["units"][0]
    assert unit["energy"]["today"] == {
        "cooling_kwh": 0.4,
        "heating_kwh": 0.2,
        "total_kwh": 0.6,
    }
    assert unit["energy"]["yesterday"] == {
        "cooling_kwh": 1.6,
        "heating_kwh": 0.8,
        "total_kwh": 2.4,
    }
    assert snapshot["summary"]["today_total_kwh"] == 0.6
    assert snapshot["summary"]["yesterday_total_kwh"] == 2.4
    assert snapshot["summary"]["powered_units"] == 1
    assert snapshot["summary"]["powered_modes"] == ["cooling"]


def test_four_unit_snapshot_aggregates_energy_without_exposing_names_or_ids():
    snapshot = monitor.normalize_hvac_snapshot(
        [
            _device("one", cooling_today=0.1),
            _device("two", cooling_today=0.2),
            _device("three", cooling_today=0.3, on=False),
            _device("four", cooling_today=0.4),
        ]
    )

    assert snapshot["summary"]["device_count"] == 4
    assert snapshot["summary"]["connected_units"] == 4
    assert snapshot["summary"]["powered_units"] == 3
    assert snapshot["summary"]["today_cooling_kwh"] == 1.0
    assert snapshot["summary"]["today_heating_kwh"] == 0.0
    assert snapshot["summary"]["today_total_kwh"] == 1.0
    serialised = json.dumps(snapshot)
    assert "Mock Unit" in serialised
    assert '"id"' not in serialised
    assert all(unit["unit"].startswith("unit-") for unit in snapshot["units"])


def test_malformed_daily_array_is_not_silently_summed():
    device = _device("one", cooling_today=0.4)
    climate = device["managementPoints"][0]
    climate["consumptionData"]["value"]["electrical"]["cooling"]["d"].pop()

    snapshot = monitor.normalize_hvac_snapshot([device])

    assert snapshot["units"][0]["energy"]["today"]["cooling_kwh"] is None
    assert snapshot["summary"]["today_cooling_kwh"] is None
    assert any(
        issue.endswith("cooling_daily_array_length_23")
        for issue in snapshot["quality"]["issues"]
    )


def test_missing_climate_unit_never_publishes_a_partial_house_total():
    missing = _device("missing", cooling_today=9.0)
    missing["managementPoints"] = []

    snapshot = monitor.normalize_hvac_snapshot(
        [_device("one", cooling_today=0.4), missing]
    )

    assert snapshot["quality"]["complete"] is False
    assert snapshot["summary"]["device_count"] == 1
    assert snapshot["summary"]["today_total_kwh"] is None


def test_store_snapshot_is_atomic_and_history_deduplicates_source_state(tmp_path):
    latest = tmp_path / "latest.json"
    history = tmp_path / "history.ndjson"
    snapshot = monitor.normalize_hvac_snapshot(
        [_device("one", cooling_today=0.4)],
        fetched_at=datetime(2026, 7, 27, 9, 1, tzinfo=timezone.utc),
    )

    assert monitor.store_snapshot(snapshot, latest, history) is True
    assert monitor.store_snapshot(snapshot, latest, history) is False

    assert json.loads(latest.read_text()) == snapshot
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    assert rows == [snapshot]


def test_collector_publishes_retained_summary_and_units_after_success(
    tmp_path, monkeypatch
):
    published = []
    monkeypatch.setattr(
        monitor,
        "publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    collector = monitor.OnectaMonitor(
        fetch=lambda: (
            [_device("one", cooling_today=0.4)],
            {"remaining_day": 198, "limit_day": 200},
        ),
        latest_path=tmp_path / "latest.json",
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
    )

    assert collector.refresh() is True

    topics = {topic: kwargs for topic, kwargs in published}
    assert set(topics) == {
        "hvac/status",
        "hvac/summary",
        f"hvac/units/{collector.snapshot['units'][0]['unit']}",
    }
    assert all(kwargs["retain"] is True for kwargs in topics.values())
    assert json.loads(topics["hvac/status"]["payload"])["status"] == "connected"
    assert collector.snapshot["rate_limits"]["remaining_day"] == 198


def test_ensure_fresh_waits_for_one_inflight_refresh_and_fails_open(
    tmp_path, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_fetch():
        calls.append("fetch")
        started.set()
        release.wait(1)
        return [_device("one", cooling_today=0.4)], {}

    collector = monitor.OnectaMonitor(
        fetch=slow_fetch,
        latest_path=tmp_path / "latest.json",
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
    )
    worker = collector.request_refresh()
    assert started.wait(1)

    stale = collector.ensure_fresh(max_age_seconds=60, wait_timeout_seconds=0.01)
    assert stale["available"] is False
    assert stale["fresh"] is False
    assert calls == ["fetch"]

    release.set()
    worker.join(1)
    fresh = collector.ensure_fresh(max_age_seconds=60, wait_timeout_seconds=0.1)
    assert fresh["available"] is True
    assert fresh["fresh"] is True
    assert calls == ["fetch"]


def test_disabled_monitor_never_calls_cloud(tmp_path):
    collector = monitor.OnectaMonitor(
        fetch=lambda: pytest.fail("disabled monitor made an API call"),
        latest_path=tmp_path / "latest.json",
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: False,
    )

    assert collector.request_refresh() is None
    assert collector.ensure_fresh()["enabled"] is False


def test_rate_limit_reserve_blocks_cloud_without_discarding_cached_state(
    tmp_path,
):
    latest = tmp_path / "latest.json"
    cached = monitor.normalize_hvac_snapshot(
        [_device("one", cooling_today=0.4)],
        {"remaining_day": 10, "limit_day": 200},
    )
    monitor.store_snapshot(cached, latest, tmp_path / "history.ndjson")
    collector = monitor.OnectaMonitor(
        fetch=lambda: pytest.fail("reserved API allowance was consumed"),
        latest_path=latest,
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
        daily_request_reserve=10,
    )

    assert collector.request_refresh() is None
    context = collector.ensure_fresh(max_age_seconds=3600)
    assert context["available"] is True


def test_failed_refresh_retains_last_snapshot_and_marks_status_stale(
    tmp_path, monkeypatch
):
    published = []
    latest = tmp_path / "latest.json"
    cached = monitor.normalize_hvac_snapshot(
        [_device("one", cooling_today=0.4)],
    )
    monitor.store_snapshot(cached, latest, tmp_path / "history.ndjson")
    monkeypatch.setattr(
        monitor,
        "publish_message",
        lambda topic, **kwargs: published.append((topic, kwargs)),
    )
    collector = monitor.OnectaMonitor(
        fetch=lambda: (_ for _ in ()).throw(
            monitor.OnectaError("cloud unavailable")
        ),
        latest_path=latest,
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
    )

    assert collector.refresh() is False
    assert collector.snapshot == cached
    assert json.loads(published[-1][1]["payload"])["status"] == "stale"


def test_incomplete_device_response_does_not_replace_four_unit_snapshot(
    tmp_path, monkeypatch
):
    latest = tmp_path / "latest.json"
    cached = monitor.normalize_hvac_snapshot(
        [
            _device("one", cooling_today=0.1),
            _device("two", cooling_today=0.2),
            _device("three", cooling_today=0.3),
            _device("four", cooling_today=0.4),
        ]
    )
    monitor.store_snapshot(cached, latest, tmp_path / "history.ndjson")
    monkeypatch.setattr(monitor, "publish_message", lambda *args, **kwargs: None)
    collector = monitor.OnectaMonitor(
        fetch=lambda: ([_device("one", cooling_today=9.0)], {}),
        latest_path=latest,
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
    )

    assert collector.refresh() is False
    assert collector.snapshot == cached
    assert json.loads(latest.read_text()) == cached


def test_recent_durable_snapshot_prevents_restart_storm_api_call(tmp_path):
    latest = tmp_path / "latest.json"
    cached = monitor.normalize_hvac_snapshot(
        [_device("one", cooling_today=0.4)],
    )
    monitor.store_snapshot(cached, latest, tmp_path / "history.ndjson")
    collector = monitor.OnectaMonitor(
        fetch=lambda: pytest.fail("restart repeated a recent cloud read"),
        latest_path=latest,
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
    )

    assert collector.request_refresh() is None


def test_startup_cache_window_reuses_snapshot_younger_than_nineteen_minutes(
    tmp_path, monkeypatch
):
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    cached = monitor.normalize_hvac_snapshot(
        [_device("one", cooling_today=0.4)],
        fetched_at=now,
    )
    # Avoid depending on wall-clock time while exercising the exact cache gate.
    monkeypatch.setattr(monitor, "_snapshot_age_seconds", lambda snapshot: 1139)
    latest = tmp_path / "latest.json"
    monitor.store_snapshot(cached, latest, tmp_path / "history.ndjson")
    collector = monitor.OnectaMonitor(
        fetch=lambda: pytest.fail("recent startup cache caused a cloud read"),
        latest_path=latest,
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
    )

    assert collector.request_refresh(max_cached_age_seconds=1140) is None


def test_default_twenty_minute_schedule_is_aligned_to_ten_thirty_fifty():
    # 12:12 UTC -> 12:30 UTC; epoch values avoid local-time/DST assumptions.
    now = datetime(2026, 7, 27, 12, 12, tzinfo=timezone.utc).timestamp()
    due = monitor._next_fetch_due_epoch(now, 20 * 60)

    assert datetime.fromtimestamp(due, timezone.utc).strftime("%H:%M") == "12:30"


def test_forced_refresh_bypasses_fresh_cache_when_control_registry_is_missing(
    tmp_path,
):
    latest = tmp_path / "latest.json"
    cached = monitor.normalize_hvac_snapshot(
        [_device("one", cooling_today=0.4)],
    )
    monitor.store_snapshot(cached, latest, tmp_path / "history.ndjson")
    called = threading.Event()
    collector = monitor.OnectaMonitor(
        fetch=lambda: (
            called.set() or [_device("one", cooling_today=0.4)],
            {},
        ),
        latest_path=latest,
        history_path=tmp_path / "history.ndjson",
        enabled=lambda: True,
    )

    worker = collector.request_refresh(
        max_cached_age_seconds=1140,
        force=True,
    )

    assert worker is not None
    worker.join(1)
    assert called.is_set()
    assert collector.control_registry_ready() is True
