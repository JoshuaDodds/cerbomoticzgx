"""Hard spend guard for the Tesla Fleet API.

Tesla bills the Fleet API per call and gives each account a $10/month credit. This
module makes it *structurally impossible* to exceed that credit: every billable call
must first pass ``spend(category)``, which atomically checks a per-category daily cap
and refuses (returns False, records nothing) once the cap is hit.

Caps are sized so the worst case — every cap maxed every day of the longest month —
stays under a safety ceiling below the $10 credit. If a misconfiguration ever raises
the caps past that ceiling, they are clamped down automatically, so no config mistake
(or runaway retry loop) can produce a surprise bill.

Usage counters roll per UTC day and are persisted to a durable path (the data volume
on k8s), so a pod restart can't reset the day's budget and let a loop overspend.
"""
import os
import json
import threading
from datetime import datetime, timezone

from lib.constants import logging

# Tesla Fleet API unit prices, USD (2026): commands 1000/$1, data 500/$1, wakes 50/$1.
UNIT_COST_USD = {"command": 0.001, "data": 0.002, "wake": 0.02}

MONTHLY_CREDIT_USD = 10.0          # Tesla's monthly discount
MONTHLY_SAFETY_CEILING_USD = 9.0   # keep worst-case spend below this (margin under the credit)
DAYS_PER_MONTH = 31                # bill against the longest possible month

# Conservative default daily caps → worst case ≈ $8.06/month
# (31 × (40·0.001 commands + 50·0.002 data + 6·0.02 wakes) = 31 × $0.26). Extra data + wake
# headroom lets the controller spend a few purposeful surplus-discovery wakes; still < $10
# and under the $9 safety ceiling (caps above it are auto-clamped).
DEFAULT_DAILY_CAPS = {"command": 40, "data": 50, "wake": 6}

DEFAULT_STATE_PATH = "data/tesla_budget.json"


def projected_monthly_cost_usd(caps: dict) -> float:
    """Worst-case monthly $ if every daily cap is hit every day of the longest month."""
    return DAYS_PER_MONTH * sum(UNIT_COST_USD[c] * float(caps.get(c, 0)) for c in UNIT_COST_USD)


def clamp_caps_to_ceiling(caps: dict, ceiling: float = MONTHLY_SAFETY_CEILING_USD) -> dict:
    """Return caps whose worst-case monthly cost is <= ceiling, scaling down if needed."""
    out = {c: max(0, int(caps.get(c, 0))) for c in UNIT_COST_USD}
    cost = projected_monthly_cost_usd(out)
    if cost <= ceiling or cost <= 0:
        return out
    factor = ceiling / cost
    clamped = {c: int(out[c] * factor) for c in out}
    logging.warning("tesla_budget: caps %s project $%.2f/mo (> $%.2f ceiling); clamped to %s ($%.2f/mo).",
                    out, cost, ceiling, clamped, projected_monthly_cost_usd(clamped))
    return clamped


class TeslaBudget:
    """Per-category daily spend limiter with durable, UTC-day-rolling counters."""

    def __init__(self, caps: dict = None, state_path: str = None, clock=None):
        self._caps = clamp_caps_to_ceiling(caps or DEFAULT_DAILY_CAPS)
        self._path = state_path or DEFAULT_STATE_PATH
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    @property
    def caps(self) -> dict:
        return dict(self._caps)

    def _today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                d = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        today = self._today()
        month = today[:7]                      # YYYY-MM billing cycle (UTC)
        if d.get("date") != today:             # daily counters (for cap enforcement) roll daily
            d["date"] = today
            d["counts"] = {}
        if d.get("month") != month:            # monthly counters (for display) roll monthly
            d["month"] = month
            d["month_counts"] = {}
        d.setdefault("counts", {})
        d.setdefault("month_counts", {})
        return d

    def _save(self, d: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, self._path)
        except OSError as e:                       # pragma: no cover - disk failure
            logging.warning("tesla_budget: could not persist counters to %s: %s", self._path, e)

    def allow(self, category: str, n: int = 1) -> bool:
        """True if n more calls of this category fit under today's cap (no state change)."""
        with self._lock:
            d = self._load()
            return (d["counts"].get(category, 0) + n) <= self._caps.get(category, 0)

    def spend(self, category: str, n: int = 1) -> bool:
        """Atomic check-and-record. Returns False and records nothing if it would exceed the cap.

        Every billable Fleet API call MUST gate on this: ``if not budget.spend('data'): return``.
        """
        with self._lock:
            d = self._load()
            used = d["counts"].get(category, 0)
            if (used + n) > self._caps.get(category, 0):
                logging.info("tesla_budget: BLOCKED %s call — daily cap %d reached (spent $%.3f today).",
                             category, self._caps.get(category, 0), self._spent_usd(d))
                return False
            d["counts"][category] = used + n
            d["month_counts"][category] = d["month_counts"].get(category, 0) + n
            self._save(d)
            return True

    def _spent_usd(self, d: dict) -> float:
        return sum(UNIT_COST_USD[c] * d["counts"].get(c, 0) for c in UNIT_COST_USD)

    def spent_today_usd(self) -> float:
        with self._lock:
            return round(self._spent_usd(self._load()), 4)

    def snapshot(self) -> dict:
        with self._lock:
            d = self._load()
            return {
                "date": d["date"],
                "counts": dict(d["counts"]),
                "caps": dict(self._caps),
                "spent_usd": round(self._spent_usd(d), 4),
                "projected_month_usd": round(projected_monthly_cost_usd(self._caps), 2),
            }


