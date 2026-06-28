"""Unit tests for the advisor's on-demand history retrieval.

These cover _parse_need_history (pure logic, no I/O): only a reply that *is* a
NEED_HISTORY directive triggers retrieval, only days that actually exist are honored,
commas and A..B ranges work, and the day count is capped.
"""
from frontend import advisor
from frontend.advisor import _parse_need_history, _day_summary

AVAIL = ["2026-06-10", "2026-06-11", "2026-06-12", "2026-06-13", "2026-06-14"]


def test_normal_answer_is_not_a_directive():
    assert _parse_need_history("The optimizer sold at 15:00 because...", AVAIL, 14) == []


def test_mention_mid_text_does_not_trigger():
    # Must STAND ALONE (start of reply); a stray mention must not pull data.
    txt = "I could answer better with more data, e.g. NEED_HISTORY: 2026-06-11"
    assert _parse_need_history(txt, AVAIL, 14) == []


def test_single_day():
    assert _parse_need_history("NEED_HISTORY: 2026-06-11", AVAIL, 14) == ["2026-06-11"]


def test_comma_list_filters_nonexistent():
    out = _parse_need_history("NEED_HISTORY: 2026-06-11, 2026-06-14, 1999-01-01", AVAIL, 14)
    assert out == ["2026-06-11", "2026-06-14"]


def test_inclusive_range():
    out = _parse_need_history("NEED_HISTORY: 2026-06-10..2026-06-12", AVAIL, 14)
    assert out == ["2026-06-10", "2026-06-11", "2026-06-12"]


def test_reversed_range_normalised():
    out = _parse_need_history("NEED_HISTORY: 2026-06-12..2026-06-10", AVAIL, 14)
    assert out == ["2026-06-10", "2026-06-11", "2026-06-12"]


def test_max_days_cap():
    out = _parse_need_history("NEED_HISTORY: 2026-06-10..2026-06-14", AVAIL, max_days=2)
    assert out == ["2026-06-10", "2026-06-11"]


def test_case_insensitive_and_whitespace():
    assert _parse_need_history("  need_history:   2026-06-13  ", AVAIL, 14) == ["2026-06-13"]


def test_empty_and_garbage():
    assert _parse_need_history("", AVAIL, 14) == []
    assert _parse_need_history("NEED_HISTORY:", AVAIL, 14) == []
    assert _parse_need_history("NEED_HISTORY: not-a-date", AVAIL, 14) == []


def test_day_summary_normalises_pv_load_units():
    # pv_forecast_today_kwh is stored in Wh (the known mislabel); pv_actual_today_kwh
    # is real kWh. The summary must put them on the same (kWh) footing.
    recs = [{
        "kind": "cycle", "control_action": "IDLE",
        "pv_forecast_today_kwh": 44592.5,   # Wh -> 44.59 kWh
        "pv_actual_today_kwh": 40.1,        # already kWh
        "load_forecast_today_wh": 31959.0,  # Wh -> 31.96 kWh
        "load_actual_today_wh": 30000.0,    # Wh -> 30.0 kWh
        "day_import_kwh": 7.5, "realized_net_eur": 0.9,
    }]
    s = _day_summary(recs)
    assert s["pv_forecast_kwh"] == 44.59
    assert s["pv_actual_kwh"] == 40.1
    assert s["pv_forecast_err_kwh"] == round(40.1 - 44.59, 2)   # actual - forecast
    assert s["load_forecast_kwh"] == 31.96
    assert s["load_actual_kwh"] == 30.0


