import json

from frontend import advisor


def _use_latest_path(monkeypatch, tmp_path):
    path = tmp_path / "data" / "advisor_latest.json"
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(path))
    return path


def test_latest_report_returns_empty_when_no_report_exists(monkeypatch, tmp_path):
    _use_latest_path(monkeypatch, tmp_path)

    assert advisor.latest_report() == {
        "ok": False,
        "schema": "advisor_chat_v1",
        "messages": [],
    }


def test_run_stream_appends_chat_turn_and_persists_completed_output(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "ok": True,
        "schema": "advisor_chat_v1",
        "messages": [
            {"role": "user", "text": "Earlier?", "created_at": "2026-06-26T08:00:00+02:00"},
            {"role": "assistant", "text": "Earlier answer.", "created_at": "2026-06-26T08:01:00+02:00"},
        ],
    }), encoding="utf-8")
    captured_contexts = []

    monkeypatch.setattr(advisor, "_conf", lambda: {})
    monkeypatch.setattr(advisor, "_auth_mode", lambda conf: "cli")
    monkeypatch.setattr(advisor, "_model", lambda conf, mode: "sonnet")
    monkeypatch.setattr(advisor, "_auth_log_event", lambda mode, conf: {"type": "log", "msg": "auth=cli"})

    def fake_build_messages(question, conf, conversation_context=None):
        captured_contexts.append(conversation_context)
        return "system", "user"

    monkeypatch.setattr(advisor, "_build_messages", fake_build_messages)

    def fake_stream_for(mode, system, user, model, conf):
        yield {"type": "delta", "text": "fresh "}
        yield {"type": "delta", "text": "answer"}

    monkeypatch.setattr(advisor, "_stream_for", fake_stream_for)

    events = list(advisor.run_stream("Why?"))

    assert "Earlier?" in captured_contexts[0]
    assert "Earlier answer." in captured_contexts[0]
    assert events[-1]["type"] == "done"
    saved = advisor.latest_report()
    assert saved["ok"] is True
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant", "user", "assistant"]
    assert saved["messages"][-2]["text"] == "Why?"
    assert saved["messages"][-2]["mode"] == "question"
    assert saved["messages"][-1]["text"] == "fresh answer"
    assert saved["messages"][-1]["model"] == "sonnet"
    assert saved["messages"][-1]["auth"] == "cli"
    assert "created_at" in saved["messages"][-1]


def test_run_stream_appends_error_response(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "ok": True,
        "schema": "advisor_chat_v1",
        "messages": [{"role": "assistant", "text": "old"}],
    }), encoding="utf-8")

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
    assert saved["messages"][-2]["role"] == "user"
    assert saved["messages"][-2]["text"] == "Run daily review"
    assert saved["messages"][-1]["role"] == "assistant"
    assert saved["messages"][-1]["ok"] is False
    assert saved["messages"][-1]["error"].startswith("No Claude credentials configured")


def test_clear_chat_removes_persistent_session(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ok": True, "messages": [{"role": "assistant", "text": "old"}]}), encoding="utf-8")

    result = advisor.clear_chat()

    assert result == {"ok": True, "schema": "advisor_chat_v1", "messages": []}
    assert not path.exists()


def test_delete_exchange_removes_user_and_following_assistant(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "ok": True,
        "schema": "advisor_chat_v1",
        "messages": [
            {"role": "user", "text": "First?"},
            {"role": "assistant", "text": "First answer."},
            {"role": "user", "text": "Second?"},
            {"role": "assistant", "text": "Second answer."},
        ],
    }), encoding="utf-8")

    result = advisor.delete_exchange(2)

    assert result["ok"] is True
    assert [m["text"] for m in result["messages"]] == ["First?", "First answer."]
    saved = advisor.latest_report()
    assert [m["text"] for m in saved["messages"]] == ["First?", "First answer."]


def test_delete_exchange_removes_assistant_with_previous_user(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "ok": True,
        "schema": "advisor_chat_v1",
        "messages": [
            {"role": "user", "text": "Question?"},
            {"role": "assistant", "text": "Answer."},
            {"role": "assistant", "text": "Standalone note."},
        ],
    }), encoding="utf-8")

    result = advisor.delete_exchange(1)

    assert result["ok"] is True
    assert [m["text"] for m in result["messages"]] == ["Standalone note."]


def test_delete_exchange_rejects_bad_index(monkeypatch, tmp_path):
    path = _use_latest_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "ok": True,
        "schema": "advisor_chat_v1",
        "messages": [{"role": "assistant", "text": "Only"}],
    }), encoding="utf-8")

    try:
        advisor.delete_exchange(3)
    except IndexError as e:
        assert "message index out of range" in str(e)
    else:
        raise AssertionError("expected IndexError")
