from frontend import server
from frontend import advisor
import sys
import types


def test_favicon_route_redirects_to_brand_icon():
    response = server.app.test_client().get("/favicon.ico")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/static/img/logo.svg")


def test_clear_import_schedule_route_calls_broker_helper(monkeypatch):
    calls = []

    monkeypatch.setattr(server, "_clear_import_schedule", lambda: calls.append("clear"), raising=False)

    response = server.app.test_client().post("/api/victron/clear-schedule")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert calls == ["clear"]


def test_clear_import_schedule_route_reports_helper_failure(monkeypatch):
    def fail():
        raise RuntimeError("mqtt publish failed")

    monkeypatch.setattr(server, "_clear_import_schedule", fail, raising=False)

    response = server.app.test_client().post("/api/victron/clear-schedule")

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "mqtt publish failed" in body["error"]


def test_restart_route_publishes_existing_shutdown_topic(monkeypatch):
    calls = []

    monkeypatch.setattr(
        server,
        "_request_service_restart",
        lambda: calls.append(("Cerbomoticzgx/system/shutdown", "True", True)),
        raising=False,
    )

    response = server.app.test_client().post("/api/restart")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert calls == [("Cerbomoticzgx/system/shutdown", "True", True)]


def test_restart_helper_uses_existing_supervised_restart_mqtt_topic(monkeypatch):
    calls = []

    monkeypatch.setattr(
        server,
        "publish_message",
        lambda topic, message, retain: calls.append((topic, message, retain)),
        raising=False,
    )

    server._request_service_restart()

    assert calls == [("Cerbomoticzgx/system/shutdown", "True", True)]


def test_restart_route_reports_publish_failure(monkeypatch):
    def fail():
        raise RuntimeError("mqtt publish failed")

    monkeypatch.setattr(server, "_request_service_restart", fail, raising=False)

    response = server.app.test_client().post("/api/restart")

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "mqtt publish failed" in body["error"]


def test_ai_override_route_sets_state_and_idles_once(monkeypatch):
    calls = []

    monkeypatch.setattr(server, "_set_ai_ess_override", lambda enabled: calls.append(("override", enabled)), raising=False)

    response = server.app.test_client().post("/api/control/ai-override", json={"enabled": True})

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "enabled": True}
    assert calls == [("override", True)]


def test_grid_assist_route_reuses_existing_grid_charge_toggle(monkeypatch):
    calls = []

    monkeypatch.setattr(server, "_set_grid_assist_toggle", lambda enabled: calls.append(("grid", enabled)), raising=False)

    response = server.app.test_client().post("/api/control/grid-assist", json={"enabled": True})

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "enabled": True}
    assert calls == [("grid", True)]


def test_grid_assist_helper_applies_full_load_immediately(monkeypatch):
    import lib.global_state as global_state

    calls = []

    class FakeState:
        def __init__(self):
            self.values = {"ac_out_power": 4200}

        def set(self, key, value):
            calls.append(("state", key, value))
            self.values[key] = value

        def get(self, key):
            return self.values.get(key)

    monkeypatch.setattr(global_state, "GlobalStateClient", FakeState)
    monkeypatch.setattr(server, "publish_message", lambda topic, message, retain: calls.append(("mqtt", topic, message, retain)))
    monkeypatch.setitem(
        sys.modules,
        "lib.energy_broker",
        types.SimpleNamespace(
            _apply_grid_assist_setpoint=lambda load_watts=None, cover_all_load=False: calls.append(("apply", load_watts, cover_all_load))
        ),
    )

    server._set_grid_assist_toggle(True)

    assert ("state", "grid_charging_enabled", True) in calls
    assert ("state", "ai_grid_assist", "on") in calls
    assert ("mqtt", "Cerbomoticzgx/system/grid_charging_enabled", "True", True) in calls
    assert ("apply", 4200, True) in calls


def test_grid_assist_helper_logs_on_and_off(monkeypatch, caplog):
    import lib.global_state as global_state

    class FakeState:
        def __init__(self):
            self.values = {"ac_out_power": 4200}

        def set(self, key, value):
            self.values[key] = value

        def get(self, key):
            return self.values.get(key)

    monkeypatch.setattr(global_state, "GlobalStateClient", FakeState)
    monkeypatch.setattr(server, "publish_message", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        sys.modules,
        "lib.energy_broker",
        types.SimpleNamespace(_apply_grid_assist_setpoint=lambda **kwargs: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "lib.victron_integration",
        types.SimpleNamespace(ac_power_setpoint=lambda **kwargs: None),
    )

    caplog.set_level("INFO")

    server._set_grid_assist_toggle(True)
    server._set_grid_assist_toggle(False)

    assert "Grid assist enabled: matching grid setpoint to current AC load 4200W." in caplog.text
    assert "Grid assist disabled: returned AC setpoint to 0W." in caplog.text


def test_history_accuracy_route_returns_forecast_accuracy(monkeypatch):
    calls = []

    monkeypatch.setattr(
        server.data,
        "forecast_accuracy",
        lambda days=3: calls.append(days) or {"available": True, "slots": [], "summary": {"slots": 0}},
    )

    response = server.app.test_client().get("/api/history/accuracy?days=5")

    assert response.status_code == 200
    assert response.get_json()["available"] is True
    assert calls == [5]


def test_weather_route_returns_weather_dashboard_data(monkeypatch):
    calls = []

    monkeypatch.setattr(
        server.data,
        "weather_dashboard",
        lambda: calls.append("weather") or {"available": True, "days": [], "hours": []},
    )

    response = server.app.test_client().get("/api/weather")

    assert response.status_code == 200
    assert response.get_json()["available"] is True
    assert calls == ["weather"]


def test_advisor_latest_route_returns_saved_report(monkeypatch):
    monkeypatch.setattr(
        advisor,
        "latest_report",
        lambda: {
            "ok": True,
            "schema": "advisor_chat_v1",
            "messages": [{"role": "assistant", "text": "Because."}],
        },
    )

    response = server.app.test_client().get("/api/advisor/latest")

    assert response.status_code == 200
    assert response.get_json()["messages"][0]["text"] == "Because."


def test_advisor_clear_route_empties_saved_chat(monkeypatch):
    calls = []

    monkeypatch.setattr(advisor, "clear_chat", lambda: calls.append("clear") or {"ok": True, "messages": []})

    response = server.app.test_client().post("/api/advisor/clear")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "messages": []}
    assert calls == ["clear"]


def test_advisor_delete_exchange_route_removes_turn(monkeypatch):
    calls = []

    monkeypatch.setattr(
        advisor,
        "delete_exchange",
        lambda index: calls.append(index) or {"ok": True, "messages": [{"role": "assistant", "text": "left"}]},
    )

    response = server.app.test_client().post("/api/advisor/delete-exchange", json={"index": 2})

    assert response.status_code == 200
    assert response.get_json()["messages"][0]["text"] == "left"
    assert calls == [2]


def test_advisor_delete_exchange_route_rejects_bad_index(monkeypatch):
    def fail(index):
        raise IndexError("message index out of range")

    monkeypatch.setattr(advisor, "delete_exchange", fail)

    response = server.app.test_client().post("/api/advisor/delete-exchange", json={"index": 99})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert "message index out of range" in response.get_json()["error"]