def test_load_days_preserves_every_requested_summary_when_detail_budget_is_exhausted(monkeypatch):
    def fake_read_day(day):
        return [{
            "kind": "cycle",
            "ts": f"{day.isoformat()}T23:45:00+02:00",
            "control_action": "IDLE",
            "load_actual_today_wh": int(day.day) * 1000,
            "load_w": 1200,
        }]

    monkeypatch.setattr(advisor, "_read_day", fake_read_day)

    loaded = advisor._load_days(
        ["2026-06-10", "2026-06-11", "2026-06-12"],
        {"ADVISOR_RETRIEVAL_MAX_CHARS": "1"},
    )

    assert sorted(loaded) == ["2026-06-10", "2026-06-11", "2026-06-12"]
    assert loaded["2026-06-10"]["summary"]["load_actual_kwh"] == 10.0
    assert loaded["2026-06-11"]["summary"]["load_actual_kwh"] == 11.0
    assert loaded["2026-06-12"]["summary"]["load_actual_kwh"] == 12.0
    assert "cycles" not in loaded["2026-06-10"]
    assert "retrieval budget reached" in loaded["2026-06-10"]["note"]


def test_inline_daily_load_summaries_can_satisfy_history_request():
    user_prompt = """TASK

=== DATA (JSON) ===
{"performance": {"daily_summaries": {
  "2026-06-23": {"load_actual_kwh": 34.75},
  "2026-06-24": {"load_actual_kwh": 40.68}
}}}
=== END DATA ==="""

    assert advisor._inline_data_can_satisfy_history_request(
        "show AC load consumption totals per day for the last 2 days",
        ["2026-06-23", "2026-06-24"],
        user_prompt,
    )


def test_question_prompt_tells_model_to_prefer_chat_prompt_and_daily_summaries(monkeypatch):
    monkeypatch.setattr(advisor, "_tunables", lambda conf: [])
    monkeypatch.setattr(advisor, "_plan_excerpt", lambda: {})
    monkeypatch.setattr(advisor, "_live_excerpt", lambda: None)
    monkeypatch.setattr(advisor, "_history_manifest", lambda: {"available_days": []})
    monkeypatch.setattr(advisor, "_gather", lambda days, detail_days=2: {
        "daily_summaries": {"2026-06-24": {"load_actual_kwh": 40.68}},
        "recent_detail": {},
    })

    _, user = advisor._build_messages(
        "can you show total AC load consumption totals per day",
        {},
        conversation_context="User [earlier]: 2026-06-24 load was 40.68 kWh",
    )

    assert "Use the user's prompt, conversation_context, and inline data first" in user
    assert "`performance.daily_summaries[date].load_actual_kwh`" in user
    assert "NEED_HISTORY only when" in user


def test_answer_with_retrieval_reasks_from_inline_summary_without_loading_files(monkeypatch):
    user_prompt = """TASK

=== DATA (JSON) ===
{"performance": {"daily_summaries": {
  "2026-06-23": {"load_actual_kwh": 34.75},
  "2026-06-24": {"load_actual_kwh": 40.68}
}}}
=== END DATA ==="""
    calls = []

    monkeypatch.setattr(advisor, "_history_manifest", lambda: {
        "available_days": ["2026-06-23", "2026-06-24"],
    })
    monkeypatch.setattr(advisor, "_build_messages", lambda question, conf, conversation_context=None: (
        "system",
        user_prompt,
    ))

    def fake_stream_for(mode, system, user, model, conf):
        calls.append(user)
        if len(calls) == 1:
            yield {"type": "delta", "text": "NEED_HISTORY: 2026-06-23, 2026-06-24"}
        else:
            assert "performance.daily_summaries" in user
            assert "already contains the requested daily summary values" in user
            yield {"type": "delta", "text": "2026-06-23: 34.75 kWh"}

    def fail_load_days(date_strs, conf):
        raise AssertionError("should not retrieve files when inline summaries answer")

    monkeypatch.setattr(advisor, "_stream_for", fake_stream_for)
    monkeypatch.setattr(advisor, "_load_days", fail_load_days)

    events = list(advisor._answer_with_retrieval(
        "show AC load consumption totals per day",
        {},
        "cli",
        "sonnet",
    ))

    assert any(ev.get("text") == "2026-06-23: 34.75 kWh" for ev in events)
    assert len(calls) == 2
