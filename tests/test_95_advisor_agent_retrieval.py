"""Advisor open-question retrieval and durable-conversation regressions."""

import json
import sys
from types import SimpleNamespace
from pathlib import Path

from frontend import advisor


def test_api_sync_and_stream_disable_adaptive_thinking(monkeypatch):
    """Sonnet 5 must reserve the API output budget for the visible answer."""
    calls = {}

    class FakeStream:
        text_stream = iter(["streamed answer"])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def get_final_message():
            return SimpleNamespace(stop_reason="end_turn")

    class FakeMessages:
        @staticmethod
        def create(**kwargs):
            calls["create"] = kwargs
            return SimpleNamespace(
                content=[SimpleNamespace(text="answer")],
                usage=None,
                stop_reason="end_turn",
            )

        @staticmethod
        def stream(**kwargs):
            calls["stream"] = kwargs
            return FakeStream()

    class FakeAnthropic:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.messages = FakeMessages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(Anthropic=FakeAnthropic),
    )

    result = advisor._call_claude_api(
        "system", "user", "claude-sonnet-5", "test-key", {}
    )
    events = list(advisor._stream_api(
        "system", "user", "claude-sonnet-5", "test-key", {}
    ))

    assert result["ok"] is True
    assert events == [{"type": "delta", "text": "streamed answer"}]
    for path in ("create", "stream"):
        assert calls[path]["thinking"] == {"type": "disabled"}
        assert calls[path]["max_tokens"] == advisor.DEFAULT_MAX_OUTPUT_TOKENS


def _stub_prompt_inputs(monkeypatch):
    monkeypatch.setattr(advisor, "_tunables", lambda conf: [])
    monkeypatch.setattr(advisor, "_plan_excerpt", lambda: {})
    monkeypatch.setattr(advisor, "_live_excerpt", lambda: None)
    monkeypatch.setattr(advisor, "_history_manifest", lambda: {"available_days": []})
    monkeypatch.setattr(advisor, "_gather", lambda days, detail_days=2: {
        "daily_summaries": {},
        "recent_detail": {},
    })


def test_daily_review_is_not_contaminated_by_chat_history(monkeypatch):
    _stub_prompt_inputs(monkeypatch)

    _, prompt = advisor._build_messages(
        None,
        {"ADVISOR_MAX_INPUT_CHARS": "8000"},
        conversation_context="An unrelated old conversation about the EV.",
    )

    payload = advisor._prompt_data_payload(prompt)
    assert "conversation_context" not in payload
    assert payload["payload_meta"]["conversation_chars"] == 0


def test_conversation_memory_preserves_recent_turns_and_summarizes_older_ones():
    chat = advisor._empty_chat(ok=True)
    for index in range(8):
        chat["messages"].extend([
            {"role": "user", "created_at": f"2026-07-2{index}T10:00:00+02:00",
             "text": f"Question {index}: " + ("q" * 700)},
            {"role": "assistant", "created_at": f"2026-07-2{index}T10:01:00+02:00",
             "text": f"Answer {index}: " + ("a" * 700)},
        ])

    memory = advisor._conversation_memory(chat, recent_turns=2, summary_chars=3000)

    assert "Earlier conversation summary" in memory
    assert "Question 0" in memory
    assert "Recent exact turns" in memory
    assert "Question 6" in memory
    assert "Answer 7" in memory
    assert len(memory) <= 7000


def test_delete_exchange_rebuilds_conversation_summary(monkeypatch, tmp_path):
    path = tmp_path / "advisor.json"
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(path))
    chat = advisor._empty_chat(ok=True)
    for index in range(4):
        chat["messages"].extend([
            {"role": "user", "created_at": "now", "text": f"Question {index}"},
            {"role": "assistant", "created_at": "now", "text": f"Answer {index}"},
        ])
    advisor._save_chat(chat)

    updated = advisor.delete_exchange(0)

    assert "Question 0" not in updated.get("conversation_summary", "")
    assert "Question 1" in updated.get("conversation_summary", "")


