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


def test_replan_route_reports_optimizer_already_running(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "lib.energy_broker",
        types.SimpleNamespace(run_ai_optimizer=lambda: False),
    )

    response = server.app.test_client().post("/api/replan")

    assert response.status_code == 409
    body = response.get_json()
    assert body["ok"] is False
    assert body["skipped"] is True


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


def test_manual_ev_stop_sets_intent_off_and_latched_force_stop(monkeypatch):
    import lib.global_state as global_state

    calls = []
    published = []

    class FakeState:
        def set(self, key, value):
            calls.append((key, value))

    monkeypatch.setattr(global_state, "GlobalStateClient", FakeState)
    monkeypatch.setattr(server, "publish_message",
                        lambda *args, **kwargs: published.append((args, kwargs)))

    response = server.app.test_client().post(
        "/api/control/ev-charge", json={"enabled": False})

    assert response.status_code == 200
    assert calls == [
        ("ev_charge_requested", False),
        ("vehicle_stop_requested", True),
    ]
    assert published


def test_refresh_vehicle_route_sets_one_shot_state_flag(monkeypatch):
    import lib.global_state as global_state

    calls = []

    class FakeState:
        def set(self, key, value):
            calls.append((key, value))

    monkeypatch.setattr(global_state, "GlobalStateClient", FakeState)

    response = server.app.test_client().post("/api/control/refresh-vehicle")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert calls == [("vehicle_refresh_requested", True)]


def test_refresh_vehicle_route_reports_failure(monkeypatch):
    import lib.global_state as global_state

    class FailingState:
        def set(self, key, value):
            raise RuntimeError("db locked")

    monkeypatch.setattr(global_state, "GlobalStateClient", FailingState)

    response = server.app.test_client().post("/api/control/refresh-vehicle")

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "db locked" in body["error"]


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


def test_ev_smart_charge_get_is_available_when_control_module_is_missing(monkeypatch):
    monkeypatch.setattr(
        server.data,
        "ev_smart_charge_dashboard",
        lambda: {"available": False, "message": "Smart charging is unavailable."},
    )

    response = server.app.test_client().get("/api/ev/smart-charge")

    assert response.status_code == 200
    assert response.get_json()["available"] is False


def test_ev_smart_charge_put_validates_and_saves_one_job(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "_save_ev_smart_charge_job",
        lambda payload: calls.append(payload) or {"id": "job-1", **payload},
        raising=False,
    )

    response = server.app.test_client().put(
        "/api/ev/smart-charge",
        json={"target_soc": 80, "ready_by": "2099-07-21T10:00:00+02:00"},
    )

    assert response.status_code == 200
    assert response.get_json()["job"]["target_soc"] == 80
    assert calls == [{"target_soc": 80, "ready_by": "2099-07-21T10:00:00+02:00"}]


def test_ev_smart_charge_put_rejects_invalid_target_or_naive_deadline(monkeypatch):
    client = server.app.test_client()

    target = client.put(
        "/api/ev/smart-charge",
        json={"target_soc": 101, "ready_by": "2099-07-21T10:00:00+02:00"},
    )
    deadline = client.put(
        "/api/ev/smart-charge",
        json={"target_soc": 80, "ready_by": "2099-07-21T10:00"},
    )
    fractional = client.put(
        "/api/ev/smart-charge",
        json={"target_soc": 80.5, "ready_by": "2099-07-21T10:00:00+02:00"},
    )
    below_tesla_limit = client.put(
        "/api/ev/smart-charge",
        json={"target_soc": 49, "ready_by": "2099-07-21T10:00:00+02:00"},
    )

    assert target.status_code == 400
    assert deadline.status_code == 400
    assert fractional.status_code == 400
    assert below_tesla_limit.status_code == 400


def test_ev_smart_charge_delete_and_actions_delegate_to_control_module(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "_delete_ev_smart_charge_job",
        lambda: calls.append("delete") or None,
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_act_on_ev_smart_charge_job",
        lambda action: calls.append(action) or {"status": action},
        raising=False,
    )
    client = server.app.test_client()

    deleted = client.delete("/api/ev/smart-charge")
    paused = client.post("/api/ev/smart-charge/action", json={"action": "pause"})
    bad = client.post("/api/ev/smart-charge/action", json={"action": "explode"})

    assert deleted.status_code == 200
    assert paused.status_code == 200
    assert bad.status_code == 400
    assert calls == ["delete", "pause"]


def test_ev_smart_charge_save_uses_canonical_job_factory_and_live_soc(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        create_job=lambda **kwargs: calls.append(("create", kwargs)) or {"id": "canonical", **kwargs},
        save_job=lambda job, path=None: calls.append(("save", job, path)) or job,
    )
    monkeypatch.setitem(sys.modules, "lib.ev_smart_charge", fake)
    monkeypatch.setattr(server.live, "snapshot", lambda: {"veh_soc": 36})
    monkeypatch.setattr(server.data, "_env", lambda: {"EV_SMART_CHARGE_JOB_PATH": "/tmp/custom-job.json"})

    result = server._save_ev_smart_charge_job({
        "target_soc": 80,
        "ready_by": "2099-07-21T10:00:00+02:00",
    })

    assert result["id"] == "canonical"
    assert calls[0][0] == "create"
    assert calls[0][1]["current_soc"] == 36.0
    assert calls[1][0] == "save"
    assert calls[1][2] == "/tmp/custom-job.json"


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


def test_logs_route_returns_buffered_lines(monkeypatch):
    import lib.log_buffer as log_buffer

    class FakeHandler:
        def snapshot(self):
            return [(1, "line one"), (2, "line two")]

    monkeypatch.setattr(log_buffer, "get_handler", lambda: FakeHandler())

    response = server.app.test_client().get("/api/logs")

    assert response.status_code == 200
    assert response.get_json() == {"lines": ["line one", "line two"]}
