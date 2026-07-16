"""Safety tests for the Tesla Fleet API spend guard.

The headline guarantee: the enforced MONTHLY ceiling means the billing cycle's spend can
never exceed the safety ceiling (below Tesla's $10 credit), while safety-critical calls (a
charge_stop and the wake to deliver it) are never blocked. A per-day cap is only a runaway
circuit-breaker. Charging is bursty (a couple of days a week), so a monthly guard fits it —
a daily "worst case every day" model would wrongly block a legitimate charge day.
"""
from datetime import datetime, timezone

from lib import tesla_budget as tb


def _budget(tmp_path, caps=None, clock=None):
    return tb.TeslaBudget(caps=caps, state_path=str(tmp_path / "budget.json"), clock=clock)


# --- the money-safety guarantee -------------------------------------------

def test_ceiling_is_below_the_credit():
    # There must be real margin between what we allow and what Tesla starts billing.
    assert tb.MONTHLY_SAFETY_CEILING_USD < tb.MONTHLY_CREDIT_USD


def test_default_caps_under_daily_runaway_ceiling():
    # The daily caps are a per-day runaway breaker; their worst-case DAILY cost stays under the
    # daily ceiling. (The monthly bill is guarded separately, per-spend.)
    assert tb.projected_daily_cost_usd(tb.DEFAULT_DAILY_CAPS) <= tb.DAILY_SAFETY_CEILING_USD


def test_guard_clamps_caps_below_daily_ceiling(tmp_path):
    # A wildly unsafe config is clamped so no single day can burn more than the daily ceiling.
    insane = {"command": 1_000_000, "data": 1_000_000, "wake": 1_000_000}
    b = _budget(tmp_path, caps=insane)
    assert tb.projected_daily_cost_usd(b.caps) <= tb.DAILY_SAFETY_CEILING_USD


def test_monthly_ceiling_is_the_hard_guard_and_stops_bypass_it(tmp_path):
    # Seed the billing cycle right at the ceiling. A normal call is then blocked, but a
    # safety-critical call (a charge_stop / its wake) is NEVER blocked.
    path = str(tmp_path / "budget.json")
    wakes_to_ceiling = int(tb.MONTHLY_SAFETY_CEILING_USD / tb.UNIT_COST_USD["wake"])   # 450
    tb.seed_month_usage({"command": 0, "data": 0, "wake": wakes_to_ceiling}, path)
    b = tb.TeslaBudget(caps=tb.DEFAULT_DAILY_CAPS, state_path=path)
    assert b.spend("command") is False                    # normal call blocked at the ceiling
    assert b.spend("command", critical=True) is True      # safety-critical stop always goes through
    assert b.spend("wake", critical=True) is True         # ...and the wake to deliver it


def test_daily_runaway_breaker_caps_a_stuck_loop(tmp_path):
    # Even well under the monthly ceiling, a stuck loop can't exceed the per-day cap.
    b = _budget(tmp_path, caps=tb.DEFAULT_DAILY_CAPS)
    n = 0
    while b.spend("command") and n <= 100_000:
        n += 1
    assert n == tb.DEFAULT_DAILY_CAPS["command"]           # blocked at the daily cap (monthly still tiny)


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


def test_refund_reverses_a_non_billable_spend_and_floors_at_zero(tmp_path):
    path = str(tmp_path / "budget.json")
    b = tb.TeslaBudget(caps={"command": 10, "data": 10, "wake": 10}, state_path=path)
    b.spend("command"); b.spend("command")
    assert tb.usage_snapshot(path)["categories"]["command"]["count"] == 2
    b.refund("command")                                    # a 5xx/network call reversed
    assert tb.usage_snapshot(path)["categories"]["command"]["count"] == 1
    b.refund("command"); b.refund("command")               # never goes negative
    assert tb.usage_snapshot(path)["categories"]["command"]["count"] == 0


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


# --- Streaming Signals: durable (survives restarts), never gated ----------

def test_bump_signal_count_accumulates_and_persists(tmp_path):
    path = str(tmp_path / "budget.json")
    assert tb.bump_signal_count(20, path) == 20
    assert tb.bump_signal_count(5, path) == 25
    # "Restart" = a fresh read of the same file -- nothing kept in memory to lose.
    assert tb.usage_snapshot(path)["streaming"]["count"] == 25


