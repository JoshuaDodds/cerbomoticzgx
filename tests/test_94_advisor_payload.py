"""Regression tests for the Advisor's bounded, structured prompt payload."""

from datetime import datetime, timedelta

import pytest

from frontend import advisor


def _stub_operational_inputs(monkeypatch, *, tunable_count=4, detail_rows=0):
    monkeypatch.setattr(
        advisor,
        "_tunables",
        lambda conf: [
            {
                "key": f"SETTING_{index}",
                "value": str(index),
                "type": "float",
                "group": "Test",
                "desc": "Long documentation that must not be repeated in every prompt. " * 20,
            }
            for index in range(tunable_count)
        ],
    )
    monkeypatch.setattr(
        advisor,
        "_plan_excerpt",
        lambda: {
            "generated_at": "2026-07-24T12:00:00+02:00",
            "battery_soc": 50,
            "current": {"control_action": "BUY", "price": 0.14},
            "today": {"grid_import_kwh": 4.2},
            "next_blocks": [
                {
                    "start": "2026-07-24T12:00:00+02:00",
                    "end": "2026-07-24T13:00:00+02:00",
                    "control_action": "BUY",
                    "soc_start": 50,
                    "soc_end": 65,
                }
            ],
        },
    )
    monkeypatch.setattr(
        advisor,
        "_live_excerpt",
        lambda: {"connected": True, "soc_pct": 50, "grid_w": 8000, "batt_w": 7000},
    )
    monkeypatch.setattr(
        advisor,
        "_history_manifest",
        lambda: {
            "available_days": ["2026-07-22", "2026-07-23", "2026-07-24"],
            "earliest": "2026-07-22",
            "latest": "2026-07-24",
            "count": 3,
        },
    )

    rows = [
        {
            "ts": f"{hour:02d}:00",
            "control_action": "BUY",
            "reason_code": "PRICE_LOW",
            "soc": 20 + hour,
            "price_buy": 0.14,
            "grid_w": 12000,
        }
        for hour in range(detail_rows)
    ]
    monkeypatch.setattr(
        advisor,
        "_gather",
        lambda days, detail_days=2: {
            "daily_summaries": {
                "2026-07-24": {
                    "in_progress": True,
                    "as_of": "12:00",
                    "realized_net_eur": -1.25,
                },
                "2026-07-23": {
                    "realized_net_eur": 2.4,
                    "settlement_mean_abs_net_err_eur": 0.03,
                },
            },
            "recent_detail": (
                {"2026-07-24": {"cycles": rows, "settlements": []}}
                if detail_days
                else {}
            ),
        },
    )


def test_prompt_is_valid_json_and_preserves_operational_core_under_schema_growth(monkeypatch):
    _stub_operational_inputs(monkeypatch, tunable_count=180, detail_rows=24)
    cap = 12000

    _, user = advisor._build_messages(
        None,
        {"ADVISOR_MAX_INPUT_CHARS": str(cap), "ADVISOR_HISTORY_DAYS": "3"},
        conversation_context="old conversation " * 10000,
    )

    payload = advisor._prompt_data_payload(user)
    assert len(user) <= cap
    assert set(("now", "live_now", "current_plan", "performance", "tunables")) <= set(payload)
    assert payload["live_now"]["connected"] is True
    assert payload["current_plan"]["current"]["control_action"] == "BUY"
    assert payload["performance"]["daily_summaries"]["2026-07-23"]["realized_net_eur"] == 2.4
    assert len(payload["tunables"]) == 180
    assert payload["tunables"]["SETTING_179"] == "179"
    assert "Long documentation" not in user
    assert payload["payload_meta"]["schema"] == "advisor_payload_v2"


def test_prompt_never_slices_json_when_optional_detail_does_not_fit(monkeypatch):
    _stub_operational_inputs(monkeypatch, tunable_count=8, detail_rows=500)

    _, user = advisor._build_messages(
        "Why did the plan buy now?",
        {"ADVISOR_MAX_INPUT_CHARS": "9000", "ADVISOR_HISTORY_DAYS": "3"},
        conversation_context="context " * 3000,
    )

    payload = advisor._prompt_data_payload(user)
    assert payload
    assert len(user) <= 9000
    assert "data truncated" not in user
    assert payload["performance"]["daily_summaries"]
    assert payload["payload_meta"]["json_validated"] is True
    assert payload["payload_meta"]["conversation_chars"] < len("context " * 3000)


