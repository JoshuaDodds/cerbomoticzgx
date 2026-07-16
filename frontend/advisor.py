"""Read-only AI advisor for the ESS dashboard (Phase 1).

A manually-triggered analyst: it gathers recent performance history, the current
plan, and the **tunable** configuration (never secrets), sends them to Claude, and
returns a markdown report. Two modes:

  * default daily review — "how is the optimizer doing, anything to improve?"
  * an open question — e.g. "Why did we sell at 15:00 yesterday?"

SAFETY: this module is strictly read-only. It never writes config, never touches
control, and never sends secrets to the API — only the allow-listed tunables from
``CONFIG_SCHEMA`` (plus performance data) are shared. It lives in the frontend
package so it is fully isolated from the control runtime.
"""
import os
import re
import glob
import json
import time
import logging
import threading
from datetime import datetime, timedelta

from dotenv import dotenv_values

from frontend.config_schema import CONFIG_SCHEMA
from lib.config_paths import env_path, secrets_path
from frontend import data as _data
from lib import history_store as _hist

# Current Claude models (override via ADVISOR_MODEL). Sonnet is the sensible
# default for this analysis; Haiku is cheaper/faster for lighter use.
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_HISTORY_DAYS = 4
MAX_OUTPUT_TOKENS = 1800
ADVISOR_TIMEOUT_S = 300
# Token-budget guards so a review costs a few K tokens, not ~100K. Extended thinking
# is the big sink (it ran away on the first call), so it's disabled by default; the
# input prompt is also hard-capped (history detail auto-trims to fit).
DEFAULT_MAX_INPUT_CHARS = 16000          # ~4K tokens of data
DEFAULT_MAX_THINKING_TOKENS = 0          # 0 = no extended thinking on the CLI
# On-demand history retrieval (question path only): when the model decides it needs
# day(s) beyond the inline window it emits a NEED_HISTORY directive and we pull those
# specific day files from data/history/. Bounded so a deep question can't blow up.
DEFAULT_RETRIEVAL_MAX_DAYS = 14
DEFAULT_RETRIEVAL_MAX_CHARS = 120000     # ~30K tokens; one full day of slots is ~45K chars
# Claude Code CLI streaming flags (overridable via ADVISOR_CLI_STREAM_ARGS in case a
# CLI version differs). stream-json + partial messages gives token-by-token output.
DEFAULT_STREAM_ARGS = "--output-format stream-json --verbose --include-partial-messages"
ADVISOR_LATEST_PATH = os.path.join("data", "advisor_latest.json")

