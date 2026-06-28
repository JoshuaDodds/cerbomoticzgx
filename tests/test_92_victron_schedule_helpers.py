from lib import helpers
from lib import constants


def test_clear_victron_schedules_disables_five_charge_slots(monkeypatch):
    calls = []

    monkeypatch.setattr(constants, "systemId0", "portal-123")
    monkeypatch.setattr(
        helpers,
        "publish_message",
        lambda topic, payload=None, retain=True, **kwargs: calls.append(
            {"topic": topic, "payload": payload, "retain": retain, **kwargs}
        ),
    )

    helpers.clear_victron_schedules()

    assert calls == [
        {
            "topic": f"W/portal-123/settings/0/Settings/CGwacs/BatteryLife/Schedule/Charge/{i}/Day",
            "payload": "{\"value\": -1}",
            "retain": False,
        }
        for i in range(5)
    ]
