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

# Fleet Telemetry "Streaming Signals" - pushed by the car, not requests we make, so this is
# display-only and never gated by the spend guard below. Tesla bills ~$1 per 150,000 signals.
STREAMING_SIGNAL_COST_USD = 1.0 / 150000

MONTHLY_CREDIT_USD = 10.0          # Tesla's monthly discount
MONTHLY_SAFETY_CEILING_USD = 9.0   # HARD guard: the billing cycle's spend never exceeds this
DAILY_SAFETY_CEILING_USD = 2.0     # per-day runaway breaker (a loop can't burn more than this/day)
DAYS_PER_MONTH = 31                # for the informational worst-case projection only

# The REAL guard is the monthly ceiling (enforced per-spend). Charging is bursty and infrequent
# (often only a couple of days a WEEK), so sizing a daily cap as "worst case every single day"
# was far too tight — it clamped the caps down and then blocked a legitimate charge day (even a
# safety-critical charge_stop) while the month was nowhere near the $10 credit. These daily caps
# are now only a runaway circuit-breaker (~$1/day worst case); real charge days spend ~$0.30.
DEFAULT_DAILY_CAPS = {"command": 300, "data": 150, "wake": 20}

DEFAULT_STATE_PATH = "data/tesla_budget.json"

_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    """One process-wide RLock per resolved state-file path.

    Multiple writers share this file: TeslaBudget.spend()/refund() (the EV controller's
    thread) and the module-level seed_month_usage()/bump_signal_count()/seed_signal_count()
    below (the telemetry bridge's own MQTT thread calls bump_signal_count() every ~20
    messages when TESLA_TELEMETRY_ENABLED is on, and scripts/tesla_seed_usage.py can run
    while the service is live). Without a SHARED lock, a read-modify-write from one caller
    can interleave with another's and silently revert it -- the write itself is atomic
    (write-to-.tmp then os.replace), so this isn't file corruption, just a lost update. Keyed
    by path (not a single global lock) so independent TeslaBudget instances in tests
    (different tmp_path files) never contend with each other or with the real
    DEFAULT_STATE_PATH.
    """
    key = os.path.abspath(path)
    with _FILE_LOCKS_GUARD:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = threading.RLock()
        return _FILE_LOCKS[key]


def projected_daily_cost_usd(caps: dict) -> float:
    return sum(UNIT_COST_USD[c] * float(caps.get(c, 0)) for c in UNIT_COST_USD)


def projected_monthly_cost_usd(caps: dict) -> float:
    """Informational worst-case month $ (every daily cap hit every day). NOT the guard — the
    enforced MONTHLY_SAFETY_CEILING is the real bound, so this can exceed the credit for caps
    that are only a per-day runaway breaker."""
    return DAYS_PER_MONTH * projected_daily_cost_usd(caps)


def clamp_caps_to_ceiling(caps: dict, ceiling: float = DAILY_SAFETY_CEILING_USD) -> dict:
    """Clamp caps so the worst-case DAILY cost stays under the per-day runaway ceiling. (The
    monthly bill is guarded separately, per-spend, against MONTHLY_SAFETY_CEILING_USD.)"""
    out = {c: max(0, int(caps.get(c, 0))) for c in UNIT_COST_USD}
    cost = projected_daily_cost_usd(out)
    if cost <= ceiling or cost <= 0:
        return out
    factor = ceiling / cost
    clamped = {c: int(out[c] * factor) for c in out}
    logging.warning("tesla_budget: caps %s project $%.2f/day (> $%.2f daily ceiling); clamped to %s.",
                    out, cost, ceiling, clamped)
    return clamped


class TeslaBudget:
    """Per-category daily spend limiter with durable, UTC-day-rolling counters."""

    def __init__(self, caps: dict = None, state_path: str = None, clock=None):
        self._caps = clamp_caps_to_ceiling(caps or DEFAULT_DAILY_CAPS)
        self._path = state_path or DEFAULT_STATE_PATH
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Shared per-path lock (see _lock_for) -- not a private RLock -- so this instance's
        # spend()/refund() serialize against the module-level seed_month_usage()/
        # bump_signal_count()/seed_signal_count() below when they target the same file.
        self._lock = _lock_for(self._path)

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

    def spend(self, category: str, n: int = 1, critical: bool = False) -> bool:
        """Atomic check-and-record. Returns False and records nothing if it would breach a guard.

        Every billable Fleet API call MUST gate on this: ``if not budget.spend('data'): return``.

        ``critical=True`` marks a safety-essential call (e.g. a charge_stop and the wake needed
        to deliver it) that must NEVER be blocked — leaving the car charging is worse than a
        fraction of a cent of overage. It is still recorded so the accounting stays honest.

        Two guards for non-critical calls:
          * HARD monthly ceiling — the billing cycle's spend never exceeds MONTHLY_SAFETY_CEILING
            (this is the real bound; charging is bursty so a monthly guard fits it, not a daily one);
          * per-day runaway breaker — a stuck loop can't burn more than ~a dollar in a single day.
        """
        with self._lock:
            d = self._load()
            if not critical:
                if self._spent_usd_month(d) + UNIT_COST_USD[category] * n > MONTHLY_SAFETY_CEILING_USD:
                    logging.warning("tesla_budget: BLOCKED %s — monthly ceiling $%.2f reached "
                                    "(spent $%.2f this cycle).", category, MONTHLY_SAFETY_CEILING_USD,
                                    self._spent_usd_month(d))
                    return False
                if (d["counts"].get(category, 0) + n) > self._caps.get(category, 0):
                    logging.info("tesla_budget: BLOCKED %s — daily runaway cap %d reached (spent $%.3f today).",
                                 category, self._caps.get(category, 0), self._spent_usd(d))
                    return False
            d["counts"][category] = d["counts"].get(category, 0) + n
            d["month_counts"][category] = d["month_counts"].get(category, 0) + n
            self._save(d)
            return True

    def refund(self, category: str, n: int = 1) -> None:
        """Reverse a previously-recorded spend that turned out NON-billable. Tesla only bills
        responses < 500 (a 5xx or a network exception is not billed), but we spend up-front to
        enforce the cap — so when the call comes back non-billable we refund it to keep the
        displayed usage in line with the portal. Floors at 0."""
        with self._lock:
            d = self._load()
            d["counts"][category] = max(0, d["counts"].get(category, 0) - n)
            d["month_counts"][category] = max(0, d["month_counts"].get(category, 0) - n)
            self._save(d)

    def _spent_usd(self, d: dict) -> float:
        return sum(UNIT_COST_USD[c] * d["counts"].get(c, 0) for c in UNIT_COST_USD)

    def _spent_usd_month(self, d: dict) -> float:
        mc = d.get("month_counts", {}) or {}
        return sum(UNIT_COST_USD[c] * mc.get(c, 0) for c in UNIT_COST_USD)

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


