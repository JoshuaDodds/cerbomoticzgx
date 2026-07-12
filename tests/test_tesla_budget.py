"""Safety tests for the Tesla Fleet API spend guard.

The headline guarantee: the worst-case monthly spend from the enforced caps can never
exceed Tesla's $10 credit. These tests fail loudly if anyone raises a cap past the
ceiling — the whole point is that a config or code change can't produce a surprise bill.
"""
from datetime import datetime, timezone

from lib import tesla_budget as tb


def _budget(tmp_path, caps=None, clock=None):
    return tb.TeslaBudget(caps=caps, state_path=str(tmp_path / "budget.json"), clock=clock)


# --- the money-safety guarantee -------------------------------------------

def test_default_caps_stay_under_the_monthly_credit():
    cost = tb.projected_monthly_cost_usd(tb.DEFAULT_DAILY_CAPS)
    assert cost <= tb.MONTHLY_SAFETY_CEILING_USD, (
        f"Default Tesla caps project ${cost:.2f}/mo — over the ${tb.MONTHLY_SAFETY_CEILING_USD} "
        f"safety ceiling. Lower the caps in tesla_budget.DEFAULT_DAILY_CAPS.")
    assert cost < tb.MONTHLY_CREDIT_USD


def test_ceiling_is_below_the_credit():
    # There must be real margin between what we allow and what Tesla starts billing.
    assert tb.MONTHLY_SAFETY_CEILING_USD < tb.MONTHLY_CREDIT_USD


def test_guard_clamps_unsafe_caps_below_ceiling(tmp_path):
    # Even a wildly unsafe config cannot make the guard allow overspend: caps are clamped.
    insane = {"command": 1_000_000, "data": 1_000_000, "wake": 1_000_000}
    b = _budget(tmp_path, caps=insane)
    assert tb.projected_monthly_cost_usd(b.caps) <= tb.MONTHLY_SAFETY_CEILING_USD
    assert b.snapshot()["projected_month_usd"] <= tb.MONTHLY_SAFETY_CEILING_USD


def test_projected_cost_math():
    caps = {"command": 40, "data": 40, "wake": 4}
    # 31 * (40*0.001 + 40*0.002 + 4*0.02) = 31 * (0.04 + 0.08 + 0.08) = 31 * 0.20 = 6.20
    assert abs(tb.projected_monthly_cost_usd(caps) - 6.20) < 1e-9


# --- enforcement ----------------------------------------------------------

def test_spend_blocks_once_cap_reached(tmp_path):
    b = _budget(tmp_path, caps={"command": 0, "data": 3, "wake": 0})
    assert b.spend("data") is True
    assert b.spend("data") is True
    assert b.spend("data") is True
    assert b.spend("data") is False        # 4th exceeds the cap of 3
    assert b.allow("data") is False
    # A category with a zero cap is always blocked.
    assert b.spend("wake") is False


def test_blocked_call_records_nothing(tmp_path):
    b = _budget(tmp_path, caps={"command": 0, "data": 1, "wake": 0})
    assert b.spend("data") is True
    before = b.spent_today_usd()
    assert b.spend("data") is False
    assert b.spent_today_usd() == before   # a blocked call must not add to spend


def test_counters_roll_over_at_utc_day(tmp_path):
    day = {"t": datetime(2026, 7, 10, 23, 59, tzinfo=timezone.utc)}
    b = _budget(tmp_path, caps={"command": 0, "data": 1, "wake": 0}, clock=lambda: day["t"])
    assert b.spend("data") is True
    assert b.spend("data") is False        # cap hit for 2026-07-10
    day["t"] = datetime(2026, 7, 11, 0, 1, tzinfo=timezone.utc)   # new UTC day
    assert b.spend("data") is True         # fresh budget
    assert b.spent_today_usd() == round(tb.UNIT_COST_USD["data"], 4)


def test_counters_persist_across_instances(tmp_path):
    # A restart (new instance, same durable path) must not reset the day's budget.
    path = str(tmp_path / "budget.json")
    b1 = tb.TeslaBudget(caps={"command": 0, "data": 1, "wake": 0}, state_path=path)
    assert b1.spend("data") is True
    b2 = tb.TeslaBudget(caps={"command": 0, "data": 1, "wake": 0}, state_path=path)
    assert b2.spend("data") is False       # counter survived the "restart"


def test_usage_snapshot_reports_counts_and_costs(tmp_path):
    path = str(tmp_path / "budget.json")
    b = tb.TeslaBudget(caps={"command": 10, "data": 10, "wake": 10}, state_path=path)
    b.spend("command"); b.spend("data"); b.spend("data"); b.spend("wake")
    snap = tb.usage_snapshot(path)
    assert snap["categories"]["command"] == {"count": 1, "cost": 0.001}
    assert snap["categories"]["data"] == {"count": 2, "cost": 0.004}
    assert snap["categories"]["wake"] == {"count": 1, "cost": 0.02}
    assert snap["total"] == 0.025
    assert snap["currency"] == "EUR"


def test_usage_snapshot_missing_file_is_zero(tmp_path):
    snap = tb.usage_snapshot(str(tmp_path / "nope.json"))
    assert snap["total"] == 0.0
    assert snap["categories"]["data"]["count"] == 0


def test_seed_month_usage_reconciles_and_guard_accumulates(tmp_path):
    path = str(tmp_path / "budget.json")
    tb.seed_month_usage({"command": 13, "data": 30, "wake": 2}, path)
    snap = tb.usage_snapshot(path)
    assert snap["categories"]["command"]["count"] == 13
    assert snap["categories"]["data"]["count"] == 30
    assert snap["categories"]["wake"]["count"] == 2
    assert snap["total"] == round(13 * 0.001 + 30 * 0.002 + 2 * 0.02, 4)   # 0.113
    # The guard keeps counting from the seeded baseline.
    b = tb.TeslaBudget(caps={"command": 100, "data": 100, "wake": 100}, state_path=path)
    assert b.spend("data") is True
    assert tb.usage_snapshot(path)["categories"]["data"]["count"] == 31


def test_month_total_accumulates_across_days_and_resets_monthly(tmp_path):
    import json as _json
    path = str(tmp_path / "budget.json")
    day = {"t": datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)}
    b = tb.TeslaBudget(caps={"command": 0, "data": 100, "wake": 0}, state_path=path, clock=lambda: day["t"])
    b.spend("data"); b.spend("data")                      # day 1
    day["t"] = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    b.spend("data")                                        # day 2, same month
    d = _json.loads(open(path).read())
    assert d["month"] == "2026-07"
    assert d["month_counts"]["data"] == 3                  # accumulates across days
    assert d["counts"]["data"] == 1                        # daily counter reset on the new day
    day["t"] = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)   # new month
    b.spend("data")
    d = _json.loads(open(path).read())
    assert d["month"] == "2026-08"
    assert d["month_counts"]["data"] == 1                  # monthly total reset at cycle boundary


def test_caps_from_settings_defaults_and_overrides():
    assert tb.caps_from_settings(lambda name: None) == tb.DEFAULT_DAILY_CAPS
    got = tb.caps_from_settings({"TESLA_BUDGET_MAX_DATA_PER_DAY": "25"}.get)
    assert got["data"] == 25
    assert got["wake"] == tb.DEFAULT_DAILY_CAPS["wake"]     # unset falls back