def test_structured_requests_are_bounded_and_legacy_directives_remain_supported():
    request = json.dumps({
        "requests": [
            {"tool": "current_state", "args": {}},
            {"tool": "recent_logs", "args": {"query": "AI_ESS"}},
            {"tool": "read_source", "args": {"path": "frontend/advisor.py"}},
            {"tool": "read_source", "args": {"path": "lib/energy_broker.py"}},
        ]
    })

    parsed = advisor._parse_tool_requests(request, {}, max_requests=3)
    legacy = advisor._parse_tool_requests("NEED_CONFIG: SAFE, SECRET", {
        "tunables": [{"key": "SAFE"}],
    })

    assert [item["tool"] for item in parsed] == [
        "current_state", "recent_logs", "read_source",
    ]
    assert legacy == [{"tool": "config_metadata", "args": {"keys": ["SAFE"]}}]


def test_tool_result_has_strict_serialized_cap_even_with_huge_source_locator():
    bounded = advisor._bound_tool_result({
        "tool": "read_source",
        "ok": True,
        "data": "x" * 100000,
        "source": {"kind": "source", "locator": "p" * 100000},
    }, 32000)
    assert len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))) <= 32000
    assert bounded["truncated"] is True


def test_source_reader_rejects_secrets_env_traversal_and_symlinks(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "frontend").mkdir()
    (repo / "frontend" / "safe.py").write_text("SAFE = True\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "secret.json").write_text('{"token":"never"}', encoding="utf-8")
    (repo / "frontend" / "linked").symlink_to(repo / "data", target_is_directory=True)
    (repo / ".env").write_text("PASSWORD=no\n", encoding="utf-8")
    (repo / "frontend" / "leak.py").symlink_to(repo / ".env")
    monkeypatch.setattr(advisor, "_REPO_ROOT", str(repo))

    assert advisor._tool_read_source({"path": "frontend/safe.py"})["ok"] is True
    for path in (
        ".env", "../outside.py", "frontend/leak.py",
        "frontend/linked/secret.json", "/etc/passwd", ".secrets",
    ):
        result = advisor._tool_read_source({"path": path})
        assert result["ok"] is False
        assert "PASSWORD" not in json.dumps(result)


def test_runtime_artifacts_are_explicitly_allow_listed(monkeypatch, tmp_path):
    allowed = tmp_path / "plan.json"
    allowed.write_text(
        '{"action":"BUY","access_token":"never-model","note":"password: note-secret"}',
        encoding="utf-8",
    )
    secret = tmp_path / "secret.json"
    secret.write_text('{"token":"never"}', encoding="utf-8")
    monkeypatch.setattr(advisor, "_RUNTIME_ARTIFACTS", {"ess_plan": str(allowed)})

    good = advisor._tool_runtime_artifact({"name": "ess_plan"})
    bad = advisor._tool_runtime_artifact({"name": str(secret)})

    assert good["ok"] is True
    assert good["data"]["action"] == "BUY"
    assert "never-model" not in json.dumps(good)
    assert "note-secret" not in json.dumps(good)
    assert bad["ok"] is False
    assert "never" not in json.dumps(bad)


def test_recent_logs_redact_credentials_and_apply_time_filter(monkeypatch):
    class Handler:
        def snapshot(self):
            return [
                (1, "2026-07-25 09:59:59 app: old"),
                (2, "2026-07-25 10:00:00 app: access_token=super-secret"),
                (3, '2026-07-25 10:00:01 app: {"access_token":"json-secret"}'),
                (4, "2026-07-25 10:00:02 app: Authorization: Bearer bearer-secret"),
                (5, "2026-07-25 10:00:03 app: X-Authorization: Token header-secret"),
                (6, "2026-07-25 10:00:04 app: password: colon-secret"),
                (7, "2026-07-25 10:01:00 app: safe"),
            ]

    monkeypatch.setattr("lib.log_buffer.get_handler", lambda: Handler())

    result = advisor._tool_recent_logs({
        "since": "2026-07-25T10:00:00",
        "until": "2026-07-25T10:00:30",
    })

    assert result["ok"] is True
    assert len(result["data"]["lines"]) == 5
    serialized = json.dumps(result)
    for secret in (
        "super-secret", "json-secret", "bearer-secret", "header-secret", "colon-secret",
    ):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_evidence_redaction_covers_generic_and_camel_case_credentials():
    evidence = {
        "token": "one",
        "client_secret": "two",
        "private_key": "three",
        "apiKey": "four",
        "nested": ["token=five", "clientSecret: six", "privateKey=seven"],
    }
    redacted = json.dumps(advisor._redact_evidence(evidence))
    for secret in ("one", "two", "three", "four", "five", "six", "seven"):
        assert secret not in redacted


def test_multi_round_retrieval_emits_sources_and_stops_at_final_answer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        advisor,
        "_build_messages",
        lambda question, conf, conversation_context=None: ("system", "initial prompt"),
    )
    monkeypatch.setattr(advisor, "_tunables", lambda conf: [])
    monkeypatch.setattr(advisor, "_history_manifest", lambda: {"available_days": []})

    replies = [
        json.dumps({"requests": [{"tool": "current_state", "args": {}}]}),
        json.dumps({"requests": [{"tool": "read_source",
                                  "args": {"path": "frontend/advisor.py",
                                           "start_line": 1, "line_count": 2}}]}),
        "The answer is grounded in the current state and advisor source.",
    ]

    def fake_stream(mode, system, user, model, conf):
        calls.append(user)
        yield {"type": "delta", "text": replies[len(calls) - 1]}

    def fake_execute(request, context):
        return {
            "tool": request["tool"],
            "ok": True,
            "data": {"sample": True},
            "source": {"kind": "runtime", "locator": request["tool"],
                       "observed_at": "2026-07-25T10:00:00+02:00"},
            "truncated": False,
        }

    monkeypatch.setattr(advisor, "_stream_for", fake_stream)
    monkeypatch.setattr(advisor, "_execute_tool_request", fake_execute)

    events = list(advisor._answer_with_retrieval(
        "Inspect this behavior", {}, "cli", "sonnet", conversation_context="prior turn"
    ))

    assert len(calls) == 3
    assert any(event.get("type") == "sources" for event in events)
    assert any("grounded" in event.get("text", "") for event in events)
    assert "UNTRUSTED READ-ONLY EVIDENCE" in calls[1]