def usage_snapshot(state_path=None) -> dict:
    """Read the CURRENT billing cycle's spend counters for DISPLAY (no live guard needed).
    Returns per-category counts + $ and the month-to-date total, so the dashboard can show
    Tesla API usage against the $10 monthly credit. Counters roll to zero at the month
    boundary (UTC calendar month)."""
    path = state_path or DEFAULT_STATE_PATH
    try:
        with open(path) as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        d = {}
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    counts = {}
    if isinstance(d, dict) and d.get("month") == month:
        counts = d.get("month_counts", {}) or {}
    cats, total = {}, 0.0
    for c in UNIT_COST_USD:
        n = int(counts.get(c, 0) or 0)
        cost = round(UNIT_COST_USD[c] * n, 4)
        cats[c] = {"count": n, "cost": cost}
        total += cost
    return {
        "month": month,
        "categories": cats,                      # command/data/wake -> {count, cost}
        "total": round(total, 4),
        "unit_cost": dict(UNIT_COST_USD),
        "monthly_credit": MONTHLY_CREDIT_USD,
        "remaining": round(max(0.0, MONTHLY_CREDIT_USD - total), 2),
        # Tesla localises the developer portal's billing display; this account shows EUR, so
        # match it. The per-call rates are numerically the same as Tesla's published values.
        "currency": "EUR",
    }


def seed_month_usage(counts: dict, state_path=None) -> dict:
    """Set the current billing cycle's DISPLAY counters (e.g. to reconcile with the Tesla
    developer portal, which is the only authoritative source). Daily cap counters are left
    untouched; the guard then accumulates from this baseline. Returns the new snapshot."""
    path = state_path or DEFAULT_STATE_PATH
    try:
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        d = {}
    now = datetime.now(timezone.utc)
    d["month"] = now.strftime("%Y-%m")
    d["month_counts"] = {c: max(0, int(counts.get(c, 0) or 0)) for c in UNIT_COST_USD}
    d.setdefault("date", now.strftime("%Y-%m-%d"))
    d.setdefault("counts", {})
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError as e:                        # pragma: no cover
        logging.warning("tesla_budget: could not seed usage to %s: %s", path, e)
    return usage_snapshot(path)


def caps_from_settings(getter=None) -> dict:
    """Build daily caps from settings, falling back to the conservative defaults.

    ``getter(name)`` is a settings resolver (e.g. retrieve_setting); injected for testing.
    """
    if getter is None:
        from lib.config_retrieval import retrieve_setting as getter
    keys = {
        "command": "TESLA_BUDGET_MAX_COMMANDS_PER_DAY",
        "data": "TESLA_BUDGET_MAX_DATA_PER_DAY",
        "wake": "TESLA_BUDGET_MAX_WAKES_PER_DAY",
    }
    caps = {}
    for cat, key in keys.items():
        raw = getter(key)
        try:
            caps[cat] = int(raw) if raw not in (None, "", "None") else DEFAULT_DAILY_CAPS[cat]
        except (TypeError, ValueError):
            caps[cat] = DEFAULT_DAILY_CAPS[cat]
    return caps


_DEFAULT_BUDGET = None
_DEFAULT_BUDGET_LOCK = threading.Lock()


def default_budget() -> TeslaBudget:
    """Process-wide singleton guard built from settings (used by tesla_api)."""
    global _DEFAULT_BUDGET
    with _DEFAULT_BUDGET_LOCK:
        if _DEFAULT_BUDGET is None:
            from lib.config_retrieval import retrieve_setting
            path = retrieve_setting("TESLA_BUDGET_STATE_PATH") or DEFAULT_STATE_PATH
            _DEFAULT_BUDGET = TeslaBudget(caps=caps_from_settings(), state_path=path)
            logging.info("tesla_budget: guard active — caps %s (~$%.2f/mo worst case).",
                         _DEFAULT_BUDGET.caps, projected_monthly_cost_usd(_DEFAULT_BUDGET.caps))
    return _DEFAULT_BUDGET
