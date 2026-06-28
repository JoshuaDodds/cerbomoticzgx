from frontend import server
from frontend import advisor


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