def test_malformed_request_is_repaired_not_rendered(monkeypatch):
    calls = []
    monkeypatch.setattr(
        advisor,
        "_build_messages",
        lambda question, conf, conversation_context=None: ("system", "initial"),
    )
    monkeypatch.setattr(advisor, "_tunables", lambda conf: [])
    monkeypatch.setattr(advisor, "_history_manifest", lambda: {"available_days": []})

    def fake_stream(mode, system, user, model, conf):
        calls.append(user)
        yield {
            "type": "delta",
            "text": "{requests:[{tool:'read_source'}]}" if len(calls) == 1 else "Safe answer.",
        }

    monkeypatch.setattr(advisor, "_stream_for", fake_stream)
    events = list(advisor._answer_with_retrieval("inspect", {}, "cli", "sonnet"))
    answer = "".join(event.get("text", "") for event in events)
    assert answer == "Safe answer."
    assert "invalid read-only retrieval request" in json.dumps(events)


def test_retrieval_loop_obeys_round_and_total_character_limits(monkeypatch):
    calls = []
    monkeypatch.setattr(
        advisor,
        "_build_messages",
        lambda question, conf, conversation_context=None: ("system", "initial"),
    )
    monkeypatch.setattr(advisor, "_tunables", lambda conf: [])
    monkeypatch.setattr(advisor, "_history_manifest", lambda: {"available_days": []})

    def fake_stream(mode, system, user, model, conf):
        calls.append(user)
        yield {"type": "delta", "text": json.dumps({
            "requests": [{"tool": "current_state", "args": {}}]
        })}

    monkeypatch.setattr(advisor, "_stream_for", fake_stream)
    monkeypatch.setattr(advisor, "_execute_tool_request", lambda request, context: {
        "tool": "current_state", "ok": True, "data": "x" * 50000,
        "source": {"kind": "runtime", "locator": "current_state"},
        "truncated": False,
    })

    events = list(advisor._answer_with_retrieval(
        "keep fetching", {
            "ADVISOR_RETRIEVAL_MAX_CHARS": "40000",
        }, "cli", "sonnet",
    ))

    assert len(calls) == 5  # four retrieval rounds plus a forced final-answer pass
    assert all(len(call) < 50000 for call in calls)
    assert any("retrieval limit" in call.lower() for call in calls[-1:])


