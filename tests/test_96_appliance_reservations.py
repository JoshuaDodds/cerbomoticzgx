"""TDD coverage for persisted flexible-load reservations."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest


def _reservation(device="dishwasher", *, start=None, hours=1.0, load_kw=1.2):
    start = start or datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
    return {
        "device": device,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=hours)).isoformat(),
        "load_kw": load_kw,
        "program": 8203 if device == "dishwasher" else 32023,
        "source": "appliance_optimizer",
    }


def test_reservation_round_trips_atomically(tmp_path):
    from lib import appliance_reservations as reservations

    path = tmp_path / "reservations.json"
    expected = _reservation()

    reservations.upsert(expected, path=path)

    assert reservations.active(path=path, now=datetime(2026, 1, 15, tzinfo=timezone.utc)) == [expected]
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_upsert_replaces_only_the_same_device(tmp_path):
    from lib import appliance_reservations as reservations

    path = tmp_path / "reservations.json"
    first = _reservation(start=datetime(2026, 1, 15, 1, tzinfo=timezone.utc))
    replacement = _reservation(start=datetime(2026, 1, 15, 2, tzinfo=timezone.utc))
    dryer = _reservation("dryer", start=datetime(2026, 1, 15, 3, tzinfo=timezone.utc))

    reservations.upsert(first, path=path)
    reservations.upsert(dryer, path=path)
    reservations.upsert(replacement, path=path)

    values = reservations.active(
        path=path, now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert {item["device"] for item in values} == {"dishwasher", "dryer"}
    assert next(item for item in values if item["device"] == "dishwasher") == replacement


def test_overlay_accounts_for_partial_overlap_and_two_appliances():
    from lib import appliance_reservations as reservations

    slot = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
    forecast = {slot: 0.25}
    loads = [
        _reservation(start=slot + timedelta(minutes=5), hours=0.25, load_kw=1.2),
        _reservation("dryer", start=slot + timedelta(minutes=10), hours=0.25, load_kw=0.8),
    ]

    result, diagnostics = reservations.overlay_forecast(
        forecast, loads, slot_duration_h=0.25)

    # Dishwasher overlaps 10 minutes (0.2 kWh), dryer 5 minutes (~0.0667 kWh).
    assert result[slot] == pytest.approx(0.25 + 0.2 + (0.8 * 5 / 60))
    assert diagnostics["reserved_kwh"] == pytest.approx(0.2 + (0.8 * 5 / 60))
    assert diagnostics["devices"] == ["dishwasher", "dryer"]


def test_overlay_uses_profile_energy_instead_of_flat_average():
    from lib import appliance_reservations as reservations

    slot = datetime(2026, 1, 15, 1, tzinfo=timezone.utc)
    load = _reservation(start=slot, hours=1, load_kw=1.0)
    load["load_profile"] = [{
        "start": slot.isoformat(),
        "end": (slot + timedelta(minutes=15)).isoformat(),
        "energy_kwh": 1.0,
        "load_w": 4000,
    }]

    result, diagnostics = reservations.overlay_forecast(
        {slot: 0.0}, [load], slot_duration_h=0.25)

    assert result[slot] == pytest.approx(1.0)
    assert diagnostics["reserved_kwh"] == pytest.approx(1.0)


def test_overlay_uses_elapsed_time_across_dst_fallback():
    from lib import appliance_reservations as reservations

    zone = ZoneInfo("Europe/Amsterdam")
    slot = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0)
    start = slot
    end = datetime.fromtimestamp(start.timestamp() + 90 * 60, tz=zone)
    load = {
        "device": "dishwasher",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "load_kw": 1.2,
    }

    result, diagnostics = reservations.overlay_forecast(
        {slot: 0.0}, [load], slot_duration_h=0.25)

    # One optimizer quarter is always 15 elapsed minutes, even though adding a
    # wall-time timedelta at 02:45 CEST would land at 03:00 CET (75 minutes).
    assert result[slot] == pytest.approx(0.3)
    assert diagnostics["reserved_kwh"] == pytest.approx(0.3)


def test_expired_invalid_and_naive_reservations_are_ignored(tmp_path):
    from lib import appliance_reservations as reservations

    path = tmp_path / "reservations.json"
    expired = _reservation(start=datetime(2026, 1, 14, 1, tzinfo=timezone.utc))
    invalid = {"device": "dryer", "start": "bad", "end": "worse", "load_kw": 1}
    naive = _reservation(start=datetime(2026, 1, 15, 1))

    reservations._write([expired, invalid, naive], path)

    assert reservations.active(
        path=path, now=datetime(2026, 1, 15, tzinfo=timezone.utc)) == []


def test_remove_is_idempotent(tmp_path):
    from lib import appliance_reservations as reservations

    path = tmp_path / "reservations.json"
    reservations.upsert(_reservation(), path=path)

    assert reservations.remove("dishwasher", path=path) is True
    assert reservations.remove("dishwasher", path=path) is False
    assert reservations.active(
        path=path, now=datetime(2026, 1, 15, tzinfo=timezone.utc)) == []


@pytest.mark.parametrize(
    ("optimizer_mode", "feature_enabled", "master_enabled"),
    [
        ("summer", False, True),
        ("winter", False, True),
        ("summer", True, False),
        ("winter", True, False),
    ],
)
def test_energy_broker_does_not_overlay_reservations_outside_feature_and_master_gate(
    monkeypatch, optimizer_mode, feature_enabled, master_enabled
):
    from lib import appliance_reservations, energy_broker

    slot = datetime(2026, 1, 15, 1, tzinfo=timezone.utc)
    original = {slot: 0.25}
    monkeypatch.setattr(energy_broker, "OPTIMIZER_MODE", optimizer_mode)
    monkeypatch.setattr(
        energy_broker, "APPLIANCE_OPTIMIZATION_ENABLED", feature_enabled, raising=False)
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: master_enabled if name == "HOME_CONNECT_APPLIANCE_SCHEDULING" else None,
    )
    monkeypatch.setattr(appliance_reservations, "active", lambda **kwargs: [])

    forecast, diagnostics = energy_broker._apply_appliance_reservations_to_forecast(
        original, slot_duration_h=0.25, now=slot)

    assert forecast == original
    assert diagnostics == {
        "enabled": False,
        "devices": [],
        "reserved_kwh": 0.0,
        "active_reservations": 0,
    }


def test_accepted_reservation_remains_forecast_after_policy_is_disabled(monkeypatch):
    """Disabling future scheduling must not erase appliance work already accepted."""
    from lib import appliance_reservations, energy_broker

    slot = datetime(2026, 1, 15, 1, tzinfo=timezone.utc)
    accepted = [_reservation(start=slot, hours=0.25, load_kw=1.2)]
    monkeypatch.setattr(energy_broker, "OPTIMIZER_MODE", "summer")
    monkeypatch.setattr(
        energy_broker, "APPLIANCE_OPTIMIZATION_ENABLED", False, raising=False)
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: False if name == "HOME_CONNECT_APPLIANCE_SCHEDULING" else None,
    )
    monkeypatch.setattr(appliance_reservations, "active", lambda **kwargs: accepted)

    forecast, diagnostics = energy_broker._apply_appliance_reservations_to_forecast(
        {slot: 0.25}, slot_duration_h=0.25, now=slot)

    assert forecast[slot] == pytest.approx(0.55)
    assert diagnostics["enabled"] is False
    assert diagnostics["active_reservations"] == 1
    assert diagnostics["reserved_kwh"] == pytest.approx(0.3)


@pytest.mark.parametrize("optimizer_mode", ["summer", "winter"])
def test_energy_broker_overlays_combined_accepted_load_in_both_seasons(
    monkeypatch, optimizer_mode
):
    from lib import appliance_reservations, energy_broker

    slot = datetime(2026, 1, 15, 1, tzinfo=timezone.utc)
    accepted = [
        _reservation(start=slot, hours=0.25, load_kw=1.2),
        _reservation("dryer", start=slot, hours=0.25, load_kw=0.8),
    ]
    monkeypatch.setattr(energy_broker, "OPTIMIZER_MODE", optimizer_mode)
    monkeypatch.setattr(
        energy_broker, "APPLIANCE_OPTIMIZATION_ENABLED", True, raising=False)
    monkeypatch.setattr(
        energy_broker,
        "retrieve_setting",
        lambda name: True if name == "HOME_CONNECT_APPLIANCE_SCHEDULING" else None,
    )
    monkeypatch.setattr(appliance_reservations, "active", lambda **kwargs: accepted)

    forecast, diagnostics = energy_broker._apply_appliance_reservations_to_forecast(
        {slot: 0.25}, slot_duration_h=0.25, now=slot)

    assert forecast[slot] == pytest.approx(0.75)
    assert diagnostics["enabled"] is True
    assert diagnostics["devices"] == ["dishwasher", "dryer"]
    assert diagnostics["reserved_kwh"] == pytest.approx(0.5)
    assert diagnostics["active_reservations"] == 2
