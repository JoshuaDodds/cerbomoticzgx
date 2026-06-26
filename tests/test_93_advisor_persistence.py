import json

from frontend import advisor


def _use_latest_path(monkeypatch, tmp_path):
    path = tmp_path / "data" / "advisor_latest.json"
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(path))
    return path


def test_latest_report_returns_empty_when_no_report_exists(monkeypatch, tmp_path):
    _use_latest_path(monkeypatch, tmp_path)

    assert advisor.latest_report() == {"ok": False, "report": ""}


def test_run_stream_clears_previous_report_and_persists_completed_output(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ok": True, "report": "old"}), encoding="utf-8")
    exists_when_model_starts = []

    monkeypatch.setattr(advisor, "_conf", lambda: {})
    monkeypatch.setattr(advisor, "_auth_mode", lambda conf: "cli")
    monkeypatch.setattr(advisor, "_model", lambda conf, mode: "sonnet")
    monkeypatch.setattr(advisor, "_auth_log_event", lambda mode, conf: {"type": "log", "msg": "auth=cli"})
    monkeypatch.setattr(advisor, "_build_messages", lambda question, conf: ("system", "user"))

    def fake_stream_for(mode, system, user, model, conf):
        exists_when_model_starts.append(path.exists())
        yield {"type": "delta", "text": "fresh "}
        yield {"type": "delta", "text": "answer"}

    monkeypatch.setattr(advisor, "_stream_for", fake_stream_for)

    events = list(advisor.run_stream("Why?"))

    assert exists_when_model_starts == [False]
    assert events[-1]["type"] == "done"
    saved = advisor.latest_report()
    assert saved["ok"] is True
    assert saved["mode"] == "question"
    assert saved["question"] == "Why?"
    assert saved["report"] == "fresh answer"
    assert saved["model"] == "sonnet"
    assert saved["auth"] == "cli"
    assert "generated_at" in saved


def test_run_stream_replaces_previous_report_with_error_record(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ok": True, "report": "old"}), encoding="utf-8")

    monkeypatch.setattr(advisor, "_conf", lambda: {})
    monkeypatch.setattr(advisor, "_auth_mode", lambda conf: None)

    events = list(advisor.run_stream(None))

    assert events == [{
        "type": "error",
        "error": (
            "No Claude credentials configured. If `claude` is already logged in on "
            "this host, set ADVISOR_AUTH=cli. Otherwise set CLAUDE_CODE_OAUTH_TOKEN "
            "in .secrets, or ANTHROPIC_API_KEY for API use."
        ),
    }]
    saved = advisor.latest_report()
    assert saved["ok"] is False
    assert saved["mode"] == "review"
    assert saved["report"] == ""
    assert saved["error"].startswith("No Claude credentials configured")