def test_answer_message_can_persist_sources(monkeypatch, tmp_path):
    path = tmp_path / "advisor.json"
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(path))
    chat = advisor._empty_chat()
    sources = [{"tool": "read_source", "locator": "frontend/advisor.py:1-20"}]

    advisor._append_assistant_message(
        chat,
        text="Grounded answer",
        created_at="2026-07-25T10:00:00+02:00",
        model="sonnet",
        auth="cli",
        mode="question",
        sources=sources,
    )
    advisor._save_chat(chat)

    assert advisor.latest_report()["messages"][0]["sources"] == sources


def test_sonnet_5_is_api_default_and_uses_adaptive_cli_thinking():
    assert advisor._model({}, "api") == "claude-sonnet-5"
    assert advisor._model({"ADVISOR_MODEL": "claude-sonnet-4-6"}, "api") == \
        "claude-sonnet-4-6"

    adaptive_env = {"MAX_THINKING_TOKENS": "123"}
    advisor._configure_cli_thinking_env(
        adaptive_env, {"ADVISOR_MAX_THINKING_TOKENS": "4"}, "claude-sonnet-5"
    )
    assert "MAX_THINKING_TOKENS" not in adaptive_env

    legacy_env = {}
    advisor._configure_cli_thinking_env(
        legacy_env, {"ADVISOR_MAX_THINKING_TOKENS": "4"}, "claude-sonnet-4-6"
    )
    assert legacy_env["MAX_THINKING_TOKENS"] == "4"
    assert advisor._uses_adaptive_thinking("claude-opus-4-8") is True
    assert advisor._api_max_output_tokens({}, "claude-sonnet-5") == 4096
    assert advisor._api_max_output_tokens(
        {"ADVISOR_MAX_OUTPUT_TOKENS": "999999"}, "claude-sonnet-5"
    ) == 4096


def test_secrets_overlay_env_for_advisor_credentials(monkeypatch):
    monkeypatch.setattr(advisor, "env_path", lambda: "/tmp/app.env")
    monkeypatch.setattr(advisor, "secrets_path", lambda: "/tmp/app.secrets")
    monkeypatch.setattr(
        advisor,
        "dotenv_values",
        lambda path: (
            {"CLAUDE_CODE_OAUTH_TOKEN": "stale-env", "SAFE": "env"}
            if path.endswith(".env")
            else {"CLAUDE_CODE_OAUTH_TOKEN": "fresh-secret"}
        ),
    )
    conf = advisor._conf()
    assert conf["CLAUDE_CODE_OAUTH_TOKEN"] == "fresh-secret"
    assert conf["SAFE"] == "env"


def test_stream_persists_sanitized_run_details_and_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(tmp_path / "advisor.json"))
    monkeypatch.setattr(advisor, "_conf", lambda: {})
    monkeypatch.setattr(advisor, "_auth_mode", lambda conf: "cli")
    monkeypatch.setattr(advisor, "_model", lambda conf, mode: "sonnet")
    monkeypatch.setattr(
        advisor,
        "_auth_log_event",
        lambda mode, conf: {
            "type": "log",
            "msg": "auth=cli · token=present (secret details)",
        },
    )

    def fake_answer(*args, **kwargs):
        yield {"type": "stage", "msg": "Read-only retrieval round 1."}
        yield {"type": "sources", "sources": [{
            "kind": "source",
            "label": "frontend/advisor.py:1-2",
            "ref": "frontend/advisor.py:1-2",
        }]}
        yield {"type": "thinking", "count": 3, "private": "do not persist"}
        yield {"type": "delta", "text": "Grounded."}

    monkeypatch.setattr(advisor, "_answer_with_retrieval", fake_answer)

    list(advisor.run_stream("Inspect it"))
    assistant = advisor.latest_report()["messages"][-1]

    assert assistant["sources"][0]["ref"] == "frontend/advisor.py:1-2"
    serialized = json.dumps(assistant["run_details"])
    assert "secret details" not in serialized
    assert "do not persist" not in serialized
    assert any(item["type"] == "retrieval" for item in assistant["run_details"])
    assert any(item["type"] == "completion" for item in assistant["run_details"])


