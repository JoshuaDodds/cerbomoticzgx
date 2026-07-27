"""Retained normalized Tesla state must rebuild controller-facing GlobalState."""

from lib import event_handler


def test_retained_plugged_status_hydrates_controller_key(monkeypatch):
    stored = {}

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr(event_handler, "GlobalStateClient", lambda: State())

    event_handler.Event(
        "Tesla/vehicle0/plugged_status", "Plugged"
    ).dispatch()

    assert stored["tesla_plug_status"] == "Plugged"
    assert stored["tesla_is_plugged"] == "True"


def test_retained_unplugged_status_hydrates_controller_key(monkeypatch):
    stored = {}

    class State:
        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr(event_handler, "GlobalStateClient", lambda: State())

    event_handler.Event(
        "Tesla/vehicle0/plugged_status", "Unplugged"
    ).dispatch()

    assert stored["tesla_is_plugged"] == "False"


def test_abb_power_event_records_transition_without_affecting_control(monkeypatch):
    stored = {}
    observed = []

    class State:
        def set(self, key, value):
            stored[key] = value

        def get(self, key):
            return stored.get(key, 0)

    monkeypatch.setattr(event_handler, "GlobalStateClient", lambda: State())
    monkeypatch.setattr(
        event_handler,
        "record_ev_power_observation",
        lambda value, **kwargs: observed.append(float(value)),
    )
    monkeypatch.setattr(event_handler.Event, "adjust_ac_out_power", lambda self: None)
    monkeypatch.setattr(event_handler, "publish_message", lambda *args, **kwargs: None)

    event_handler.Event("unused", 3540.5).tesla_power()

    assert observed == [3540.5]
    assert "tesla_power_updated_at" in stored