def test_seed_signal_count_sets_absolute_value(tmp_path):
    path = str(tmp_path / "budget.json")
    tb.bump_signal_count(20, path)
    tb.seed_signal_count(1334, path)                       # reconcile against the Tesla portal
    assert tb.usage_snapshot(path)["streaming"]["count"] == 1334
    tb.bump_signal_count(6, path)                           # guard keeps counting from baseline
    assert tb.usage_snapshot(path)["streaming"]["count"] == 1340


def test_signal_count_never_affects_gated_total_or_remaining(tmp_path):
    path = str(tmp_path / "budget.json")
    tb.seed_month_usage({"command": 10, "data": 10, "wake": 1}, path)
    before = tb.usage_snapshot(path)
    tb.seed_signal_count(50000, path)                       # a lot of signals, still ~free
    after = tb.usage_snapshot(path)
    assert after["total"] == before["total"]
    assert after["remaining"] == before["remaining"]
    assert after["streaming"]["count"] == 50000
    assert after["streaming"]["cost"] == round(50000 * tb.STREAMING_SIGNAL_COST_USD, 4)


def test_seeding_one_category_does_not_wipe_the_others(tmp_path):
    # Regression: seed_month_usage used to REPLACE the whole month_counts dict, so seeding just
    # one category -- or seeding signals via the separate seed_signal_count call -- would
    # silently wipe out whatever else was already recorded there.
    path = str(tmp_path / "budget.json")
    tb.seed_month_usage({"command": 13, "data": 30, "wake": 2}, path)
    tb.seed_signal_count(1334, path)

    tb.seed_month_usage({"wake": 5}, path)                  # only reconcile wake this time

    snap = tb.usage_snapshot(path)
    assert snap["categories"]["wake"]["count"] == 5         # updated
    assert snap["categories"]["command"]["count"] == 13     # untouched
    assert snap["categories"]["data"]["count"] == 30        # untouched
    assert snap["streaming"]["count"] == 1334               # untouched by either seed call


def test_signal_count_rolls_over_at_month_boundary(tmp_path):
    import json as _json
    path = str(tmp_path / "budget.json")
    d = {"month": "2026-06", "date": "2026-06-30", "counts": {}, "month_counts": {"signals": 999}}
    with open(path, "w") as f:
        _json.dump(d, f)
    # bump_signal_count/seed_signal_count use the real UTC clock (no injectable clock, unlike
    # TeslaBudget) -- so this only proves the roll logic fires when the stored month differs
    # from "now"; it can't pin an exact expected month without being a tautology of today's date.
    new_total = tb.bump_signal_count(10, path)
    assert new_total == 10                                  # rolled, not 1009


def test_concurrent_spend_and_signal_bump_do_not_lose_updates(tmp_path):
    # Regression: TeslaBudget.spend()/refund() and the module-level seed_month_usage()/
    # bump_signal_count()/seed_signal_count() read-modify-write the SAME file. In production,
    # the telemetry bridge's own MQTT thread calls bump_signal_count() every ~20 messages while
    # the EV controller's timer thread concurrently calls spend() through the shared budget
    # singleton -- without a lock shared between both call paths, one thread's read-modify-write
    # can silently revert the other's update (the write itself is atomic, so this is a lost
    # update, not file corruption).
    import threading

    path = str(tmp_path / "budget.json")
    budget = tb.TeslaBudget(caps={"command": 0, "data": 10000, "wake": 0}, state_path=path)
    n_spends, n_bumps = 200, 200

    def spend_worker():
        for _ in range(n_spends):
            assert budget.spend("data") is True

    def bump_worker():
        for _ in range(n_bumps):
            tb.bump_signal_count(1, path)

    t1 = threading.Thread(target=spend_worker)
    t2 = threading.Thread(target=bump_worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    snap = tb.usage_snapshot(path)
    assert snap["categories"]["data"]["count"] == n_spends
    assert snap["streaming"]["count"] == n_bumps