def test_custom_cli_model_is_neutral_unless_explicit_and_requires_placeholder():
    assert advisor._model({}, "custom") is None
    assert advisor._model({"ADVISOR_MODEL": "gemini-2.5-pro"}, "custom") == \
        "gemini-2.5-pro"
    assert advisor._custom_model_error({
        "ADVISOR_CLI_CMD": "gemini -p {prompt}",
        "ADVISOR_MODEL": "gemini-2.5-pro",
    }) is not None
    assert advisor._custom_model_error({
        "ADVISOR_CLI_CMD": "gemini --model {model} -p {prompt}",
        "ADVISOR_MODEL": "gemini-2.5-pro",
    }) is None

    command, use_stdin, error = advisor._prepare_generic_cli_command(
        {
            "ADVISOR_CLI_CMD": "advisor-text-wrapper --model {model} -p {prompt}",
            "ADVISOR_MODEL": "gemini-2.5-pro",
            "ADVISOR_CLI_SAFE_EXECUTABLES": "advisor-text-wrapper",
        },
        "system + user prompt",
        "gemini-2.5-pro",
    )
    assert error is None
    assert command == [
        "advisor-text-wrapper", "--model", "gemini-2.5-pro",
        "-p", "system + user prompt",
    ]
    assert use_stdin is False


def test_built_in_claude_cli_is_hard_isolated_from_tools_mcp_and_sessions():
    command = advisor._claude_cli_command(
        "claude",
        ["--output-format", "stream-json"],
        "sonnet",
    )

    for required in (
        "--tools", "--strict-mcp-config", "--mcp-config",
        "--no-session-persistence", "--setting-sources", "--safe-mode",
        "--disable-slash-commands", "--no-chrome",
    ):
        assert required in command
    assert command[command.index("--tools") + 1] == ""
    assert json.loads(command[command.index("--mcp-config") + 1]) == {
        "mcpServers": {},
    }
    assert command[command.index("--setting-sources") + 1] == ""
    assert advisor._validated_cli_stream_args({
        "ADVISOR_CLI_STREAM_ARGS": "--output-format stream-json --tools default",
    })[1]


def test_cli_failure_retains_redacted_actionable_diagnostic(monkeypatch):
    class Pipe:
        def write(self, _value):
            return None

        def close(self):
            return None

    class Process:
        stdin = Pipe()
        stdout = ()

        @staticmethod
        def wait(timeout=None):
            return 1

        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(
        advisor,
        "_process_lines",
        lambda proc, timeout: iter((
            "Error: Invalid MCP configuration; token=do-not-store\n",
        )),
    )
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: Process())

    events = list(advisor._stream_cli("system", "user", "sonnet", None, {}))

    error = next(event["error"] for event in events if event["type"] == "error")
    assert "Invalid MCP configuration" in error
    assert "do-not-store" not in error
    assert "[REDACTED]" in error


def test_custom_agentic_clis_are_rejected_and_wrapper_must_be_explicitly_trusted():
    for command in (
        "claude --print {prompt}",
        "codex exec --sandbox read-only {prompt}",
        "gemini --approval-mode plan -p {prompt}",
    ):
        error = advisor._custom_cli_safety_error({"ADVISOR_CLI_CMD": command})
        assert error and "agentic CLI" in error

    assert advisor._custom_cli_safety_error({
        "ADVISOR_CLI_CMD": "advisor-text-wrapper {prompt}",
    })
    assert advisor._custom_cli_safety_error({
        "ADVISOR_CLI_CMD": "advisor-text-wrapper {prompt}",
        "ADVISOR_CLI_SAFE_EXECUTABLES": "advisor-text-wrapper",
    }) is None