def _load_month_state(path: str) -> dict:
    """Load state and roll month_counts at the UTC month boundary. Shared by the
    free-function seed/bump helpers below, which don't need TeslaBudget's daily-cap rolling
    (streaming signals aren't gated/capped, only counted for display)."""
    try:
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        d = {}
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    if d.get("month") != month:
        d["month"] = month
        d["month_counts"] = {}
    d.setdefault("month_counts", {})
    d.setdefault("date", now.strftime("%Y-%m-%d"))
    d.setdefault("counts", {})
    return d


def _save_state(path: str, d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError as e:                        # pragma: no cover - disk failure
        logging.warning("tesla_budget: could not persist state to %s: %s", path, e)


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
    sig_count = int(counts.get("signals", 0) or 0)
    return {
        "month": month,
        "categories": cats,                      # command/data/wake -> {count, cost}
        # Streaming Signals: same durable file/month-roll as the categories above, but priced
        # and totalled separately (kept OUT of total/remaining) since it's never gated by the
        # spend guard -- see STREAMING_SIGNAL_COST_USD.
        "streaming": {"count": sig_count, "cost": round(sig_count * STREAMING_SIGNAL_COST_USD, 4),
                     "approx": True},
        "total": round(total, 4),
        "unit_cost": dict(UNIT_COST_USD),
        "monthly_credit": MONTHLY_CREDIT_USD,
        "remaining": round(max(0.0, MONTHLY_CREDIT_USD - total), 2),
        # Tesla localises the developer portal's billing display; this account shows EUR, so
        # match it. The per-call rates are numerically the same as Tesla's published values.
        "currency": "EUR",
    }


def seed_month_usage(counts: dict, state_path=None) -> dict:
    """Set the current billing cycle's DISPLAY counters for the given categories (e.g. to
    reconcile with the Tesla developer portal, which is the only authoritative source).
    Daily cap counters are left untouched; the guard then accumulates from this baseline.
    Only touches keys present in ``counts`` -- e.g. seeding just "wake" leaves "command",
    "data", and "signals" (see seed_signal_count) exactly as they were. Returns the new
    snapshot."""
    path = state_path or DEFAULT_STATE_PATH
    with _lock_for(path):     # serialize against TeslaBudget.spend()/refund() on the same file
        d = _load_month_state(path)
        for c, v in counts.items():
            if c in UNIT_COST_USD:
                d["month_counts"][c] = max(0, int(v or 0))
        _save_state(path, d)
        return usage_snapshot(path)   # read-back inside the same (reentrant) lock -- no
                                       # window for another writer to sneak in before we return


def bump_signal_count(n: int, state_path=None) -> int:
    """Durably ADD n to this billing cycle's streaming-signal count. Called by the telemetry
    bridge as signals arrive (from its own MQTT thread); rolls at the UTC month boundary like
    the other counters. Kept in the SAME file as command/data/wake so a pod restart can't lose
    the running total the way the old GlobalState-backed tracking did (GlobalState lives in a
    SQLite file on tmpfs that main.py explicitly recreates -- DROP TABLE IF EXISTS -- on every
    process start). Returns the new month-to-date signal count."""
    path = state_path or DEFAULT_STATE_PATH
    with _lock_for(path):     # serialize against TeslaBudget.spend()/refund() on the same file
        d = _load_month_state(path)
        new_total = max(0, int(d["month_counts"].get("signals", 0) or 0) + int(n))
        d["month_counts"]["signals"] = new_total
        _save_state(path, d)
    return new_total


def seed_signal_count(count: int, state_path=None) -> int:
    """SET (not add to) this billing cycle's streaming-signal count -- e.g. to reconcile with
    the number shown on the Tesla developer portal's Billing & Usage page, the only
    authoritative source (mirrors seed_month_usage's role for command/data/wake). Returns the
    new month-to-date signal count."""
    path = state_path or DEFAULT_STATE_PATH
    with _lock_for(path):     # serialize against TeslaBudget.spend()/refund() on the same file
        d = _load_month_state(path)
        d["month_counts"]["signals"] = max(0, int(count))
        _save_state(path, d)
        return d["month_counts"]["signals"]


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
            logging.info("tesla_budget: guard active — hard monthly ceiling $%.2f (of $%.0f credit); "
                         "per-day runaway caps %s. Charge-stops bypass the guard.",
                         MONTHLY_SAFETY_CEILING_USD, MONTHLY_CREDIT_USD, _DEFAULT_BUDGET.caps)
    return _DEFAULT_BUDGET