_run_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Advisor chat persistence
# --------------------------------------------------------------------------- #
def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _remove_latest_report() -> None:
    try:
        os.remove(ADVISOR_LATEST_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _empty_chat(ok: bool = False) -> dict:
    return {"ok": ok, "schema": "advisor_chat_v1", "messages": []}


def _normalize_chat(record: dict | None) -> dict:
    if not isinstance(record, dict):
        return _empty_chat()
    if isinstance(record.get("messages"), list):
        out = {
            "ok": bool(record.get("ok")),
            "schema": "advisor_chat_v1",
            "messages": [m for m in record.get("messages", []) if isinstance(m, dict)],
        }
        if record.get("updated_at"):
            out["updated_at"] = record.get("updated_at")
        return out
    # Backward-compatible read of the prior single-report shape.
    text = record.get("report") or record.get("error") or ""
    if not text:
        return _empty_chat()
    msg = {
        "role": "assistant",
        "text": text,
        "created_at": record.get("generated_at") or datetime.now().astimezone().isoformat(),
        "ok": bool(record.get("ok")),
    }
    for key in ("mode", "model", "auth", "elapsed_s", "error"):
        if record.get(key) is not None:
            msg[key] = record.get(key)
    return {
        "ok": bool(record.get("ok")),
        "schema": "advisor_chat_v1",
        "updated_at": msg["created_at"],
        "messages": [msg],
    }


def latest_report() -> dict:
    try:
        with open(ADVISOR_LATEST_PATH, encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return _empty_chat()
    except (OSError, json.JSONDecodeError):
        return {**_empty_chat(), "error": "Latest advisor chat is unavailable."}
    return _normalize_chat(record)


def _save_chat(chat: dict) -> None:
    _atomic_write_json(ADVISOR_LATEST_PATH, _normalize_chat(chat))


def clear_chat() -> dict:
    _remove_latest_report()
    return _empty_chat(ok=True)


def delete_exchange(index: int) -> dict:
    chat = latest_report()
    messages = chat.get("messages") or []
    if not isinstance(index, int) or index < 0 or index >= len(messages):
        raise IndexError("message index out of range")
    msg = messages[index]
    start, end = index, index + 1
    if msg.get("role") == "user":
        if index + 1 < len(messages) and messages[index + 1].get("role") == "assistant":
            end = index + 2
    elif msg.get("role") == "assistant" and index > 0 and messages[index - 1].get("role") == "user":
        start = index - 1
    del messages[start:end]
    if not messages:
        _remove_latest_report()
        return _empty_chat(ok=True)
    chat["messages"] = messages
    chat["updated_at"] = datetime.now().astimezone().isoformat()
    _save_chat(chat)
    return latest_report()


def _append_user_message(chat: dict, mode: str, question: str | None, created_at: str) -> dict:
    message = {
        "role": "user",
        "mode": mode,
        "text": question if question else "Run daily review",
        "created_at": created_at,
    }
    chat.setdefault("messages", []).append(message)
    chat["updated_at"] = created_at
    return message


def _append_assistant_message(
    chat: dict,
    *,
    text: str,
    created_at: str,
    model: str | None,
    auth: str | None,
    mode: str,
    elapsed_s: float | None = None,
    ok: bool = True,
    error: str | None = None,
) -> dict:
    message = {
        "role": "assistant",
        "mode": mode,
        "text": text or "",
        "created_at": created_at,
        "ok": ok,
    }
    if model:
        message["model"] = model
    if auth:
        message["auth"] = auth
    if elapsed_s is not None:
        message["elapsed_s"] = elapsed_s
    if error:
        message["error"] = error
    chat.setdefault("messages", []).append(message)
    chat["ok"] = ok
    chat["updated_at"] = created_at
    return message


def _conversation_context(chat: dict, max_chars: int = 6000) -> str | None:
    messages = _normalize_chat(chat).get("messages", [])
    if not messages:
        return None
    lines = []
    for m in messages:
        role = "User" if m.get("role") == "user" else "Advisor"
        stamp = m.get("created_at") or ""
        text = (m.get("text") or m.get("error") or "").strip()
        if text:
            lines.append(f"{role} [{stamp}]: {text}")
    context = "\n\n".join(lines).strip()
    if len(context) > max_chars:
        context = "...(earlier chat omitted)...\n" + context[-max_chars:]
    return context or None


# --------------------------------------------------------------------------- #
# Config / secrets
# --------------------------------------------------------------------------- #
def _conf() -> dict:
    """Merge .secrets + .env (secrets first so .env can't shadow a key name)."""
    cfg = {}
    try:
        cfg.update(dotenv_values(secrets_path()) or {})
    except Exception as exc:
        logging.debug("Advisor config: unable to read secrets file: %s", exc)
    try:
        cfg.update(dotenv_values(env_path()) or {})
    except Exception as exc:
        logging.debug("Advisor config: unable to read env file: %s", exc)
    return cfg


def _api_key(conf) -> str | None:
    return (conf.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def _oauth_token(conf) -> str | None:
    return (conf.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip() or None


def _auth_mode(conf) -> str | None:
    """Pick the backend:
      'custom' — any subscription-login CLI set via ADVISOR_CLI_CMD (e.g. the Gemini
                 or OpenAI Codex CLI). Usage is drawn from that plan; no API key.
      'cli'    — Claude Code, authenticated by a Claude Pro/Max OAuth token (or the
                 host's existing `claude` login). No API key.
      'api'    — Anthropic API key, pay-as-you-go.
    Honors ADVISOR_AUTH=cli|api|auto (default auto). A configured ADVISOR_CLI_CMD
    wins unless ADVISOR_AUTH=api is set explicitly."""
    pref = (conf.get("ADVISOR_AUTH") or "auto").strip().lower()
    if pref == "api":
        return "api" if _api_key(conf) else None
    if (conf.get("ADVISOR_CLI_CMD") or "").strip():
        return "custom"             # gemini / codex / any subscription-login CLI
    if pref == "cli":
        # Use an explicit token if provided, else the host's existing `claude` login.
        return "cli"
    if _oauth_token(conf):          # auto: prefer an explicit subscription token
        return "cli"
    if _api_key(conf):
        return "api"
    return None


def _model(conf, backend: str) -> str:
    """Model id. The CLI accepts short aliases (sonnet/opus/haiku); the API needs a
    full model string."""
    m = (conf.get("ADVISOR_MODEL") or "").strip()
    if m:
        return m
    return "sonnet" if backend == "cli" else DEFAULT_MODEL


def _tunables(conf) -> list[dict]:
    """The allow-listed tunables (key, value, type, description) — SECRET-SAFE.

    Only keys present in CONFIG_SCHEMA are ever included, so secrets (tokens, PATs,
    passwords, broker IPs, portal IDs) in .env are never sent to the API.
    """
    out = []
    for group in CONFIG_SCHEMA:
        for s in group.get("settings", []):
            k = s["key"]
            out.append({
                "key": k,
                "value": conf.get(k),
                "type": s.get("type"),
                "group": group.get("group"),
                "desc": s.get("desc", ""),
            })
    return out


# --------------------------------------------------------------------------- #
# History gathering
# --------------------------------------------------------------------------- #
# Compact field sets so a few days of data fit comfortably in one request.
# pv_w = instantaneous PV power; pv_actual_today_kwh = cumulative realized PV (kWh).
# Cumulative day_* totals and net live in the day summary, not per slot.
_CYCLE_FIELDS = ("ts", "control_action", "realized_action", "reason_code", "soc",
                 "price_buy", "price_sell", "applied_setpoint_w", "grid_w", "pv_w",
                 "batt_w", "load_w", "pv_actual_today_kwh", "load_actual_today_wh")
# actual_pv_kwh = per-slot realized PV; predicted_grid_kwh = predicted import/export.
_SETTLE_FIELDS = ("ts", "predicted_control_action", "predicted_grid_kwh",
                  "predicted_net_eur", "actual_net_eur", "actual_import_kwh",
                  "actual_export_kwh", "actual_pv_kwh", "soc_start", "soc_end",
                  "price_buy", "cost_basis_eur_per_kwh")


def _read_day(day) -> list[dict]:
    # Serves NDJSON hot days and Parquet-compacted cold days transparently.
    return _hist.read_day(day, _data.history_dir())


def _hm(ts):
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except (TypeError, ValueError):
        return ts


def _trim(rec, fields):
    return {k: rec.get(k) for k in fields if rec.get(k) is not None}


def _day_summary(recs, is_today: bool = False) -> dict:
    """Per-day rollup: P&L, action mix, settlement accuracy.

    For an IN-PROGRESS day (`is_today`), the *_actual_kwh and net figures are
    cumulative-so-far, NOT full-day totals — so we expose `pv_expected_so_far_kwh`
    (forecast that should already be realised, from the forecast minus the still-
    remaining forecast) for a like-for-like comparison, and we do NOT emit a full-day
    `pv_forecast_err_kwh` (which would otherwise read as a huge "miss" at dawn)."""
    cycles = [r for r in recs if r.get("kind") == "cycle"]
    settles = [r for r in recs if r.get("kind") == "settlement"]
    actions = {}
    for c in cycles:
        a = c.get("control_action") or "?"
        actions[a] = actions.get(a, 0) + 1
    last = cycles[-1] if cycles else {}
    # Settlement net error (predicted vs actual), mean absolute.
    errs = [abs((s.get("predicted_net_eur") or 0) - (s.get("actual_net_eur") or 0))
            for s in settles if s.get("actual_net_eur") is not None]

    def _kwh(v, div=1.0):
        try:
            return round(float(v) / div, 2)
        except (TypeError, ValueError):
            return None

    # PV / load forecast-vs-actual, normalised to kWh. NB the source field
    # `pv_forecast_today_kwh` is actually stored in Wh (a known mislabel), as are the
    # load_*_wh fields, so they are /1000 here; pv_actual_today_kwh is already kWh.
    pv_fc = _kwh(last.get("pv_forecast_today_kwh"), 1000.0)   # whole-day forecast
    pv_act = _kwh(last.get("pv_actual_today_kwh"))            # cumulative actual so far
    pv_remaining = _kwh(last.get("pv_remaining_wh"), 1000.0)  # forecast still to come
    ld_fc = _kwh(last.get("load_forecast_today_wh"), 1000.0)
    ld_act = _kwh(last.get("load_actual_today_wh"), 1000.0)
    out = {
        "cycles": len(cycles),
        "actions": actions,
        "day_import_kwh": last.get("day_import_kwh"),
        "day_import_cost": last.get("day_import_cost"),
        "day_export_kwh": last.get("day_export_kwh"),
        "day_export_reward": last.get("day_export_reward"),
        "realized_net_eur": last.get("realized_net_eur"),
        "settlement_mean_abs_net_err_eur": round(sum(errs) / len(errs), 4) if errs else None,
        "pv_forecast_kwh": pv_fc,        # whole-day forecast
        "pv_actual_kwh": pv_act,         # realized PV so far
        "load_forecast_kwh": ld_fc,
        "load_actual_kwh": ld_act,
    }
    if is_today:
        # Cumulative-so-far day: give the fair "expected by now" baseline and a flag.
        exp_so_far = (round(pv_fc - pv_remaining, 2)
                      if (pv_fc is not None and pv_remaining is not None) else None)
        out["in_progress"] = True
        out["as_of"] = _hm(last.get("ts"))
        out["pv_forecast_remaining_kwh"] = pv_remaining
        out["pv_expected_so_far_kwh"] = exp_so_far   # compare pv_actual_kwh to THIS, not pv_forecast_kwh
    else:
        # Completed day: a real whole-day forecast error is meaningful.
        out["pv_forecast_err_kwh"] = (round(pv_act - pv_fc, 2)
                                      if pv_fc is not None and pv_act is not None else None)
    return out


def _gather(days: int, detail_days: int = 2) -> dict:
    """Build the performance payload: per-day summaries for `days`, plus trimmed
    per-slot records for the most recent `detail_days` (so specific questions like
    'why did we sell at 15:00 yesterday' can be answered from the actual records)."""
    today = datetime.now().date()
    summaries, detail = {}, {}
    for i in range(days):
        d = today - timedelta(days=i)
        recs = _read_day(d)
        if not recs:
            continue
        key = d.strftime("%Y-%m-%d")
        summaries[key] = _day_summary(recs, is_today=(d == today))
        if i < detail_days:
            detail[key] = {
                "cycles": [{**_trim(r, _CYCLE_FIELDS), "ts": _hm(r.get("ts"))}
                           for r in recs if r.get("kind") == "cycle"],
                "settlements": [{**_trim(r, _SETTLE_FIELDS), "ts": _hm(r.get("ts"))}
                                for r in recs if r.get("kind") == "settlement"],
            }
    return {"daily_summaries": summaries, "recent_detail": detail}


# --------------------------------------------------------------------------- #
# On-demand history retrieval (question path): the model can ask for more days.
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_NEED_RE = re.compile(r"NEED_HISTORY\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
_PROMPT_DATA_RE = re.compile(
    r"=== DATA \(JSON\) ===\n(.*?)\n=== END DATA ===",
    re.IGNORECASE | re.DOTALL,
)


def _history_manifest() -> dict:
    """List every day available in data/history/ plus the record schema, so the model
    knows exactly what it can ask for (it only ever sees the recent few days inline)."""
    # available_days spans both hot NDJSON and Parquet-compacted cold months.
    try:
        days = _hist.available_days(_data.history_dir())
    except OSError:
        days = []
    return {
        "dir": "data/history",
        "available_days": days,
        "earliest": days[0] if days else None,
        "latest": days[-1] if days else None,
        "count": len(days),
        "record_schema": {
            "cycle_fields": list(_CYCLE_FIELDS),
            "settlement_fields": list(_SETTLE_FIELDS),
            "note": "one JSON object per line; kind=cycle (a 15-min decision) or "
                    "kind=settlement (predicted vs actual for the slot that closed).",
        },
    }


def _parse_need_history(text: str, available_days: list[str], max_days: int) -> list[str]:
    """If the model's reply is a NEED_HISTORY directive, return the validated days it
    asked for (only days that actually exist; supports commas and A..B ranges). Returns
    [] when the reply is a normal answer, so a stray mention can't trigger retrieval."""
    if not text:
        return []
    stripped = text.strip()
    if not stripped.upper().startswith("NEED_HISTORY"):
        return []                     # the protocol requires the directive to stand alone
    m = _NEED_RE.search(stripped)
    if not m:
        return []
    avail = set(available_days or [])
    want = set()
    for tok in re.split(r"[,\s;]+", m.group(1).strip()):
        tok = tok.strip()
        if not tok:
            continue
        if ".." in tok:               # inclusive date range A..B
            a, _, b = tok.partition("..")
            a, b = a.strip(), b.strip()
            if _DATE_RE.match(a) and _DATE_RE.match(b):
                lo, hi = sorted((a, b))
                want |= {d for d in avail if lo <= d <= hi}
        elif _DATE_RE.match(tok) and tok in avail:
            want.add(tok)
    return sorted(want)[:max_days]


def _prompt_data_payload(user_prompt: str) -> dict:
    m = _PROMPT_DATA_RE.search(user_prompt or "")
    if not m:
        return {}
    try:
        payload = json.loads(m.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _daily_summary_metric_for_question(question: str | None) -> str | None:
    q = (question or "").lower()
    wants_total = any(term in q for term in (
        "total", "totals", "daily", "per day", "each day", "by day", "last ",
        "kwh", "consumption", "produced", "production",
    ))
    asks_for_point_detail = any(term in q for term in (
        "15-minute", "15 minute", "slot", "hourly", "at ", "around ",
    ))
    if not wants_total or asks_for_point_detail:
        return None
    if "load" in q or "consumption" in q or re.search(r"\bac\b", q):
        return "load_actual_kwh"
    if "pv" in q or "solar" in q or "produced" in q or "production" in q:
        return "pv_actual_kwh"
    if "import" in q:
        return "day_import_kwh"
    if "export" in q:
        return "day_export_kwh"
    if any(term in q for term in ("net", "p/l", "profit", "loss", "eur", "euro")):
        return "realized_net_eur"
    return None


def _inline_data_can_satisfy_history_request(
    question: str | None,
    requested_days: list[str],
    user_prompt: str,
) -> bool:
    """Return True when the first prompt already has the daily summary values needed.

    This is a guard against unnecessary history-file reads: models sometimes ask for
    NEED_HISTORY after seeing a manifest even though the aggregate daily answer is
    already in `performance.daily_summaries`.
    """
    metric = _daily_summary_metric_for_question(question)
    if not metric or not requested_days:
        return False
    payload = _prompt_data_payload(user_prompt)
    summaries = ((payload.get("performance") or {}).get("daily_summaries") or {})
    if not isinstance(summaries, dict):
        return False
    for ds in requested_days:
        day = summaries.get(ds)
        if not isinstance(day, dict) or day.get(metric) is None:
            return False
    return True


def _load_days(date_strs: list[str], conf) -> dict:
    """Pull the requested day files as compact, budget-bounded detail (same trimmed
    shape as _gather's recent_detail). Stops adding heavy detail once the char budget
    is hit, keeping at least each day's summary."""
    budget = _conf_int(conf, "ADVISOR_RETRIEVAL_MAX_CHARS", DEFAULT_RETRIEVAL_MAX_CHARS)
    out, used = {}, 0
    today = datetime.now().date()
    for ds in date_strs:
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            continue
        recs = _read_day(d)
        if not recs:
            continue
        summary = _day_summary(recs, is_today=(d == today))
        detail = {
            "cycles": [{**_trim(r, _CYCLE_FIELDS), "ts": _hm(r.get("ts"))}
                       for r in recs if r.get("kind") == "cycle"],
            "settlements": [{**_trim(r, _SETTLE_FIELDS), "ts": _hm(r.get("ts"))}
                            for r in recs if r.get("kind") == "settlement"],
        }
        block = {"summary": summary, **detail}
        chunk = len(json.dumps(block, default=str))
        if used + chunk > budget:
            out[ds] = {"summary": summary,
                       "note": "per-slot detail omitted (retrieval budget reached)"}
            continue
        out[ds] = block
        used += chunk
    return out


def _plan_excerpt() -> dict:
    raw = _data.load_raw_plan() or {}
    return {
        "generated_at": raw.get("generated_at"),
        "battery_soc": raw.get("battery_soc"),
        "current": raw.get("current"),
        "today": raw.get("today"),
        "next_slots": (raw.get("schedule") or [])[:12],
    }


def _live_excerpt():
    """Real-time power flow as of NOW, from the dashboard's MQTT feed. This is GROUND
    TRUTH for what the system is actually doing this instant — which can differ from
    the plan's forecast label for the current slot (e.g. at low SoC, PV surplus charges
    the battery even on an IDLE/PV_SURPLUS slot rather than exporting). Best-effort:
    returns None if the live feed isn't available."""
    try:
        from frontend.live import live
        s = live.snapshot() or {}
    except Exception:
        return None

    def _n(k):
        v = s.get(k)
        try:
            return round(float(v), 0) if v is not None else None
        except (TypeError, ValueError):
            return None

    g, b, pv, load = _n("grid_w"), _n("batt_w"), _n("pv_w"), _n("load_w")
    parts = []
    if g is not None:
        parts.append(f"grid {'importing' if g > 15 else 'exporting' if g < -15 else 'idle'} {abs(g):.0f}W")
    if b is not None:
        parts.append(f"battery {'charging' if b > 15 else 'discharging' if b < -15 else 'idle'} {abs(b):.0f}W")
    if pv is not None:
        parts.append(f"PV {pv:.0f}W")
    if load is not None:
        parts.append(f"house {load:.0f}W")
    return {
        "connected": s.get("connected"),
        "soc_pct": _n("soc"),
        "pv_w": pv, "load_w": load,
        "grid_w": g,            # + import / − export
        "batt_w": b,            # + charging / − discharging
        "ev_w": _n("ev_w"),
        "summary": "; ".join(parts) if parts else None,
    }


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
_PRIMER = """\
You are a senior energy-systems engineer reviewing a home battery ESS optimizer.

System: 16 kW 3-phase Victron ESS, ~42 kWh LFP battery, rooftop PV, in the
Netherlands on Tibber dynamic pricing (currently net-metering / "saldering", so
the buy and sell price are equal until it ends Jan 2027). A dynamic-program
optimizer re-plans every 15 minutes over the Tibber price horizon using PV and
load forecasts. Actions are executed by commanding Victron setpoints over MQTT: the
ESS runs in "Optimized WITHOUT BatteryLife" with DVCC managing the LFP charge/
discharge limits, and the inverters typically report "External control" while this
controller is driving them. There is NO BatteryLife scheduled charging in play — do
NOT attribute any behaviour to BatteryLife. One of four actions per slot, labelled
by the commanded setpoint:
  - IDLE   : neutral setpoint — the inverter self-consumes PV and decides charge vs
             export within DVCC limits.
  - RETAIN : grid covers the house load, battery held (no forced charge/discharge).
  - BUY    : commanded full-power grid charge, held until the planned target SoC.
  - SELL   : commanded grid export at a metered setpoint (forced discharge).
If the battery is NOT charging from PV surplus, the cause is the commanded setpoint,
an active feed-in / export limit, or a DVCC current limit — never BatteryLife.
History records are 15-min "cycle" rows (the decision + realized power) paired with
"settlement" rows (predicted vs actual for the slot that just closed). A persistent
cost-basis tracks what stored energy cost; a min-sell-price floor and an arbitrage
margin prune marginal cycles; SELL hysteresis damps churn.

TIME / PARTIAL DAYS — read this carefully. `now` is the current time. The most recent
day is normally IN PROGRESS (its summary has `in_progress: true` and an `as_of` time);
its `*_actual_kwh`, net, and counts are cumulative SO FAR, not full-day totals. PV is
produced only during daylight, so before mid-morning the day's actual PV is naturally
near zero — that is NOT a forecast miss. NEVER compare a whole-day forecast
(`pv_forecast_kwh`) to a partial-day actual. For an in-progress day, compare
`pv_actual_kwh` only against `pv_expected_so_far_kwh` (what the forecast says should
already have been produced by `as_of`); if they're close, the forecast is on track.
Assess true full-day forecast accuracy only on COMPLETED days (which carry
`pv_forecast_err_kwh`). The same partial-day caveat applies to load and net.

LIVE STATE — this is GROUND TRUTH for "right now". `live_now` is the real-time power
flow as of `now` (signs: `grid_w` + import / − export; `batt_w` + charging /
− discharging; `pv_w` production; `load_w` house). When describing what the system is
doing this instant, trust `live_now`, NOT the plan's label for the current slot (the
plan is a forecast and can lag reality). Critically: when SoC is low and PV exceeds the
house load, the battery is CHARGING from the PV surplus (`batt_w` > 0) — it is NOT
exporting — even on a slot the plan calls IDLE / PV_SURPLUS. Only say surplus is
"exporting to grid" when `grid_w` is actually negative. "Excess PV that can't be stored"
exists only when the battery is full (or DVCC is capping charge); at low SoC there is
no such excess — the PV is filling the battery.

EV Charging — the system can charge an EV from the grid, PV, or the battery. The 
EV is treated as a house load and is a 3 phase charger cabable of up to 17kw and
is controlled by a Maxem.io charge controller to prevent overloading a phase if 
the house load is high (it reduces the charge rate temporarily until the load 
decreases).

You are an ADVISOR only. You cannot change anything. Recommend, explain, and
prioritise — the human applies changes separately and safely."""

_REVIEW_TASK = """\
TASK: Produce a SHORT morning review — something the user can scan in ~15 seconds.

LENGTH (obey strictly):
  - Total UNDER 250 words. No preamble, no sign-off, no "watch list", no recap of
    the data.
  - Use exactly these sections; omit a section entirely if there is nothing real to
    say:
      **P/L** — one line: made or lost money so far + the single main reason.
      **Good** — one or two lines: what is going right.
      **Issues** — up to 3 bullets, real economic problems only; one short clause of
                   explanation each is fine.
      **Do** — up to 3 bullets, each = tunable name + value + short why (or "code:"
               for a code change). Omit this section entirely if nothing is worth
               changing (see "FINDING NOTHING" below).
  - Cite a time/number where it supports the point. No confidence/risk labels,
    no nested sub-bullets.

DO NOT FLAG ANY OF THESE — they are intended and pre-approved, not problems:
  - Low, very low, or 0% battery SOC; an empty/drained battery; running the house
    off solar or cheap grid while SOC is low. "0%" is really ~5% (the BMS floor);
    draining to that level to arbitrage or self-consume is desired behaviour. Never
    call it an issue, a risk, or a "missed opportunity to store PV".
  - RETAIN or IDLE while SOC is low.
Only surface genuine money mistakes: churn, selling below cost basis, mis-timed
charge/sell, or forecast/settlement errors that actually cost euros.

BEFORE putting any tunable in **Do**, verify all three against the DATA; drop it if
it fails any:
  1. The tunable NAME appears in the provided `tunables` list — never invent one.
  2. Your value actually DIFFERS from the current value (no no-op suggestions).
  3. It really does what you claim, checked against the prices/SoC in `current_plan`.
     E.g. a max-charge-PRICE cap must sit ABOVE the slots you want to allow (a lower
     cap blocks them); charging earlier only helps if a later slot is pricier or
     time/capacity runs out.

FINDING NOTHING IS A VALID, GOOD RESULT. If the recent schedule and yesterday look
correct and well executed, say so plainly — e.g. "No changes recommended: yesterday
executed as intended and today's plan looks sound" — and stop. Do NOT manufacture
issues or tweaks just to fill **Issues** or **Do**; omit those sections when empty."""

_QUESTION_TASK = """\
TASK: Answer the user's question below using the data provided. Be specific and
cite the relevant records (times, prices, SoC, actions, reason codes). If the
question implies a possible improvement, note whether it would be an existing
tunable, a new tunable, or a code change.

Be brief. Low/0% SOC (really ~5%, the BMS floor) and draining the battery to run off
solar or cheap grid are intended and pre-approved — never flag them as problems.

SOURCE ORDER — Use the user's prompt, conversation_context, and inline data first.
If they already contain the answer, answer directly. Do not request history just
because history_manifest says files exist. For daily totals, `performance.daily_summaries`
is authoritative: AC/house load totals are
`performance.daily_summaries[date].load_actual_kwh`, PV totals are `pv_actual_kwh`,
grid import is `day_import_kwh`, grid export is `day_export_kwh`, and economics are
`realized_net_eur`. Say data is missing only when the date/field is absent from the
user prompt, conversation_context, and inline data.

DEEPER HISTORY: `history_manifest` lists EVERY day available in data/history/ plus
the record schema. The inline `performance` data only covers the most recent few days
in detail, but daily_summaries can still answer daily aggregate questions. NEED_HISTORY only when
the answer requires missing dates, missing fields, or slot-level records that are not already
in the user's prompt, conversation_context, performance.daily_summaries, or recent_detail.
If — and ONLY if — answering needs day(s) outside that available inline/chat context, do not
guess and do not say you lack data: instead make your ENTIRE reply exactly one line and nothing else —
  NEED_HISTORY: <comma-separated YYYY-MM-DD, and/or A..B ranges>
naming only days present in history_manifest (max {max_days}). You will be re-asked
with those days attached, and then you answer. If the inline data already suffices,
just answer — never request history you don't need.

USER QUESTION: {question}"""


def _build_messages(question: str | None, conf, conversation_context: str | None = None) -> tuple[str, str]:
    """Build (system, user). Keeps the prompt under ADVISOR_MAX_INPUT_CHARS by
    progressively reducing how many days of per-slot DETAIL are included (daily
    summaries are always kept), then hard-truncating as a last resort. This bounds
    the input token cost of a review."""
    max_chars = _conf_int(conf, "ADVISOR_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS)
    days = _conf_int(conf, "ADVISOR_HISTORY_DAYS", DEFAULT_HISTORY_DAYS)
    base = {"tunables": _tunables(conf), "current_plan": _plan_excerpt(),
            "live_now": _live_excerpt(),   # ground-truth real-time power flow
            "now": datetime.now().astimezone().isoformat()}
    if conversation_context:
        base["conversation_context"] = conversation_context
    if question:
        max_days = _conf_int(conf, "ADVISOR_RETRIEVAL_MAX_DAYS", DEFAULT_RETRIEVAL_MAX_DAYS)
        task = _QUESTION_TASK.format(question=question.strip(), max_days=max_days)
        base["history_manifest"] = _history_manifest()   # so it knows what it can pull
    else:
        task = _REVIEW_TASK

    user = ""
    for detail_days in (2, 1, 0):        # shrink detail until it fits the budget
        payload = {**base, "performance": _gather(days, detail_days=detail_days)}
        user = (f"{task}\n\n=== DATA (JSON) ===\n"
                f"{json.dumps(payload, default=str)}\n=== END DATA ===")
        if len(user) <= max_chars:
            break
    if len(user) > max_chars:            # last resort: hard cap
        user = user[:max_chars] + "\n…(data truncated to fit the input budget)…\n=== END DATA ==="
    return _PRIMER, user


def _conf_int(conf, key, default):
    try:
        return int(float(conf.get(key)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Claude call
# --------------------------------------------------------------------------- #
def _call_claude_cli(system: str, user: str, model: str, token: str | None, conf) -> dict:
    """Run the analysis through the Claude Code CLI on the host's Pro/Max
    subscription (no API key). Uses an explicit OAuth token if given, otherwise the
    host's existing `claude` login. Read-only: the whole prompt + data is fed on
    stdin, it runs in a neutral temp dir (nothing local to touch), plain text back."""
    import subprocess
    import tempfile

    cli = (conf.get("CLAUDE_CLI_PATH") or "claude").strip()
    prompt = f"{system}\n\n{user}"
    cmd = [cli, "--print"]
    if model:
        cmd += ["--model", model]

    env = dict(os.environ)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.pop("ANTHROPIC_API_KEY", None)   # prefer the subscription login over API credits
    env["MAX_THINKING_TOKENS"] = str(_conf_int(conf, "ADVISOR_MAX_THINKING_TOKENS",
                                               DEFAULT_MAX_THINKING_TOKENS))
    cfgdir = (conf.get("CLAUDE_CONFIG_DIR") or "").strip()
    if cfgdir:
        env["CLAUDE_CONFIG_DIR"] = cfgdir   # isolate from a possibly-stale ~/.claude cache
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=180, env=env, cwd=tempfile.gettempdir())
    except FileNotFoundError:
        return {"ok": False, "error": f"Claude Code CLI '{cli}' not found on this host. "
                "Install it (npm i -g @anthropic-ai/claude-code) and run `claude setup-token`, "
                "or set CLAUDE_CLI_PATH to its location."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Claude Code timed out (>180s)."}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:600]
        return {"ok": False, "error": f"Claude Code error: {err or 'non-zero exit'}"}
    text = (proc.stdout or "").strip()
    return {"ok": True, "report": text or "_(no content returned)_", "usage": None}


def _call_claude_api(system: str, user: str, model: str, api_key: str) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "The 'anthropic' SDK is not installed. "
                                      "Run: pip install anthropic"}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        usage = getattr(resp, "usage", None)
        return {
            "ok": True,
            "report": text or "_(no content returned)_",
            "usage": {"input_tokens": getattr(usage, "input_tokens", None),
                      "output_tokens": getattr(usage, "output_tokens", None)} if usage else None,
        }
    except Exception as e:  # anthropic.APIError and friends
        return {"ok": False, "error": f"Claude API error: {e}"}


# --------------------------------------------------------------------------- #
# Streaming (Server-Sent Events) — transparent progress + token output
# --------------------------------------------------------------------------- #
def _extract_delta(ev: dict):
    """Pull assistant text out of a Claude Code stream-json event, tolerant of the
    several shapes the CLI emits across versions. Returns a list of text fragments."""
    out = []
    t = ev.get("type")
    # token-level deltas: top-level, or nested under a stream_event "event" wrapper.
    for d in (ev.get("delta"), (ev.get("event") or {}).get("delta")):
        if isinstance(d, dict) and d.get("text"):
            out.append(d["text"])
    # a full assistant message (fallback when partials aren't emitted)
    msg = ev.get("message") if t in ("assistant", None) else None
    if isinstance(msg, dict):
        for blk in (msg.get("content") or []):
            if isinstance(blk, dict) and blk.get("type") in (None, "text") and blk.get("text"):
                out.append(blk["text"])
    return out


def _stream_cli(system, user, model, token, conf):
    """Stream the Claude Code CLI: progress 'log' events + 'delta' text as it arrives.
    Kills the subprocess if the consumer (SSE client) goes away."""
    import subprocess
    import tempfile
    import shlex

    cli = (conf.get("CLAUDE_CLI_PATH") or "claude").strip()
    stream_args = shlex.split((conf.get("ADVISOR_CLI_STREAM_ARGS") or DEFAULT_STREAM_ARGS))
    prompt = f"{system}\n\n{user}"
    cmd = [cli, "--print", *stream_args]
    if model:
        cmd += ["--model", model]

    env = dict(os.environ)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.pop("ANTHROPIC_API_KEY", None)
    # Cap (default disable) extended thinking — it was the runaway token sink.
    env["MAX_THINKING_TOKENS"] = str(_conf_int(conf, "ADVISOR_MAX_THINKING_TOKENS",
                                               DEFAULT_MAX_THINKING_TOKENS))
    cfgdir = (conf.get("CLAUDE_CONFIG_DIR") or "").strip()
    if cfgdir:
        env["CLAUDE_CONFIG_DIR"] = cfgdir   # isolate from a possibly-stale ~/.claude cache

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env=env, cwd=tempfile.gettempdir())
    except FileNotFoundError:
        yield {"type": "error", "error": f"Claude Code CLI '{cli}' not found on this host. "
               "Install it (npm i -g @anthropic-ai/claude-code) and log in / set "
               "CLAUDE_CODE_OAUTH_TOKEN, or set CLAUDE_CLI_PATH."}
        return

    emitted = False
    thinking = 0
    thinking_last = 0.0
    auth_fail = False
    start = time.time()
    # Feed the (large) prompt on a background thread so a full pipe buffer can't
    # deadlock against us reading stdout.
    def _feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            pass
    threading.Thread(target=_feed, daemon=True).start()
    try:
        for line in proc.stdout:
            if time.time() - start > ADVISOR_TIMEOUT_S:
                yield {"type": "error", "error": f"Timed out after {ADVISOR_TIMEOUT_S}s."}
                break
            line = line.rstrip("\n")
            if not line.strip():
                continue
            # Auth failures can arrive as stderr OR inside a JSON result event.
            low = line.lower()
            if any(k in low for k in ("401", "invalid authentication", "unauthor", "invalid_grant")):
                auth_fail = True
            try:
                ev = json.loads(line)
            except ValueError:
                yield {"type": "log", "msg": line[:600]}   # non-JSON (e.g. stderr) -> log
                continue
            t = ev.get("type")
            deltas = _extract_delta(ev)
            if deltas and not (emitted and t in ("assistant",)):
                # token deltas always flow; a full 'assistant' message is skipped if
                # we already streamed partials (avoids duplicating the text).
                for frag in deltas:
                    emitted = True
                    yield {"type": "delta", "text": frag}
            elif t == "result":
                if not emitted and ev.get("result"):
                    emitted = True
                    yield {"type": "delta", "text": ev["result"]}
                u = ev.get("usage") or {}
                cost = ev.get("total_cost_usd")
                yield {"type": "log", "msg": "result "
                       f"in:{u.get('input_tokens', '?')} out:{u.get('output_tokens', '?')}"
                       + (f" ${cost}" if cost else "")}
            elif t in ("system", "user"):
                sub = ev.get("subtype") or "event"
                if sub == "thinking_tokens":
                    # Coalesce the high-frequency thinking stream into one throttled,
                    # in-place "thinking…" indicator instead of spamming the log.
                    thinking += 1
                    nowt = time.time()
                    if nowt - thinking_last > 0.5:
                        thinking_last = nowt
                        yield {"type": "thinking", "count": thinking}
                elif sub == "init":
                    yield {"type": "log", "msg": "session started — Claude is working…"}
                # other system/status events are noise — ignore
        rc = proc.wait()
        if auth_fail and not emitted:
            yield {"type": "error", "error": (
                "Claude authentication failed (401). This is a known Claude Code issue: "
                "the cached credential state at ~/.claude/.credentials.json goes stale and "
                "rejects even a valid token. Recovery: delete that file (or `claude logout`), "
                "re-run `claude setup-token`, update CLAUDE_CODE_OAUTH_TOKEN in .secrets, and "
                "restart the frontend. To make it more durable, set CLAUDE_CONFIG_DIR to a "
                "dedicated dir so the advisor uses only the token and isn't poisoned by the "
                "interactive login's cache.")}
        elif rc not in (0, None) and not emitted:
            yield {"type": "error", "error": f"Claude Code exited with code {rc}."}
    finally:
        if proc.poll() is None:        # consumer gone or we're done — never leave a zombie
            try:
                proc.kill()
            except Exception:
                pass


def _stream_generic_cli(system, user, conf):
    """Provider-agnostic CLI path. Runs ADVISOR_CLI_CMD (e.g. the Gemini or OpenAI
    Codex CLI, authenticated by your own subscription login), feeds the prompt on
    stdin, and streams stdout back as the report. Lets the advisor use ANY
    subscription-login CLI, not just Claude Code — no API key, no per-call charge.
    The prompt is delivered two ways depending on the CLI:
      * if ADVISOR_CLI_CMD contains the literal token {prompt}, it's substituted as
        a single argument (for CLIs that want the prompt as a flag value);
      * otherwise the prompt is piped on stdin.
    Examples (set in .env):
        ADVISOR_CLI_CMD=gemini -p {prompt}   # Gemini CLI (after `gemini login`)
        ADVISOR_CLI_CMD=codex exec {prompt}  # OpenAI Codex CLI (after signing in)
        ADVISOR_CLI_CMD=claude --print       # any CLI that reads the prompt on stdin
    """
    import subprocess
    import tempfile
    import shlex

    raw = (conf.get("ADVISOR_CLI_CMD") or "").strip()
    tokens = shlex.split(raw)
    if not tokens:
        yield {"type": "error", "error": "ADVISOR_CLI_CMD is not set."}
        return
    prompt = f"{system}\n\n{user}"
    use_stdin = "{prompt}" not in raw
    cmd = [prompt if t == "{prompt}" else t for t in tokens]
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.Popen(cmd,
                                stdin=(subprocess.PIPE if use_stdin else subprocess.DEVNULL),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env, cwd=tempfile.gettempdir())
    except FileNotFoundError:
        yield {"type": "error", "error": f"Command not found: {tokens[0]!r}. Install the CLI "
               "(e.g. `gemini login` / `codex`) or fix ADVISOR_CLI_CMD."}
        return

    if use_stdin:
        def _feed():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except Exception:
                pass
        threading.Thread(target=_feed, daemon=True).start()

    emitted = False
    start = time.time()
    try:
        for line in proc.stdout:
            if time.time() - start > ADVISOR_TIMEOUT_S:
                yield {"type": "error", "error": f"Timed out after {ADVISOR_TIMEOUT_S}s."}
                break
            emitted = True
            yield {"type": "delta", "text": line}   # raw text/markdown from the CLI
        rc = proc.wait()
        if rc not in (0, None) and not emitted:
            yield {"type": "error", "error": f"CLI exited with code {rc}."}
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _call_generic_cli(system, user, conf) -> dict:
    """Non-streaming wrapper around _stream_generic_cli for the plain POST path."""
    parts, err = [], None
    for ev in _stream_generic_cli(system, user, conf):
        if ev.get("type") == "delta":
            parts.append(ev.get("text", ""))
        elif ev.get("type") == "error":
            err = ev.get("error")
    text = "".join(parts).strip()
    if not text:
        return {"ok": False, "report": "", "error": err or "No output from ADVISOR_CLI_CMD."}
    return {"ok": True, "report": text, "error": None}


def _stream_api(system, user, model, api_key):
    try:
        import anthropic
    except ImportError:
        yield {"type": "error", "error": "The 'anthropic' SDK is not installed (pip install anthropic)."}
        return
    try:
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(model=model, max_tokens=MAX_OUTPUT_TOKENS,
                                     system=system,
                                     messages=[{"role": "user", "content": user}]) as stream:
            for text in stream.text_stream:
                yield {"type": "delta", "text": text}
    except Exception as e:
        yield {"type": "error", "error": f"Claude API error: {e}"}


def _auth_log_event(mode, conf) -> dict:
    """Non-secret diagnostic line about which backend/credential is in use."""
    if mode == "custom":
        cmd0 = (conf.get("ADVISOR_CLI_CMD") or "").split()
        return {"type": "log", "msg": "auth=custom · cmd=" + (cmd0[0] if cmd0 else "?")}
    if mode == "cli":
        tok = _oauth_token(conf)
        return {"type": "log", "msg": "auth=cli · token=" +
                (f"present ({len(tok)} chars)" if tok else "absent → using host `claude` login")
                + ((" · CLAUDE_CONFIG_DIR=" + conf.get("CLAUDE_CONFIG_DIR"))
                   if conf.get("CLAUDE_CONFIG_DIR") else "")}
    return {"type": "log", "msg": "auth=api"}


def _stream_for(mode, system, user, model, conf):
    """Dispatch one streamed model call to the active backend (yields event dicts)."""
    if mode == "custom":
        yield from _stream_generic_cli(system, user, conf)
    elif mode == "cli":
        yield from _stream_cli(system, user, model, _oauth_token(conf), conf)
    else:
        yield from _stream_api(system, user, model, _api_key(conf))


def _answer_with_retrieval(question, conf, mode, model, conversation_context: str | None = None):
    """Question path with on-demand history. Streams the answer live, but sniffs the
    first line: if the model replies with a NEED_HISTORY directive instead of an
    answer, pull those day files from data/history/ and re-ask (pass 2), now streaming
    the real answer. The daily review never comes through here, so it stays cheap."""
    manifest = _history_manifest()
    system, user = _build_messages(question, conf, conversation_context=conversation_context)
    yield {"type": "stage", "msg": f"Prompt ~{len(system) + len(user):,} chars. Asking {model}…"}

    # --- Pass 1: stream, but hold back the first line to detect a directive. ---
    buf, decided, is_request, captured, err_ev = "", False, False, [], None
    for ev in _stream_for(mode, system, user, model, conf):
        t = ev.get("type")
        if t == "delta":
            txt = ev.get("text", "")
            captured.append(txt)
            if decided:
                yield ev
                continue
            buf += txt
            if "\n" in buf or len(buf) >= 16:          # enough to judge the first line
                if buf.lstrip().upper().startswith("NEED_HISTORY"):
                    is_request, decided = True, True    # suppress; consume rest quietly
                else:
                    decided = True
                    yield {"type": "delta", "text": buf}   # flush, then stream live
        elif t == "error":
            err_ev = ev
            break
        elif t in ("thinking", "stage", "log"):
            yield ev

    # Resolve a very short pass-1 reply that never crossed the decision threshold.
    if not decided:
        if buf.lstrip().upper().startswith("NEED_HISTORY"):
            is_request = True
        elif buf:
            yield {"type": "delta", "text": buf}
    if err_ev is not None:
        yield err_ev
        return
    if not is_request:
        return                                   # a normal answer was already streamed

    # --- Retrieval: resolve the requested days and re-ask. ---
    max_days = _conf_int(conf, "ADVISOR_RETRIEVAL_MAX_DAYS", DEFAULT_RETRIEVAL_MAX_DAYS)
    want = _parse_need_history("".join(captured), manifest.get("available_days") or [], max_days)
    if not want:
        yield {"type": "stage", "msg": "Model asked for history, but no matching days "
               "exist — answering from inline data."}
        user2 = user + ("\n\n(You requested more history but no matching days exist. "
                        "Answer with the data already provided; do not request more.)")
    elif _inline_data_can_satisfy_history_request(question, want, user):
        yield {"type": "stage", "msg": "Requested history is already in the inline daily "
               "summaries — re-asking without file retrieval."}
        user2 = (user + "\n\n(Your first reply requested history, but the inline JSON "
                 "already contains the requested daily summary values in "
                 f"performance.daily_summaries for: {', '.join(want)}. Use those "
                 "values now. Do NOT request more history.)")
    else:
        loaded = _load_days(want, conf)
        yield {"type": "stage", "msg": f"Pulled {len(loaded)} day(s): "
               f"{', '.join(sorted(loaded))}. Re-asking…"}
        extra = json.dumps({"requested_history": loaded}, default=str)
        user2 = (user + "\n\n=== ADDITIONAL HISTORY (you requested this) ===\n"
                 + extra + "\n=== END ADDITIONAL HISTORY ===\n\n"
                 "Now answer the question using ALL data above. A day with only a "
                 "summary and a retrieval-budget note is still present and valid for "
                 "daily aggregate totals; do not call it missing unless the needed "
                 "summary field itself is absent. Do NOT request more history.")
    yield from _stream_for(mode, system, user2, model, conf)


def run_stream(question: str | None = None):
    """Generator of SSE event dicts (stage/log/delta/done/error) for live progress.
    Read-only. The lock is released — and any CLI subprocess killed — when the
    generator closes, including when the browser disconnects, so a wedged run can
    never leave the advisor stuck on 'already running'."""
    question_text = (question or "").strip() or None
    mode_name = "question" if question_text else "review"
    chat = latest_report()
    conversation = _conversation_context(chat)
    conf = _conf()
    mode = _auth_mode(conf)
    if not mode:
        error = (
            "No Claude credentials configured. If `claude` is already logged in on "
            "this host, set ADVISOR_AUTH=cli. Otherwise set CLAUDE_CODE_OAUTH_TOKEN "
            "in .secrets, or ANTHROPIC_API_KEY for API use."
        )
        now = datetime.now().astimezone().isoformat()
        _append_user_message(chat, mode_name, question_text, now)
        _append_assistant_message(
            chat, text="", created_at=now, model=None, auth=None,
            mode=mode_name, elapsed_s=0, ok=False, error=error,
        )
        _save_chat(chat)
        yield {"type": "error", "error": error}
        return
    model = _model(conf, mode)
    if not _run_lock.acquire(blocking=False):
        yield {"type": "error", "error": "An advisor review is already running — please wait."}
        return
    t0 = time.time()
    report_parts = []
    error_msg = None
    try:
        started_at = datetime.now().astimezone().isoformat()
        _append_user_message(chat, mode_name, question_text, started_at)
        _save_chat(chat)
        yield {"type": "stage", "msg": f"Gathering history + tunables ({mode})…"}
        yield _auth_log_event(mode, conf)
        if question_text:
            # Question path: may pull deeper history from data/history/ on demand.
            events = _answer_with_retrieval(
                question_text, conf, mode, model, conversation_context=conversation
            )
        else:
            system, user = _build_messages(None, conf, conversation_context=conversation)
            yield {"type": "stage", "msg": f"Prompt ~{len(system) + len(user):,} chars. "
                   f"Calling {model}…"}
            events = _stream_for(mode, system, user, model, conf)
        for ev in events:
            if ev.get("type") == "delta":
                report_parts.append(ev.get("text", ""))
            elif ev.get("type") == "error":
                error_msg = ev.get("error")
                now = datetime.now().astimezone().isoformat()
                _append_assistant_message(
                    chat, text="".join(report_parts).strip(), created_at=now,
                    model=model, auth=mode, mode=mode_name,
                    elapsed_s=round(time.time() - t0, 1), ok=False, error=error_msg,
                )
                _save_chat(chat)
            yield ev
            if ev.get("type") == "error":
                return
        elapsed = round(time.time() - t0, 1)
        generated_at = datetime.now().astimezone().isoformat()
        report = "".join(report_parts).strip()
        if not report and not error_msg:
            error_msg = "No answer produced."
        _append_assistant_message(
            chat, text=report, created_at=generated_at, model=model, auth=mode,
            mode=mode_name, elapsed_s=elapsed, ok=bool(report and not error_msg),
            error=error_msg,
        )
        _save_chat(chat)
        yield {"type": "done", "model": model, "auth": mode,
               "mode": mode_name, "elapsed_s": elapsed, "generated_at": generated_at}
    except GeneratorExit:
        raise
    except Exception as e:
        error = f"Advisor failed: {e}"
        now = datetime.now().astimezone().isoformat()
        _append_assistant_message(
            chat, text="".join(report_parts).strip(), created_at=now,
            model=model, auth=mode, mode=mode_name,
            elapsed_s=round(time.time() - t0, 1), ok=False, error=error,
        )
        _save_chat(chat)
        yield {"type": "error", "error": error}
    finally:
        _run_lock.release()


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run(question: str | None = None) -> dict:
    """Run the advisor (default review, or answer `question`). Returns a dict with
    ok / report / model / auth / generated_at / error. Read-only and best-effort."""
    question_text = (question or "").strip() or None
    mode_name = "question" if question_text else "review"
    chat = latest_report()
    conversation = _conversation_context(chat)
    conf = _conf()
    mode = _auth_mode(conf)
    if not mode:
        error = (
            "No Claude credentials configured. If `claude` is already installed and "
            "logged in on this host, set ADVISOR_AUTH=cli in .env to use that login. "
            "Otherwise put a CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) in "
            ".secrets, or set ANTHROPIC_API_KEY for pay-as-you-go API use."
        )
        generated_at = datetime.now().astimezone().isoformat()
        _append_user_message(chat, mode_name, question_text, generated_at)
        _append_assistant_message(
            chat, text="", created_at=generated_at, model=None, auth=None,
            mode=mode_name, ok=False, error=error,
        )
        _save_chat(chat)
        return {"ok": False, "model": None, "error": error, "generated_at": generated_at}

    model = _model(conf, mode)
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "model": model,
                "error": "An advisor review is already running — please wait."}
    try:
        started_at = datetime.now().astimezone().isoformat()
        _append_user_message(chat, mode_name, question_text, started_at)
        _save_chat(chat)
        if question_text:
            # Question path: reuse the streaming retrieval orchestrator, collected.
            parts, err = [], None
            for ev in _answer_with_retrieval(
                question_text, conf, mode, model, conversation_context=conversation
            ):
                t = ev.get("type")
                if t == "delta":
                    parts.append(ev.get("text", ""))
                elif t == "error":
                    err = ev.get("error")
            text = "".join(parts).strip()
            result = ({"ok": True, "report": text, "error": None} if text
                      else {"ok": False, "report": "", "error": err or "No answer produced."})
        else:
            system, user = _build_messages(None, conf, conversation_context=conversation)
            if mode == "custom":
                result = _call_generic_cli(system, user, conf)
            elif mode == "cli":
                result = _call_claude_cli(system, user, model, _oauth_token(conf), conf)
            else:
                result = _call_claude_api(system, user, model, _api_key(conf))
        result["model"] = model
        result["auth"] = mode
        result["generated_at"] = datetime.now().astimezone().isoformat()
        result["mode"] = mode_name
        result["question"] = question_text
        _append_assistant_message(
            chat, text=result.get("report") or "", created_at=result["generated_at"],
            model=model, auth=mode, mode=mode_name, ok=bool(result.get("ok")),
            error=result.get("error"),
        )
        _save_chat(chat)
        return result
    except Exception as e:
        generated_at = datetime.now().astimezone().isoformat()
        error = f"Advisor failed: {e}"
        _append_assistant_message(
            chat, text="", created_at=generated_at, model=model, auth=mode,
            mode=mode_name, ok=False, error=error,
        )
        _save_chat(chat)
        return {"ok": False, "model": model, "error": error,
                "generated_at": generated_at}
    finally:
        _run_lock.release()