def test_stream_emits_accepted_only_after_start_is_admitted(monkeypatch, tmp_path):
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(tmp_path / "advisor.json"))
    monkeypatch.setattr(advisor, "_conf", lambda: {})
    monkeypatch.setattr(advisor, "_auth_mode", lambda conf: "cli")
    monkeypatch.setattr(advisor, "_model", lambda conf, mode: "sonnet")
    monkeypatch.setattr(advisor, "_build_messages", lambda *args, **kwargs: ("sys", "usr"))
    monkeypatch.setattr(
        advisor,
        "_stream_for",
        lambda *args, **kwargs: iter([{"type": "delta", "text": "answer"}]),
    )

    events = list(advisor.run_stream(None))
    assert events[0]["type"] == "accepted"

    assert advisor._run_lock.acquire(blocking=False)
    try:
        rejected = list(advisor.run_stream(None))
    finally:
        advisor._run_lock.release()
    assert all(event.get("type") != "accepted" for event in rejected)


def test_stream_disconnect_persists_interrupted_assistant_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(tmp_path / "advisor.json"))
    monkeypatch.setattr(advisor, "_conf", lambda: {})
    monkeypatch.setattr(advisor, "_auth_mode", lambda conf: "cli")
    monkeypatch.setattr(advisor, "_model", lambda conf, mode: "sonnet")

    generator = advisor.run_stream("Why?")
    assert next(generator)["type"] == "accepted"
    generator.close()

    messages = advisor.latest_report()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["ok"] is False
    assert "interrupted" in messages[-1]["error"].lower()


def test_thinking_run_details_are_coalesced():
    details = []
    advisor._record_run_detail(details, {"type": "thinking", "count": 1})
    advisor._record_run_detail(details, {"type": "stage", "msg": "Working."})
    advisor._record_run_detail(details, {"type": "thinking", "count": 99, "done": True})

    thinking = [item for item in details if item["type"] == "thinking"]
    assert thinking == [{"type": "thinking", "count": 99, "done": True}]


def test_sync_run_persists_sanitized_details(monkeypatch, tmp_path):
    monkeypatch.setattr(advisor, "ADVISOR_LATEST_PATH", str(tmp_path / "advisor.json"))
    monkeypatch.setattr(advisor, "_conf", lambda: {})
    monkeypatch.setattr(advisor, "_auth_mode", lambda conf: "cli")
    monkeypatch.setattr(advisor, "_model", lambda conf, mode: "sonnet")
    monkeypatch.setattr(advisor, "_build_messages", lambda *args, **kwargs: ("sys", "usr"))
    monkeypatch.setattr(
        advisor,
        "_call_claude_cli",
        lambda *args, **kwargs: {"ok": True, "report": "Review complete.", "error": None},
    )

    result = advisor.run(None)
    assistant = advisor.latest_report()["messages"][-1]

    assert result["ok"] is True
    assert assistant["run_details"][0]["type"] == "stage"
    assert assistant["run_details"][-1]["type"] == "completion"


def test_oversized_conversation_keeps_summary_and_complete_newest_pair(monkeypatch):
    _stub_prompt_inputs(monkeypatch)
    chat = advisor._empty_chat(ok=True)
    for index in range(8):
        chat["messages"].extend([
            {"role": "user", "created_at": f"t{index}", "text": f"Question {index} " + "q" * 2000},
            {"role": "assistant", "created_at": f"a{index}", "text": f"Answer {index} " + "a" * 4000},
        ])
    context = advisor._conversation_context(chat)

    _, prompt = advisor._build_messages(
        "continue", {"ADVISOR_MAX_INPUT_CHARS": "16000"}, context
    )
    memory = advisor._prompt_data_payload(prompt)["conversation_context"]
    assert memory["earlier_summary"].startswith("U: Question 0")
    assert [turn["role"] for turn in memory["recent_exact_turns"][-2:]] == [
        "user", "assistant",
    ]
    assert memory["recent_exact_turns"][-2]["text"].startswith("Question 7")
    assert memory["recent_exact_turns"][-1]["text"].startswith("Answer 7")


def test_chat_mutations_reject_while_run_lock_is_held():
    assert advisor._run_lock.acquire(blocking=False)
    try:
        for mutation in (advisor.clear_chat, lambda: advisor.delete_exchange(0)):
            try:
                mutation()
            except advisor.AdvisorBusyError:
                pass
            else:
                raise AssertionError("mutation should be rejected during active run")
    finally:
        advisor._run_lock.release()
