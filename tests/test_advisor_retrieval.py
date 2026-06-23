"""Unit tests for the advisor's on-demand history retrieval directive parsing.

These cover _parse_need_history (pure logic, no I/O): only a reply that *is* a
NEED_HISTORY directive triggers retrieval, only days that actually exist are honored,
commas and A..B ranges work, and the day count is capped.
"""
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
