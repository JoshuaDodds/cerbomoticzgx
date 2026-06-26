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
            "mode": "question",
            "question": "Why?",
            "report": "Because.",
            "model": "sonnet",
            "auth": "cli",
            "generated_at": "2026-06-26T12:00:00+02:00",
        },
    )

    response = server.app.test_client().get("/api/advisor/latest")

    assert response.status_code == 200
    assert response.get_json()["report"] == "Because."