def test_impossibly_small_budget_fails_before_a_model_call(monkeypatch):
    _stub_operational_inputs(monkeypatch)

    with pytest.raises(advisor.AdvisorPayloadError, match="required operational data"):
        advisor._build_messages(
            None,
            {"ADVISOR_MAX_INPUT_CHARS": "1000", "ADVISOR_HISTORY_DAYS": "3"},
        )


def test_plan_excerpt_collapses_consecutive_slots_into_action_blocks(monkeypatch):
    start = datetime.fromisoformat("2026-07-24T12:00:00+02:00")
    schedule = []
    for index, action in enumerate(("BUY", "BUY", "SELL", "SELL")):
        schedule.append(
            {
                "time": (start + timedelta(minutes=15 * index)).isoformat(),
                "control_action": action,
                "reason_code": "PRICE_LOW" if action == "BUY" else "PRICE_HIGH",
                "soc_start": 20 + index * 5,
                "soc_end": 25 + index * 5,
                "price": 0.10 + index * 0.01,
                "sell": 0.09 + index * 0.01,
                "grid_energy": 1.0 if action == "BUY" else -1.0,
                "pv": 0.2,
                "load": 0.3,
                "planned_ev_kwh": 0.1,
                "ev_target_kw": 2.0,
                "ev_supply": "grid",
                "ev_tentative": False,
                "reason": "Verbose repeated prose which should not be copied.",
            }
        )
    monkeypatch.setattr(
        advisor._data,
        "load_raw_plan",
        lambda: {
            "generated_at": start.isoformat(),
            "battery_soc": 20,
            "current": {"control_action": "BUY"},
            "today": {},
            "schedule": schedule,
        },
    )

    excerpt = advisor._plan_excerpt()

    assert "next_slots" not in excerpt
    assert len(excerpt["next_blocks"]) == 2
    buy, sell = excerpt["next_blocks"]
    assert buy["control_action"] == "BUY"
    assert buy["slots"] == 2
    assert buy["start"] == "2026-07-24T12:00:00+02:00"
    assert buy["end"] == "2026-07-24T12:30:00+02:00"
    assert buy["soc_start"] == 20
    assert buy["soc_end"] == 30
    assert buy["grid_energy_kwh"] == 2.0
    assert buy["price_min"] == 0.1
    assert buy["price_max"] == 0.11
    assert sell["control_action"] == "SELL"


def test_daily_review_prioritizes_completed_day_detail(monkeypatch):
    _stub_operational_inputs(monkeypatch, tunable_count=4, detail_rows=10)
    today_key = datetime.now().date().isoformat()
    yesterday_key = (datetime.now().date() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(
        advisor,
        "_compact_recent_detail",
        lambda detail: {
            today_key: {"evidence": "today", "padding": "x" * 3000},
            yesterday_key: {"evidence": "completed", "padding": "x" * 3000},
        },
    )

    _, user = advisor._build_messages(
        None,
        {"ADVISOR_MAX_INPUT_CHARS": "7500", "ADVISOR_HISTORY_DAYS": "3"},
    )

    detail = advisor._prompt_data_payload(user)["performance"]["recent_detail"]
    assert list(detail) == [yesterday_key]


def test_need_config_only_returns_allow_listed_metadata():
    conf = {"KNOWN_SETTING": "7", "SECRET_TOKEN": "never expose"}
    tunables = [{
        "key": "KNOWN_SETTING",
        "value": "7",
        "type": "int",
        "group": "Test",
        "desc": "A safe setting.",
    }]

    requested = advisor._parse_need_config(
        "NEED_CONFIG: KNOWN_SETTING, SECRET_TOKEN",
        tunables,
    )
    metadata = advisor._tunable_metadata(tunables, requested)

    assert requested == ["KNOWN_SETTING"]
    assert metadata == {
        "KNOWN_SETTING": {
            "value": "7",
            "type": "int",
            "group": "Test",
            "description": "A safe setting.",
        }
    }
    assert "SECRET_TOKEN" not in str(metadata)
