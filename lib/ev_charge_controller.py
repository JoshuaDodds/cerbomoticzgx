import datetime
import json
import math
import os
import tempfile
import time
import urllib3
import pytz
import threading
from pathlib import Path

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.tesla_api import TeslaApi
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message, is_truthy
from lib.notifications import pushover_notification, pushover_notification_critical
from lib.ev_smart_charge import load_job, load_plan_snapshot


# Charge-control tuning.
SURPLUS_MIN_AMPS = 2          # below this there isn't enough PV to bother charging
SURPLUS_LOSS_GRACE_S = 60     # ride out passing clouds before stopping a surplus charge
COMMAND_COOLDOWN_S = 60       # min spacing between start/stop/amp commands (anti-chatter + budget)
AMP_ADJUST_MIN_DELTA = 1      # only re-issue a set-amps command when it moves by >= this
STOP_RETRY_BACKOFF_S = 60     # retry an unconfirmed safety stop within the required minute
STOP_ALERT_INTERVAL_S = 900   # min spacing between "could not stop the car" Pushover alerts
STOP_MAX_RETRIES = 5          # bounded auto-retry attempts before escalating to a human (audit
                               # finding: an uncapped critical-bypass retry loop can blow past
                               # the Tesla budget guard entirely if the car stays unreachable)
STALE_STATUS_MAX_AGE_S = 300  # a cached tesla.is_charging older than this is UNKNOWN, not
                               # authoritative "not charging"; a stale cache must not suppress
                               # a necessary stop command
# Surplus-driven "is the car here?" discovery wakes are rate-limited so an away/asleep car
# can't drain the wake budget; the tesla_budget guard is the hard backstop on top.
DISCOVERY_WAKE_INTERVAL_S = 3600     # at most one surplus-discovery wake per hour
DISCOVERY_AWAY_BACKOFF_S = 10800     # after finding the car NOT HOME, wait 3h before another wake
DISCOVERY_HOME_UNPLUGGED_BACKOFF_S = 1200  # home but not plugged -> recheck sooner (20m)

# A planner snapshot is refreshed on the normal quarter-hour broker cycle.  A little over one
# cycle allows ordinary scheduling jitter without permitting an abandoned plan to start a car.
SMART_PLAN_MAX_AGE_S = 20 * 60
SMART_SCHEDULE_RETRY_S = 60
SMART_COMMAND_RETRY_S = 60
SMART_COMMAND_ACK_TIMEOUT_S = 60
SMART_COMMAND_MAX_ATTEMPTS = 3
# Stable, application-owned ID. Tesla's own command proxy creates schedule IDs from Unix
# seconds; use this feature's 2026-07-21 UTC epoch rather than an arbitrary uint64 so legacy
# vehicle firmware receives the same shape it creates itself. Only this exact ID is touched.
SMART_OWNED_SCHEDULE_ID = 1_784_592_000
# The first branch implementation briefly used this decorative uint64. It reached the live
# vehicle before the compatibility correction above, so cleanup paths must retire that exact
# application-owned ID too. Never widen this into list-and-delete behavior: user schedules are
# deliberately outside our ownership.
SMART_LEGACY_OWNED_SCHEDULE_IDS = (4_847_371_018_685_470_720,)
SMART_STATE_PREFIX = "ev_smart_charge_"
SMART_CONTROLLER_STATE_PATH = Path("data/ev_smart_charge_controller_state.json")
_SMART_CONTROLLER_STATE_LOCK = threading.RLock()


def _num(value, default=0.0):
    """Coerce an on-bus value (which may be a str or None) to float without throwing."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Dedicated EV-charge intent flag. This is DELIBERATELY separate from the ESS grid-assist
# toggle ('grid_charging_enabled'): grid-assist controls the house battery only and must
# never start or stop the car. A future dedicated "charge the car now" button will set this
# key (and could source from grid OR battery — e.g. draining the pack for transport). Until
# that button exists it stays unset, so only PV-surplus charging can engage the car.
EV_CHARGE_INTENT_KEY = "ev_charge_requested"

# One-shot "Refresh Data" button (Vehicle tab). Setting this to True wakes the car and forces
# an immediate status refresh outside the normal engagement gating, then main() clears it back
# to False so it fires exactly once per press rather than on every subsequent tick.
REFRESH_REQUEST_KEY = "vehicle_refresh_requested"
MANUAL_STOP_REQUEST_KEY = "vehicle_stop_requested"


PROPERTY_MAPPING = {
    "charging_watts": "tesla_power",
    "surplus_watts": "surplus_watts",
    "surplus_amps": "surplus_amps",
    "no_sun_production": "is_the_sun_shining",
    "pv_watts": "pv_power",
    "pv_amps": "pv_current",
    "ess_soc": "batt_soc",
    "ess_volts": "batt_voltage",
    "ess_watts": "batt_power",
    "ess_max_charge_voltage": "max_charge_voltage",
    "acin_watts": "ac_in_power",
    "acout_watts": "ac_out_power",
    "acload_watts": "ac_out_adjusted_power",
    "l1_charging_amps": "tesla_l1_current",
    "l2_charging_amps": "tesla_l2_current",
    "l3_charging_amps": "tesla_l3_current",
    "charging_amps": "tesla_charging_amps_total"
}


class DynamicProperty:
    def __init__(self, key):
        self.key = key

    def __get__(self, instance, owner):
        if instance is None:
            return self

        return instance.global_state.get(self.key)

def create_property(property_name: str, key: str):
    setattr(EvCharger, property_name, DynamicProperty(key))


class EvCharger:

    _http = urllib3.PoolManager()
    tz = pytz.timezone('Europe/Amsterdam')

    def __init__(self):
        logging.info("EvCharger (__init__): Initializing...")

        self.main_thread = None
        self.global_state = GlobalStateClient()

        # Dynamically creates properties from PROPERTY_MAPPING dict
        for property_name, key in PROPERTY_MAPPING.items():
            create_property(property_name, key)

        # int(float(...)) tolerates a float-formatted .env value (e.g. "95.0" from the Config editor).
        self.load_reservation = int(float(retrieve_setting("LOAD_RESERVATION") or 0))
        self.load_reservation_is_reduced = False
        self.load_reservation_reduction_factor = float(retrieve_setting("LOAD_REDUCTION_FACTOR") or 0) or 1
        self.minimum_ess_soc = int(float(retrieve_setting("MINIMUM_ESS_SOC") or 0))

        self.tesla = TeslaApi()

        # Charge-control state.
        self._last_command_ts = 0.0        # for the command cooldown
        self._last_commanded_amps = None   # last amps we SET (not the meter) — see audit M1
        self._low_surplus_since = None     # start of the current surplus-loss grace window
        self._intent_was_on = False        # to detect the grid-assist / charge-request transition
        self._intent_off_edge = False      # set the tick intent switches OFF -> stop the charge now
        self._charge_mode = None           # 'grid' (express override) | 'surplus' | None
        self._stop_backoff_until = 0.0     # don't retry a rejected stop command every tick
        self._last_stop_alert_ts = 0.0     # rate-limit the "could not stop" Pushover alert
        self._stop_attempt_count = 0       # consecutive failed stop attempts (bounded retry)
        self._stop_escalated = False       # STOP_MAX_RETRIES exhausted -> paused, human alerted
        self._manual_stop_active = False   # dashboard Stop stays latched until confirmed/escalated
        self._fresh_stop_request = False   # reset retry state only on the first request tick
        self._last_discovery_wake_ts = 0.0 # rate-limit surplus-driven discovery wakes
        self._discovery_backoff_until = 0.0  # longer backoff after finding the car away
        self._last_status_state = None       # last logged state, so we only log on change
        self._grid_current_pending = None    # bounded manual/grid current acknowledgement
        self._grid_current_signature = None  # confirmed target for this intent session
        self._grid_current_confirmed_at = 0.0
        self._grid_current_warning_logged = False
        self._grid_delivery_warning_logged = False
        self._grid_current_state_cache = None

        # Smart-charge execution state. Live-session ownership is deliberately process-local:
        # after a restart, an already-running charge is treated as external unless durable state
        # identifies the branch's invalid application-owned fallback as its source.
        self._smart_plan = None
        self._smart_job = None
        self._smart_job_loaded = False
        self._smart_owns_charge = False
        self._smart_schedule_signature = None
        self._smart_removed_signature = None
        self._smart_removed_schedule_ids = set()
        self._smart_removed_existing_owned_schedule = False
        self._smart_limit_signature = None
        self._smart_limit_pending = None
        self._smart_current_pending = None
        self._smart_start_pending = None
        self._smart_limit_retry_after = 0.0
        self._smart_schedule_retry_after = 0.0
        self._smart_schedule_failure_signature = None
        self._smart_schedule_failure_attempts = 0
        self._smart_remove_failure_key = None
        self._smart_remove_failure_attempts = 0
        self._smart_schedule_supported = True
        self._smart_cleanup_requires_stop = False
        self._smart_state_cache = {}
        self._smart_reminder_keys = None
        self._smart_suppressed_blocks = None

        logging.info("EvCharger (__init__): Init complete.")

    def __del__(self):
        # Robust teardown: never let cleanup raise during GC (it runs at unpredictable
        # times, including interpreter shutdown). Call the Tesla topic housekeeping via its
        # public cleanup(), not __del__ directly.
        try:
            self.cleanup()
            tesla = getattr(self, "tesla", None)
            cleanup = getattr(tesla, "cleanup", None)
            if callable(cleanup):
                cleanup()
        except Exception:
            pass
        logging.info("EvCharger (__del__): Exiting...")

    def _local_engagement_signal(self) -> bool:
        """True only when a LOCAL (free, on-bus) signal says we might need to act on the
        car — so we never spend a Tesla Fleet API call while there is no reason to.

        Preconditions (any of):
          * explicit intent: the dedicated EV-charge request is on (NOT grid-assist); or
          * PV surplus: the sun is up, the house battery is already at/above its target
            SoC, and there is >= 2 A of exportable surplus (i.e. PV is spilling to grid); or
          * the car is already drawing power locally (measured charger amps) — we may need
            to adjust the rate or stop.
        This is the primary cost-avoidance layer; the tesla_budget guard is the backstop.

        Deliberately does NOT re-check the manual "Refresh Data" flag (REFRESH_REQUEST_KEY)
        here — main() reads and clears that flag exactly once per tick and ORs the captured
        value into its own dormancy check. A second independent read here would race that
        read-then-clear (a fresh request landing between the two reads would see this method
        return True on a since-cleared flag, while main()'s stale local copy still read False
        and skipped both the wake and the clear — delaying, not losing, the request by one
        tick, but needlessly). Single source of truth avoids that.
        """
        if self._intent_on():                 # dedicated EV-charge intent (decoupled from grid-assist)
            return True
        # Shadow mode is intentionally absent here: an unapplied plan must make zero smart
        # vehicle calls. An applied active job engages the loop so it can reconcile the one
        # fallback schedule and act exactly at block boundaries.
        if self._smart_apply() and self._smart_plan_context().get("handled"):
            return True
        if (self.is_the_sun_shining()
                and _num(self.ess_soc) >= self.minimum_ess_soc
                and _num(self.surplus_amps) >= SURPLUS_MIN_AMPS):
            return True
        # Stay engaged whenever we believe the car is charging (local amps OR cached Tesla
        # state) so we can still manage/stop it — e.g. when the EV-charge request is switched off.
        if self._charging_now():
            return True
        return False

    def main(self):
        """Main control tick. Rescheduled by a self-restarting Timer.

        Cost discipline: if no local signal wants a charge we stay fully dormant and never
        touch the Tesla API. When engaged, we refresh cached status with ONE throttled,
        no-wake read, then decide and act on fresh-ish status plus free local meters.
        """
        try:
            self._refresh_smart_plan()
            intent = self._intent_on()
            intent_on_edge = intent and not self._intent_was_on       # user just switched charging ON
            manual_stop_requested = self._manual_stop_requested()
            manual_stop_edge = (
                manual_stop_requested
                and not getattr(self, "_manual_stop_active", False)
            )
            self._intent_off_edge = (
                self._intent_was_on and not intent) or manual_stop_requested
            self._fresh_stop_request = (
                self._intent_was_on and not intent) or manual_stop_edge
            self._manual_stop_active = manual_stop_requested
            self._intent_was_on = intent

            # Manual "Refresh Data" button — one-shot, so consume (clear) it now regardless of
            # what happens later this tick; a stuck True would otherwise force a wake every tick.
            refresh_requested = self._refresh_requested()
            if refresh_requested:
                self.global_state.set(REFRESH_REQUEST_KEY, False)

            # Engage if something wants a charge OR the user just switched intent off (so we can
            # stop the car) OR a refresh was requested. Otherwise stay dormant and make zero
            # Tesla API calls.
            if not (self._local_engagement_signal() or self._intent_off_edge or refresh_requested):
                self._log_status("dormant", self._dormant_reason())
                self._reschedule(30.0)
                return

            self.dynamic_load_reservation_adjustment()

            # Decide whether this tick may WAKE the car (the expensive call). Justified by:
            #  * a fresh intent toggle (on OR off) -> check/act now; or
            #  * a manual refresh request -> check now; or
            #  * ongoing intent / enough PV surplus -> a rate-limited discovery wake.
            # All wakes remain hard-capped by the tesla_budget guard.
            surplus = self._surplus_available()
            force = wake = False
            if intent_on_edge or self._fresh_stop_request or refresh_requested:
                force = wake = True
                if intent_on_edge:
                    logging.info("EvCharger: EV-charge request toggled on — checking vehicle now.")
                elif refresh_requested:
                    # Same telemetry-mode distinction as the discovery-wake log below (L1): in
                    # telemetry mode update_vehicle_status() never actually wakes the car.
                    what = "refreshing telemetry state" if self._telemetry_on() else "waking vehicle to refresh status"
                    logging.info(f"EvCharger: manual refresh requested — {what}.")
            elif (intent or surplus) and self._should_discovery_wake():
                force = wake = True
                self._last_discovery_wake_ts = time.time()
                why = "charge intent on" if intent else f"PV surplus {int(_num(self.surplus_amps))}A"
                # In telemetry mode update_vehicle_status() short-circuits to the pushed state
                # and never actually wakes the car (audit L1) — say so instead of claiming a
                # wake that doesn't happen.
                what = "refreshing telemetry state" if self._telemetry_on() else "waking vehicle to check home/plugged state"
                logging.info(f"EvCharger: {why} — {what}.")

            self.tesla.update_vehicle_status(force=force, allow_wake=wake)

            # After a discovery wake that found the car not here to charge, back off further
            # wakes (not on the intent-off path — that just needs to stop the car).
            if wake and not self._intent_off_edge and not (self.tesla.is_home and self.tesla.is_plugged):
                backoff = DISCOVERY_AWAY_BACKOFF_S if not self.tesla.is_home else DISCOVERY_HOME_UNPLUGGED_BACKOFF_S
                self._discovery_backoff_until = time.time() + backoff
                logging.info(f"EvCharger: vehicle not chargeable (home={self.tesla.is_home}, "
                             f"plugged={self.tesla.is_plugged}); next wake-check in ~{int(backoff / 60)}m.")

            active = self._control_charging()
            self._log_status("charging" if active else "engaged", self.vehicle_status_msg())
            self._reschedule(20.0 if active else 30.0)

        except Exception as E:
            logging.info(f"EvCharger: main loop error: {E}")
            self._reschedule(30.0)

    def _dormant_reason(self) -> str:
        sa = int(_num(self.surplus_amps))
        return f"no charge intent; PV surplus {sa}A < {SURPLUS_MIN_AMPS}A threshold ({_num(self.surplus_watts):.0f}W)"

    def _log_status(self, state: str, detail: str = "") -> None:
        """Emit an INFO status line ONLY when the state changes (dormant <-> engaged <->
        charging). While nothing needs to happen — e.g. no/negative surplus — it logs once on
        entering that state and then stays silent. Per-tick detail lives at debug."""
        if state != self._last_status_state:
            logging.info(f"EvCharger [{state}]: {detail}")
            self._last_status_state = state
        else:
            logging.debug(f"EvCharger [{state}]: {detail}")

    def _reschedule(self, seconds: float):
        self.main_thread = threading.Timer(seconds, self.main)
        self.main_thread.daemon = True
        self.main_thread.start()

    # --- charge decision helpers (all read-only / free) --------------------
    def _telemetry_on(self) -> bool:
        return is_truthy(retrieve_setting("TESLA_TELEMETRY_ENABLED"), False)

    def _smart_enabled(self) -> bool:
        return is_truthy(retrieve_setting("EV_SMART_CHARGE_ENABLED"), False)

    def _smart_apply(self) -> bool:
        return self._smart_enabled() and is_truthy(
            retrieve_setting("EV_SMART_CHARGE_APPLY"), False)

    def _refresh_smart_plan(self) -> None:
        """Load one atomically-published planner snapshot without touching the vehicle."""
        if not self._smart_enabled():
            self._smart_plan = None
            self._smart_job = None
            self._smart_job_loaded = False
            return
        plan_path = retrieve_setting("EV_SMART_CHARGE_PLAN_PATH") or None
        job_path = retrieve_setting("EV_SMART_CHARGE_JOB_PATH") or None
        self._smart_plan = (load_plan_snapshot(path=plan_path)
                            if plan_path else load_plan_snapshot())
        self._smart_job = load_job(path=job_path) if job_path else load_job()
        self._smart_job_loaded = True

    @staticmethod
    def _parse_plan_time(value):
        try:
            parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None

    def _smart_plan_context(self, now=None) -> dict:
        """Validate the small execution-facing subset of the planner snapshot.

        A job remains *handled* even when paused, completed, infeasible or stale. This prevents
        legacy surplus automation from unexpectedly taking over while a smart job exists.
        """
        plan = getattr(self, "_smart_plan", None)
        if not self._smart_enabled() or not isinstance(plan, dict):
            return {"handled": False, "reason": "disabled_or_no_plan"}
        job = plan.get("job")
        if not isinstance(job, dict) or not str(job.get("id") or "").strip():
            return {"handled": False, "reason": "no_active_job"}
        now_dt = now or datetime.datetime.now(datetime.timezone.utc)
        generated = self._parse_plan_time(plan.get("generated_at"))
        age = None if generated is None else now_dt.timestamp() - generated.timestamp()
        stale = generated is None or age > SMART_PLAN_MAX_AGE_S or age < -120
        job_status = str(job.get("status") or "active").lower()
        plan_status = str(plan.get("status") or "").lower()
        reconcile_status = None
        reconcile_reason = ""
        durable = getattr(self, "_smart_job", None)
        if getattr(self, "_smart_job_loaded", False):
            if not isinstance(durable, dict):
                reconcile_status = "cancelled"
                reconcile_reason = "durable_job_removed"
            elif str(durable.get("id") or "") != str(job.get("id") or ""):
                reconcile_status = "waiting"
                reconcile_reason = "durable_job_replaced"
            elif str(durable.get("status") or "active").lower() != job_status:
                reconcile_status = (
                    "paused" if str(durable.get("status") or "").lower() == "paused"
                    else "waiting"
                )
                reconcile_reason = "durable_job_status_changed"
            else:
                durable_target = _num(durable.get("target_soc"), -1)
                plan_target = _num(plan.get("target_soc", job.get("target_soc")), -2)
                durable_deadline = self._parse_plan_time(durable.get("ready_by"))
                plan_deadline = self._parse_plan_time(
                    plan.get("ready_by") or job.get("ready_by"))
                if (abs(durable_target - plan_target) > 0.001
                        or durable_deadline is None or plan_deadline is None
                        or abs(durable_deadline.timestamp() - plan_deadline.timestamp()) > 1):
                    reconcile_status = "waiting"
                    reconcile_reason = "durable_job_edited"
        active_slot = None
        for slot in plan.get("slots") or ():
            if not isinstance(slot, dict):
                continue
            start = self._parse_plan_time(slot.get("start"))
            end = self._parse_plan_time(slot.get("end"))
            if start and end and start.timestamp() <= now_dt.timestamp() < end.timestamp():
                active_slot = slot
                break
        actionable = reconcile_status is None and not stale and job_status == "active" and plan_status in {
            "planned", "infeasible", "charging", "at_risk"
        }
        return {
            "handled": True,
            "plan": plan,
            "job": job,
            "job_id": str(job.get("id")),
            "stale": stale,
            "paused": job_status == "paused" or plan_status == "paused",
            "terminal": plan_status in {"completed", "cancelled", "idle"},
            "reconcile_status": reconcile_status,
            "reconcile_reason": reconcile_reason,
            "actionable": actionable,
            "slot": active_slot if actionable else None,
            "now": now_dt,
        }

    def _set_smart_state(self, status: str, *, reason="", target_amps=0,
                         job_id="", fallback=None) -> None:
        """Publish controller state only on change, avoiding retained MQTT/SQLite churn."""
        values = {
            f"{SMART_STATE_PREFIX}controller_status": status,
            f"{SMART_STATE_PREFIX}controller_reason": reason,
            f"{SMART_STATE_PREFIX}job_id": job_id,
            f"{SMART_STATE_PREFIX}target_amps": target_amps,
            f"{SMART_STATE_PREFIX}owned_by_controller": bool(
                getattr(self, "_smart_owns_charge", False)),
        }
        if fallback is not None:
            values[f"{SMART_STATE_PREFIX}fallback_status"] = fallback
        cache = getattr(self, "_smart_state_cache", None)
        if cache is None:
            cache = self._smart_state_cache = {}
        for key, value in values.items():
            if cache.get(key) != value:
                self.global_state.set(key, value)
                cache[key] = value

    def _pushed_vehicle_is_unplugged(self) -> bool:
        """Use only the Fleet Telemetry bridge's pushed state; never query/wake the car."""
        key = "tesla_is_plugged"
        has = getattr(self.global_state, "has", None)
        if callable(has):
            try:
                if not has(key):
                    return False
            except Exception:
                return False
        elif isinstance(self.global_state, dict) and key not in self.global_state:
            return False
        value = self.global_state.get(key)
        if isinstance(value, str):
            return value.strip().lower() in {"false", "0", "off", "no"}
        return value is False or value == 0

    def _controller_state_path(self) -> Path:
        configured = (retrieve_setting("EV_SMART_CHARGE_CONTROLLER_STATE_PATH")
                      or retrieve_setting("EV_SMART_CHARGE_REMINDER_STATE_PATH"))
        return Path(str(configured)) if configured else SMART_CONTROLLER_STATE_PATH

    @staticmethod
    def _read_reminder_state(path: Path) -> dict:
        try:
            with path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
            sent = payload.get("sent") if isinstance(payload, dict) else None
            suppressed = payload.get("suppressed_blocks") if isinstance(payload, dict) else None
            return {
                "schema_version": 1,
                "sent": sent if isinstance(sent, dict) else {},
                "suppressed_blocks": suppressed if isinstance(suppressed, dict) else {},
            }
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"schema_version": 1, "sent": {}, "suppressed_blocks": {}}

    @staticmethod
    def _atomic_write_reminder_state(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent,
                    prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
                temporary_name = stream.name
                json.dump(payload, stream, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def _claim_plug_reminder(self, reminder_key: str, now: datetime.datetime) -> bool:
        """Atomically claim one reminder key before notification, making restarts idempotent."""
        path = self._controller_state_path()
        with _SMART_CONTROLLER_STATE_LOCK:
            cached = getattr(self, "_smart_reminder_keys", None)
            if cached is not None and reminder_key in cached:
                return False
            state = self._read_reminder_state(path)
            sent = state["sent"]
            self._smart_reminder_keys = set(sent)
            if reminder_key in sent:
                return False
            sent[reminder_key] = now.isoformat()
            # Keep the tiny ledger bounded while retaining the most recently inserted entries.
            if len(sent) > 100:
                state["sent"] = dict(list(sent.items())[-100:])
            try:
                self._atomic_write_reminder_state(path, state)
            except OSError as exc:
                # Fail closed: without durable dedupe, sending could spam after each restart.
                logging.warning(f"EvCharger: could not persist plug-reminder claim: {exc}")
                return False
            self._smart_reminder_keys.add(reminder_key)
        return True

    def _smart_block_key(self, smart: dict) -> str:
        slot = smart.get("slot") or {}
        return f"{smart.get('job_id', '')}|{slot.get('start', '')}|{slot.get('end', '')}"

    def _suppress_smart_block(self, smart: dict, now: datetime.datetime) -> None:
        """Persist that an owned charge stopped, so restarts cannot fight that stop."""
        key = self._smart_block_key(smart)
        path = self._controller_state_path()
        with _SMART_CONTROLLER_STATE_LOCK:
            state = self._read_reminder_state(path)
            suppressed = state["suppressed_blocks"]
            suppressed[key] = now.isoformat()
            if len(suppressed) > 100:
                state["suppressed_blocks"] = dict(list(suppressed.items())[-100:])
            try:
                self._atomic_write_reminder_state(path, state)
                self._smart_suppressed_blocks = set(state["suppressed_blocks"])
            except OSError as exc:
                # Process-local relinquishment still prevents immediate fighting. Persistence
                # failure only weakens restart protection and must never interrupt control.
                logging.warning(f"EvCharger: could not persist smart-block suppression: {exc}")

    def _smart_block_is_suppressed(self, smart: dict) -> bool:
        key = self._smart_block_key(smart)
        try:
            suppressed = getattr(self, "_smart_suppressed_blocks", None)
            if suppressed is None:
                with _SMART_CONTROLLER_STATE_LOCK:
                    state = self._read_reminder_state(self._controller_state_path())
                suppressed = self._smart_suppressed_blocks = set(state["suppressed_blocks"])
            return key in suppressed
        except Exception as exc:
            logging.info(f"EvCharger: could not read smart-block suppression: {exc}")
            return False

    def _maybe_send_plug_reminder(self, smart: dict) -> None:
        """Send at most one non-critical plug reminder for one job/deadline.

        This routine consumes only the already-loaded plan and pushed plug state. Notification
        and filesystem failures are swallowed so reminder convenience can never block control.
        """
        if not is_truthy(retrieve_setting("EV_PLUG_REMINDER_ENABLED"), False):
            return
        if not smart.get("actionable") or not self._pushed_vehicle_is_unplugged():
            return
        plan = smart["plan"]
        job = smart["job"]
        now = smart["now"]
        deadline = self._parse_plan_time(plan.get("ready_by") or job.get("ready_by"))
        plug_in_by = self._parse_plan_time(plan.get("plug_in_by"))
        future_starts = []
        for slot in plan.get("slots") or ():
            if isinstance(slot, dict):
                start = self._parse_plan_time(slot.get("start"))
                if start and start.timestamp() >= now.timestamp():
                    future_starts.append(start)
        first_slot = min(future_starts, key=lambda value: value.timestamp()) if future_starts else None
        candidates = [value for value in (plug_in_by, first_slot) if value is not None]
        if deadline is None or deadline.timestamp() <= now.timestamp() or not candidates:
            return
        reminder_at = min(candidates, key=lambda value: value.timestamp())
        lead_minutes = max(0.0, _num(retrieve_setting("EV_PLUG_REMINDER_LEAD_MINUTES"), 45.0))
        if reminder_at.timestamp() > now.timestamp() + lead_minutes * 60.0:
            return
        reminder_key = f"{smart['job_id']}|{deadline.isoformat()}"
        if not self._claim_plug_reminder(reminder_key, now):
            return
        target_soc = int(round(_num(plan.get("target_soc", job.get("target_soc")), 0)))
        first_text = first_slot.astimezone(self.tz).strftime("%H:%M") if first_slot else "soon"
        deadline_text = deadline.astimezone(self.tz).strftime("%a %H:%M")
        title = "Plug in for smart EV charging"
        message = (f"Plug in the car for its planned charge starting {first_text}, targeting "
                   f"{target_soc}% by {deadline_text}.")

        def send_reminder():
            try:
                pushover_notification(title, message)
            except Exception as exc:
                logging.info(f"EvCharger: plug reminder could not be sent: {exc}")

        # The existing requests-backed Pushover helper has no bounded timeout. This convenience
        # notification therefore runs on a daemon thread and can never hold up power control.
        try:
            threading.Thread(
                target=send_reminder,
                name=f"ev-plug-reminder-{smart['job_id']}",
                daemon=True,
            ).start()
        except Exception as exc:
            logging.info(f"EvCharger: plug reminder worker could not start: {exc}")

    def _intent_on(self) -> bool:
        """Explicit intent to charge the car, read from the DEDICATED EV-charge flag
        (EV_CHARGE_INTENT_KEY). Fully decoupled from the ESS grid-assist toggle
        ('grid_charging_enabled'): toggling grid-assist must never start or stop the car.
        We also do NOT read 'tesla_charge_requested' (our own start/stop set it, which latched
        intent permanently on). Until a dedicated EV-charge button sets this key it stays off,
        so only PV-surplus charging can engage the car."""
        return is_truthy(self.global_state.get(EV_CHARGE_INTENT_KEY), False)

    def _refresh_requested(self) -> bool:
        """Manual 'Refresh Data' button (Vehicle tab). One-shot: main() clears this back to
        False after consuming it, so it forces exactly one wake+refresh per press rather than
        re-triggering on every subsequent tick."""
        return is_truthy(self.global_state.get(REFRESH_REQUEST_KEY), False)

    def _manual_stop_requested(self) -> bool:
        """Return the dashboard's one-shot imperative stop request."""
        return is_truthy(self.global_state.get(MANUAL_STOP_REQUEST_KEY), False)

    def _surplus_available(self) -> bool:
        """Exportable PV surplus exists (sun up, house battery at/above target, >= min amps)."""
        return (self.is_the_sun_shining()
                and _num(self.ess_soc) >= self.minimum_ess_soc
                and _num(self.surplus_amps) >= SURPLUS_MIN_AMPS)

    def _should_discovery_wake(self) -> bool:
        """Rate-limit surplus/intent-driven wakes so an away or asleep car can't drain the
        wake budget: at most one per DISCOVERY_WAKE_INTERVAL_S, and a longer backoff after a
        wake finds the car away/unplugged. The tesla_budget guard is the hard cap on top."""
        now = time.time()
        if now < self._discovery_backoff_until:
            return False
        return (now - self._last_discovery_wake_ts) >= DISCOVERY_WAKE_INTERVAL_S

    def _charging_now(self) -> bool:
        """Is the car actually drawing? Prefer the local charger meter (free, near real-time)
        over the cached Tesla flag, which only refreshes on a (throttled) API read."""
        return _num(self.charging_amps) >= 1 or bool(self.tesla.is_charging)

    def _cooldown_ok(self) -> bool:
        return (time.time() - self._last_command_ts) >= COMMAND_COOLDOWN_S

    def _mark_command(self):
        self._last_command_ts = time.time()

    def _reset_grid_current_tracking(self) -> None:
        """Forget one manual/grid intent session without touching the vehicle."""
        self._grid_current_pending = None
        self._grid_current_signature = None
        self._grid_current_confirmed_at = 0.0
        self._grid_current_warning_logged = False
        self._grid_delivery_warning_logged = False
        self._grid_current_state_cache = None

    def _grid_target_amps(self) -> int:
        """Return the safe full-rate request bounded by kW, wiring and vehicle limits."""
        target_kw = max(0.0, _num(retrieve_setting("EV_CHARGER_MAX_KW"), 16.0))
        return self._smart_target_amps({"requested_power_kw": target_kw})

    def _set_grid_current_state(self, status: str, target: int) -> None:
        """Publish the small manual-current acknowledgement state only on change."""
        snapshot = (status, target)
        if getattr(self, "_grid_current_state_cache", None) == snapshot:
            return
        self.global_state.set("ev_grid_charge_current_status", status)
        self.global_state.set("ev_grid_charge_target_amps", target)
        self._grid_current_state_cache = snapshot

    def _grid_current_ack(self, target: int, now_ts: float) -> tuple[str, bool]:
        """Confirm one grid-current request from pushed Tesla state, with one retry."""
        observed, observed_at = self._smart_observation(
            "tesla_charge_current_request",
            "tesla_charge_current_request_updated_at",
            "charging_amp_limit",
            "charge_current_request_update_ts",
        )
        pending = getattr(self, "_grid_current_pending", None)
        if isinstance(pending, dict) and pending.get("target") != target:
            pending = self._grid_current_pending = None
        # A retained value from before sleep/restart cannot replace the required
        # initial command for this manual session. Only an observation received
        # after our command was sent may acknowledge it.
        telemetry_confirms = bool(
            isinstance(pending, dict)
            and math.isfinite(observed)
            and int(round(observed)) == target
            and observed_at > 0
            and observed_at >= _num(pending.get("sent_at"), 0.0)
        )
        if telemetry_confirms:
            self._grid_current_pending = None
            self._grid_current_signature = target
            if not getattr(self, "_grid_current_confirmed_at", 0.0):
                self._grid_current_confirmed_at = (
                    observed_at if observed_at > 0 else now_ts)
            self._last_commanded_amps = target
            return "confirmed", False
        if getattr(self, "_grid_current_signature", None) == target:
            # Once this session was confirmed, a later requested-current change
            # belongs to the user/vehicle. Do not turn the manual override into a
            # continuous fight; Maxem delivery is independently authoritative.
            return "confirmed", False
        if isinstance(pending, dict):
            attempts = int(pending.get("attempts", 0))
            sent_at = _num(pending.get("sent_at"), 0.0)
            due = (
                now_ts - sent_at >= SMART_COMMAND_ACK_TIMEOUT_S
                or observed_at >= sent_at > 0
            )
            if attempts >= 2:
                return ("unconfirmed" if due else "pending"), False
            return ("retry_due" if due else "pending"), due
        return "required", True

    def _record_grid_current_command(self, target: int, now_ts: float,
                                     *, accepted: bool) -> None:
        pending = getattr(self, "_grid_current_pending", None)
        attempts = (
            int(pending.get("attempts", 0))
            if isinstance(pending, dict) and pending.get("target") == target else 0
        )
        self._grid_current_pending = {
            "target": target,
            "sent_at": now_ts,
            "attempts": attempts + 1,
            "accepted": accepted,
        }

    def _observe_grid_delivery(self, target: int, now_ts: float) -> str:
        """Classify ABB delivery without using it to resend or fight Maxem."""
        if target <= 5 or not self._charging_now():
            return "confirmed"
        if _num(self.charging_amps) > 5.0:
            self._grid_delivery_warning_logged = False
            return "delivery_confirmed"
        confirmed_at = _num(
            getattr(self, "_grid_current_confirmed_at", 0.0), now_ts)
        if not confirmed_at or now_ts - confirmed_at < SMART_COMMAND_ACK_TIMEOUT_S:
            return "confirmed_waiting_for_delivery"
        if not getattr(self, "_grid_delivery_warning_logged", False):
            logging.warning(
                "EvCharger: grid charge requested %d A and Tesla confirmed the request, "
                "but local delivery remains %.1f A; treating this as Maxem/site limiting "
                "and not resending the Fleet command.",
                target, _num(self.charging_amps),
            )
            self._grid_delivery_warning_logged = True
        return "delivery_limited"

    def _control_manual_grid_charge(self, now: datetime.datetime) -> bool:
        """Set full grid current once, verify via telemetry, retry once, then start."""
        target = self._grid_target_amps()
        now_ts = now.timestamp()
        if target <= 0:
            self._set_grid_current_state("invalid_target", target)
            return False
        status, should_command = self._grid_current_ack(target, now_ts)
        accepted_now = False
        if should_command and self._cooldown_ok():
            accepted_now = bool(self.tesla.set_tesla_charge_amps(
                target, installation_ceiling=self._smart_installation_ceiling()))
            self._record_grid_current_command(
                target, now_ts, accepted=accepted_now)
            self._mark_command()
            status = "confirmation_pending" if accepted_now else "command_rejected"
            logging.info(
                "EvCharger: manual grid charge requested at %d A; awaiting Fleet "
                "Telemetry confirmation%s.",
                target, "" if accepted_now else " after rejected command",
            )
        elif should_command:
            status = "command_cooldown"

        may_start = accepted_now or status in {"confirmed", "delivery_confirmed"}
        if not self._charging_now() and may_start:
            logging.info(
                "EvCharger: EV-charge request on — starting at the verified %d A ceiling.",
                target,
            )
            self.tesla.start_tesla_charge()
            self._mark_command()

        if status == "confirmed":
            status = self._observe_grid_delivery(target, now_ts)
        elif status == "pending":
            status = "confirmation_pending"
        if status == "unconfirmed" and not getattr(
                self, "_grid_current_warning_logged", False):
            logging.warning(
                "EvCharger: Tesla did not confirm the %d A manual grid request after "
                "two Fleet commands; no further automatic current commands will be sent.",
                target,
            )
            self._grid_current_warning_logged = True
        self._set_grid_current_state(status, target)
        return True

    def _control_charging(self, now=None) -> bool:
        """Decide and act on the car's charge. Returns True while actively charging.

        Two mutually-exclusive modes, never intermixed:
          * EV-CHARGE REQUEST (intent on) is an express override: charge at the car's own
            rate and IGNORE all PV-surplus logic. Stops only when the request is switched off
            or the car is full. Driven by the dedicated EV-charge flag, NOT grid-assist.
          * SURPLUS (intent off) charges only from genuine exportable PV surplus, matches the
            current to it, and stops when the surplus is gone (after a short cloud grace).
        Starts and current changes require home + plugged + non-supercharging state. An explicit
        safety Stop may also use authoritative local draw when those pushed fields are stale.
        """
        t = self.tesla
        smart = self._smart_plan_context(now=now) if self._smart_apply() else {
            "handled": False,
        }
        if smart.get("handled"):
            self._maybe_send_plug_reminder(smart)

        # An explicit dashboard Stop is imperative. Do this before home/plug/supercharging
        # eligibility: those pushed fields may be stale or unknown precisely when the local ABB
        # meter proves the car is still drawing. The robust stop path performs its own bounded
        # wake/retry and confirms against local power flow.
        if self._intent_off_edge:
            if getattr(self, "_fresh_stop_request", False):
                self._stop_attempt_count = 0
                self._stop_escalated = False
            if (smart.get("handled")
                    and getattr(self, "_smart_owns_charge", False)):
                suppression_context = (
                    self._smart_surplus_context(smart)
                    if self._charge_mode == "smart_surplus" else smart
                )
                self._suppress_smart_block(suppression_context, smart["now"])
            self._reset_grid_current_tracking()
            stopped = self._stop_charge("manual EV charge stop requested", force=True)
            if stopped or self._stop_escalated:
                self.global_state.set(MANUAL_STOP_REQUEST_KEY, False)
                self._manual_stop_active = False
            return False

        commandable = bool(t.is_home and t.is_plugged and not t.is_supercharging)
        if not commandable:
            if smart.get("handled"):
                status = ("away" if not t.is_home else
                          "unplugged" if not t.is_plugged else "supercharging")
                self._set_smart_state(status, reason="vehicle_not_commandable",
                                      job_id=smart.get("job_id", ""))
            self._low_surplus_since = None
            self._charge_mode = None
            if _num(self.charging_amps) < 1:
                self.update_charging_amp_totals(0)
            return False

        # 1) Car reached its SoC limit -> stop.
        if t.is_full:
            if self._charging_now():
                self._stop_charge("car at its SoC limit", force=True)
            self._low_surplus_since = None
            self._charge_mode = None
            return False

        # 2) EV-CHARGE REQUEST ON = express override: charge at the car's own rate; ignore
        #    PV surplus entirely. Driven by the dedicated flag, NOT grid-assist.
        if self._intent_on():
            self._low_surplus_since = None
            if self._charge_mode != 'grid':
                self._reset_grid_current_tracking()
            self._charge_mode = 'grid'
            control_now = now or smart.get("now") or datetime.datetime.now(
                datetime.timezone.utc)
            return self._control_manual_grid_charge(control_now)

        # Turning apply off is a hard no-side-effect gate. Relinquish process ownership but do
        # not send a smart stop (nor let the legacy surplus-loss path misclassify and stop this
        # session). Tesla/Maxem or the user remains in control until the observed charge ends.
        if not self._smart_apply() and self._charge_mode in {
                "smart", "smart_solar", "smart_surplus", "smart_released"}:
            self._smart_owns_charge = False
            if self._charging_now():
                self._charge_mode = "smart_released"
                return True
            self._charge_mode = None
            self._last_commanded_amps = None

        # 3) Applied smart job. This is intentionally below the explicit charge-now path, but
        # above surplus control. A plan in paused/stale/terminal state remains handled so the
        # legacy surplus loop cannot unexpectedly take over while the job still exists.
        if smart.get("handled"):
            # Between scheduled price blocks, a valid active job may make extra
            # progress from real exportable PV. The same live guard used by the
            # legacy path protects the stationary battery first. External/user
            # sessions still flow through _control_smart_charging(), which never
            # changes their current or stops them.
            smart_slot = smart.get("slot")
            solar_only_slot = bool(
                smart.get("actionable")
                and isinstance(smart_slot, dict)
                and str(smart_slot.get("supply") or "").lower() == "solar"
                and _num(smart_slot.get("grid_energy_kwh"), 0.0) <= 0.001
            )
            if solar_only_slot:
                return self._control_smart_solar_slot(smart)
            if smart.get("actionable") and smart_slot is None:
                if self._surplus_available():
                    return self._control_smart_surplus(smart)
                if (self._charge_mode == "smart_surplus"
                        and getattr(self, "_smart_owns_charge", False)):
                    active = self._maybe_stop_on_surplus_loss()
                    if self._charge_mode is None:
                        self._smart_owns_charge = False
                    self._set_smart_state(
                        "charging" if active else "waiting",
                        reason=("solar_surplus_grace" if active
                                else "solar_surplus_ended"),
                        job_id=smart.get("job_id", ""),
                    )
                    return active
            elif self._charge_mode in {"smart_solar", "smart_surplus"}:
                # A planned block has begun while an opportunistic session is
                # already drawing; retain ownership and transition seamlessly to
                # the plan's requested ceiling.
                self._charge_mode = "smart"
            return self._control_smart_charging(smart)

        # 4) SURPLUS mode (grid-assist off): charge only from real exportable PV surplus.
        if self._surplus_available():
            self._low_surplus_since = None
            if not self._charging_now():
                return self._start_surplus_charge()
            return self._adjust_surplus_amps()

        # 5) Grid-assist off and no surplus. If we were surplus-charging, ride out clouds;
        #    otherwise (e.g. a grid charge we haven't managed to stop) just stop.
        if self._charging_now():
            if self._charge_mode == 'grid':
                self._stop_charge("grid-assist off and no surplus", force=False)
                return False
            return self._maybe_stop_on_surplus_loss()
        self._low_surplus_since = None
        self._charge_mode = None
        return False

    @staticmethod
    def _smart_command_ok(result) -> bool:
        if isinstance(result, tuple):
            return bool(result and result[0])
        return result is True or result == "ok"

    @staticmethod
    def _smart_command_category(result) -> str:
        if isinstance(result, tuple) and len(result) > 1:
            return str(result[1] or "failed").lower()
        return "ok" if EvCharger._smart_command_ok(result) else "failed"

    def _smart_target_amps(self, slot: dict) -> int:
        """Convert the plan's kW ceiling to per-phase amps using pushed vehicle data.

        Actual ABB current is deliberately absent from this calculation. Maxem may temporarily
        reduce EVSE delivery for site overload protection; chasing that measured reduction with
        repeated Fleet commands would fight the protection system and waste commands.
        """
        explicit_amps = slot.get("target_amps")
        if explicit_amps is not None:
            configured_max = self._smart_installation_ceiling()
            live_max = _num(self.global_state.get("tesla_charge_current_max"), 0.0)
            live_max = live_max if 1.0 <= live_max <= 32.0 else configured_max
            ceiling = min(configured_max, live_max)
            return int(max(0, min(math.floor(_num(explicit_amps)), math.floor(ceiling))))
        target_kw = _num(slot.get("target_kw", slot.get("requested_power_kw")), 0.0)
        phases = _num(self.global_state.get("tesla_charger_phases"), 3.0)
        phases = phases if 1.0 <= phases <= 3.0 else 3.0
        voltage = _num(self.global_state.get("tesla_charger_voltage"), 230.0)
        voltage = voltage if 180.0 <= voltage <= 260.0 else 230.0
        configured_max = self._smart_installation_ceiling()
        live_max = _num(self.global_state.get("tesla_charge_current_max"), 0.0)
        live_max = live_max if 1.0 <= live_max <= 32.0 else 0.0
        ceiling = min(configured_max, live_max) if live_max > 0 else configured_max
        requested = math.floor(max(0.0, target_kw) * 1000.0 / (phases * voltage))
        if target_kw > 0.0:
            # A positive partial tail must not collapse to 0 A. Tesla accepts
            # 1–4 A on this vehicle through the intentional two-send workaround
            # owned by TeslaApi.set_tesla_charge_amps().
            requested = max(1, requested)
        return int(max(0, min(requested, math.floor(ceiling))))

    def _smart_surplus_context(self, smart: dict) -> dict:
        """Return the stable synthetic block representing between-block solar."""
        solar = dict(smart)
        plan = smart.get("plan") or {}
        job = smart.get("job") or {}
        start = plan.get("generated_at") or smart["now"].isoformat()
        end = plan.get("ready_by") or job.get("ready_by")
        if not end:
            return smart
        solar["slot"] = {
            "start": start,
            "end": end,
            "target_amps": int(max(0.0, _num(self.surplus_amps))),
            "supply": "solar",
            "opportunistic": True,
        }
        return solar

    def _control_smart_surplus(self, smart: dict) -> bool:
        """Advance an active job from protected live PV between price blocks.

        A stable synthetic block lets the normal smart command/acknowledgement,
        manual-ownership, target-limit, and Tesla fallback safeguards remain the
        sole command path. Maxem still controls delivered current independently.
        """
        solar = self._smart_surplus_context(smart)
        if solar is smart:
            return self._control_smart_charging(smart)
        active = self._control_smart_charging(solar)
        if getattr(self, "_smart_owns_charge", False):
            self._charge_mode = "smart_surplus"
            self._low_surplus_since = None
            self._set_smart_state(
                "charging" if self._charging_now() else "starting",
                reason="opportunistic_solar_surplus",
                target_amps=self._smart_target_amps(solar["slot"]),
                job_id=smart.get("job_id", ""),
            )
        return active

    def _control_smart_solar_slot(self, smart: dict) -> bool:
        """Run a forecast-solar slot only at surplus-backed live current.

        A missed/cloudy solar quarter is left for the next SoC-based replan and
        deadline fallback; it must not silently become a full-power grid block.
        Mixed and grid-labelled slots continue through normal planned control.
        """
        if not self._surplus_available():
            if (self._charge_mode == "smart_solar"
                    and getattr(self, "_smart_owns_charge", False)):
                active = self._maybe_stop_on_surplus_loss()
                if self._charge_mode is None:
                    self._smart_owns_charge = False
                self._set_smart_state(
                    "charging" if active else "waiting",
                    reason="solar_slot_grace" if active else "solar_slot_unavailable",
                    job_id=smart.get("job_id", ""),
                )
                return active
            waiting = dict(smart)
            # Keep the real block identity with a zero target. This prevents a
            # user-started-and-stopped session during the cloudy slot from later
            # being reversed if sunshine returns inside that same block.
            waiting_slot = dict(smart.get("slot") or {})
            waiting_slot["target_amps"] = 0
            waiting["slot"] = waiting_slot
            return self._control_smart_charging(waiting)

        solar = dict(smart)
        slot = dict(smart.get("slot") or {})
        planned_amps = self._smart_target_amps(slot)
        slot["target_amps"] = min(
            planned_amps, int(max(0.0, _num(self.surplus_amps))))
        solar["slot"] = slot
        active = self._control_smart_charging(solar)
        if getattr(self, "_smart_owns_charge", False):
            self._charge_mode = "smart_solar"
            self._low_surplus_since = None
            self._set_smart_state(
                "charging" if self._charging_now() else "starting",
                reason="forecast_solar_surplus",
                target_amps=self._smart_target_amps(slot),
                job_id=smart.get("job_id", ""),
            )
        return active

    @staticmethod
    def _smart_installation_ceiling() -> float:
        configured = max(0.0, _num(retrieve_setting("EV_CHARGER_MAX_AMPS"), 0.0))
        return configured if configured > 0 else 24.0

    @staticmethod
    def _tesla_weekday_mask(value: datetime.datetime) -> int:
        """Tesla weekday bitmap: Sunday=1, Monday=2, ..., Saturday=64."""
        return 1 << ((value.weekday() + 1) % 7)

    def _smart_observation(self, state_key, timestamp_key, attribute,
                           timestamp_attribute):
        """Return pushed/REST-observed command state without inventing confirmation."""
        observed = self.global_state.get(state_key)
        if observed in (None, "", "None"):
            observed = getattr(self.tesla, attribute, None)
        observed_at = _num(self.global_state.get(timestamp_key), 0.0)
        if observed_at <= 0:
            observed_at = _num(getattr(self.tesla, timestamp_attribute, 0), 0.0)
        return _num(observed, math.nan), observed_at

    def _smart_current_ack(self, target: int, now_ts: float) -> tuple[str, bool]:
        """Classify the requested-current acknowledgement; never use delivered ABB amps."""
        observed, observed_at = self._smart_observation(
            "tesla_charge_current_request",
            "tesla_charge_current_request_updated_at",
            "charging_amp_limit",
            "charge_current_request_update_ts",
        )
        pending = getattr(self, "_smart_current_pending", None)
        if isinstance(pending, dict) and pending.get("target") != target:
            pending = self._smart_current_pending = None
        if (observed_at > 0 and math.isfinite(observed)
                and int(round(observed)) == target):
            self._smart_current_pending = None
            self._last_commanded_amps = target
            return "confirmed", False
        if isinstance(pending, dict):
            attempts = int(pending.get("attempts", 0))
            sent_at = _num(pending.get("sent_at"), 0.0)
            due = (now_ts - sent_at >= SMART_COMMAND_ACK_TIMEOUT_S
                   or observed_at >= sent_at > 0)
            if attempts >= SMART_COMMAND_MAX_ATTEMPTS:
                # A definitive rejected response needs no grace period. An accepted final
                # attempt still gets the full observation window before escalation.
                exhausted = not pending.get("accepted", False) or due
                return ("unconfirmed" if exhausted else "pending"), False
            return ("retry_due" if due else "pending"), due
        if self._last_commanded_amps == target:
            # A value changed after prior confirmation. Do not fight a user or Maxem; only
            # accepted-but-unconfirmed commands receive bounded retries.
            return "external_change", False
        return "required", True

    def _record_smart_current_command(self, target: int, now_ts: float,
                                      *, accepted: bool) -> None:
        pending = getattr(self, "_smart_current_pending", None)
        attempts = (int(pending.get("attempts", 0))
                    if isinstance(pending, dict) and pending.get("target") == target else 0)
        self._smart_current_pending = {
            "target": target,
            "sent_at": now_ts,
            "attempts": attempts + 1,
            "accepted": accepted,
        }

    def _smart_continuous_fallback_start(
            self, plan: dict, deadline: datetime.datetime,
            default_start: datetime.datetime) -> datetime.datetime:
        """Return the conservative continuous Tesla fallback start for a plan."""
        required_ac_kwh = _num(plan.get("required_ac_kwh"), 0.0)
        expected_delivery_kw = _num(plan.get("expected_delivery_kw"), 0.0)
        completion_buffer_minutes = max(
            0.0, _num(plan.get("completion_buffer_minutes"), 0.0))
        if not (math.isfinite(required_ac_kwh) and required_ac_kwh > 0
                and math.isfinite(expected_delivery_kw) and expected_delivery_kw > 0):
            return default_start
        fallback_seconds = (
            required_ac_kwh / expected_delivery_kw * 3600.0
            + completion_buffer_minutes * 60.0
        )
        # Tesla's schedule payload has times and a start weekday, but no explicit end date.
        # It can therefore represent at most one day. Normal 100 kWh-car fallback windows
        # are far shorter; cap pathological configurations to the safest representable day.
        fallback_seconds = min(24 * 60 * 60.0, max(15 * 60.0, fallback_seconds))
        fallback_start_ts = deadline.timestamp() - fallback_seconds
        if fallback_seconds < 24 * 60 * 60.0:
            # Start on or before the calculated instant, aligned to Tesla/planner quarters.
            fallback_start_ts = math.floor(fallback_start_ts / (15 * 60.0)) * 15 * 60.0
        return datetime.datetime.fromtimestamp(
            fallback_start_ts, tz=deadline.tzinfo)

    def _smart_fallback_is_beyond_tesla_week(self, smart: dict) -> bool:
        """Whether the exact one-time fallback cannot yet identify its calendar week."""
        plan = smart.get("plan") or {}
        start = self._parse_plan_time(plan.get("latest_safe_start"))
        deadline = self._parse_plan_time(
            plan.get("ready_by") or (smart.get("job") or {}).get("ready_by"))
        now = smart.get("now")
        if start is None or deadline is None or now is None:
            return False
        start = self._smart_continuous_fallback_start(plan, deadline, start)
        return start.timestamp() - now.timestamp() > 7 * 24 * 60 * 60

    def _reconcile_smart_fallback(self, smart: dict) -> str:
        """Reconcile the deterministic onboard fallback without schedule-list reads."""
        if not getattr(self, "_smart_schedule_supported", True):
            return "unsupported"
        now_ts = smart["now"].timestamp()
        plan = smart["plan"]
        job = smart["job"]
        start = self._parse_plan_time(plan.get("latest_safe_start"))
        deadline = self._parse_plan_time(plan.get("ready_by") or job.get("ready_by"))
        target_soc = _num(plan.get("target_soc", job.get("target_soc")), 0.0)
        try:
            latitude = float(retrieve_setting("HOME_ADDRESS_LAT"))
            longitude = float(retrieve_setting("HOME_ADDRESS_LONG"))
        except (TypeError, ValueError):
            return "missing_location"
        if start is None or deadline is None or not 50 <= target_soc <= 100:
            return "invalid_plan"

        # The planner's capacity-safe start can span sparse, provisional slots across many
        # days. A Tesla schedule is one continuous wall-clock interval, so derive its emergency
        # window from the remaining energy and the already-conservative delivery rate instead.
        # This fallback is only meant to protect the deadline if the service disappears; normal
        # low-cost blocks are still started/stopped by the live controller.
        start = self._smart_continuous_fallback_start(plan, deadline, start)

        # Tesla schedules are wall-clock commands: installing yesterday's/latest-safe minute
        # can defer until another weekday instead of starting now. If that time has passed,
        # preserve an active selected block at the next whole minute; otherwise use the first
        # future selected slot. With no selected future start, use that same near-future minute.
        now = smart["now"]
        if start.timestamp() <= now.timestamp():
            next_minute_ts = (math.floor(now.timestamp() / 60.0) + 1) * 60.0
            next_minute = datetime.datetime.fromtimestamp(next_minute_ts, tz=start.tzinfo)
            future_starts = []
            for slot in plan.get("slots") or ():
                if isinstance(slot, dict):
                    candidate = self._parse_plan_time(slot.get("start"))
                    if (candidate and candidate.timestamp() > now.timestamp()
                            and candidate.timestamp() < deadline.timestamp()):
                        future_starts.append(candidate)
            if smart.get("slot") is not None:
                start = next_minute
            elif future_starts:
                start = min(future_starts, key=lambda value: value.timestamp())
            else:
                start = next_minute
        if start.timestamp() >= deadline.timestamp():
            return "no_future_window"

        # Tesla interprets schedule minutes and weekdays in the vehicle's local timezone. Plan
        # timestamps may carry different offsets (the persisted UI deadline is normally UTC),
        # so never take ``.hour`` or ``.date`` directly from their source representation.
        local_start = start.astimezone(self.tz)
        local_deadline = deadline.astimezone(self.tz)
        local_now = now.astimezone(self.tz)
        if local_start.timestamp() - local_now.timestamp() > 7 * 24 * 60 * 60:
            # One-time Tesla schedules carry a weekday, not a calendar date. Sending a start
            # more than seven days away can select the nearer weekday or make an approximated
            # cross-midnight window active immediately. Remove our deterministic schedule and
            # wait until the exact occurrence is representable; never approximate safety time.
            previous_fallback = str(
                self.global_state.get(f"{SMART_STATE_PREFIX}fallback_status") or "")
            known_owned = (
                getattr(self, "_smart_schedule_signature", None) is not None
                or previous_fallback in {"confirmed", "limit_pending", "limit_unconfirmed"}
                or previous_fallback.startswith(
                    "fallback_waiting_for_representable_date_remove_")
                or getattr(self, "_smart_removed_existing_owned_schedule", False)
            )
            removal = self._remove_smart_fallback(smart, force=True)
            known_owned = known_owned or getattr(
                self, "_smart_removed_existing_owned_schedule", False)
            if known_owned:
                self._smart_cleanup_requires_stop = True
            return f"fallback_waiting_for_representable_date_{removal}"

        # Charge limit and schedule are each reconciled only from pushed/local state. There is
        # no billable schedule read and no repeated command on every optimizer tick.
        target_limit = int(round(target_soc))
        observed_limit, observed_limit_at = self._smart_observation(
            "tesla_soc_setpoint", "tesla_soc_setpoint_updated_at",
            "vehicle_soc_setpoint", "charge_limit_update_ts")
        pending_limit = getattr(self, "_smart_limit_pending", None)
        if isinstance(pending_limit, dict) and pending_limit.get("target") != target_limit:
            pending_limit = self._smart_limit_pending = None
        if (observed_limit_at > 0 and math.isfinite(observed_limit)
                and int(round(observed_limit)) == target_limit):
            self._smart_limit_signature = target_limit
            self._smart_limit_pending = None
            self._smart_limit_retry_after = 0.0
            limit_status = "confirmed"
        else:
            if getattr(self, "_smart_limit_signature", None) != target_limit:
                self._smart_limit_signature = None
            attempts = int((pending_limit or {}).get("attempts", 0))
            sent_at = _num((pending_limit or {}).get("sent_at"), 0.0)
            acknowledgement_due = (
                pending_limit is None
                or now_ts - sent_at >= SMART_COMMAND_ACK_TIMEOUT_S
                or observed_limit_at >= sent_at > 0
            )
            limit_retry_ready = now_ts >= getattr(
                self, "_smart_limit_retry_after", 0.0)
            if (acknowledgement_due and limit_retry_ready
                    and attempts < SMART_COMMAND_MAX_ATTEMPTS):
                result = self.tesla.set_tesla_charge_limit(target_limit)
                if self._smart_command_ok(result):
                    self._smart_limit_retry_after = 0.0
                    self._smart_limit_pending = {
                        "target": target_limit,
                        "sent_at": now_ts,
                        "attempts": attempts + 1,
                        "accepted": True,
                    }
                    pending_limit = self._smart_limit_pending
                else:
                    # Command transport/auth failures get their own short retry clock. A
                    # separate fallback-schedule failure must never postpone this critical
                    # charge-limit reconciliation.
                    self._smart_limit_pending = {
                        "target": target_limit,
                        "sent_at": now_ts,
                        "attempts": attempts + 1,
                        "accepted": False,
                    }
                    self._smart_limit_retry_after = now_ts + SMART_COMMAND_RETRY_S
                    return f"limit_{self._smart_command_category(result)}"
            pending_limit = getattr(self, "_smart_limit_pending", None)
            attempts = int((pending_limit or {}).get("attempts", attempts))
            final_sent_at = _num((pending_limit or {}).get("sent_at"), 0.0)
            final_due = (
                now_ts - final_sent_at >= SMART_COMMAND_ACK_TIMEOUT_S
                or observed_limit_at >= final_sent_at > 0
            )
            limit_status = (
                "limit_unconfirmed"
                if (attempts >= SMART_COMMAND_MAX_ATTEMPTS and final_due)
                else "limit_pending")
            # A rejected command is not safe grounds for installing an onboard schedule. Retry
            # it promptly and, after the bounded attempts, leave the plan visibly unconfirmed.
            # An accepted-but-not-yet-observed command may still install the deadline fallback.
            if isinstance(pending_limit, dict) and not pending_limit.get("accepted", False):
                return ("limit_unconfirmed" if attempts >= SMART_COMMAND_MAX_ATTEMPTS
                        else "limit_retry_backoff")

        start_minute = local_start.hour * 60 + local_start.minute
        end_minute = local_deadline.hour * 60 + local_deadline.minute
        signature = (
            smart["job_id"], local_start.date().isoformat(), start_minute, end_minute,
            self._tesla_weekday_mask(local_start), round(latitude, 5), round(longitude, 5),
        )
        if getattr(self, "_smart_schedule_signature", None) == signature:
            return limit_status
        if getattr(self, "_smart_schedule_failure_signature", None) != signature:
            self._smart_schedule_failure_signature = signature
            self._smart_schedule_failure_attempts = 0
        if getattr(self, "_smart_schedule_failure_attempts", 0) >= SMART_COMMAND_MAX_ATTEMPTS:
            return "schedule_unconfirmed"
        if now_ts < getattr(self, "_smart_schedule_retry_after", 0.0):
            return "retry_backoff"
        result = self.tesla.upsert_owned_charge_schedule(
            SMART_OWNED_SCHEDULE_ID,
            start_time=start_minute,
            end_time=end_minute,
            days_of_week=self._tesla_weekday_mask(local_start),
            latitude=latitude,
            longitude=longitude,
            one_time=True,
            enabled=True,
        )
        if self._smart_command_ok(result):
            self._smart_schedule_signature = signature
            self._smart_removed_signature = None
            self._smart_schedule_retry_after = 0.0
            self._smart_schedule_failure_signature = None
            self._smart_schedule_failure_attempts = 0
            return limit_status
        category = self._smart_command_category(result)
        if category in {"unsupported", "not_supported", "invalid_command"}:
            self._smart_schedule_supported = False
            logging.warning("EvCharger: Tesla charge schedules unsupported; continuing with live smart control.")
            return "unsupported"
        self._smart_schedule_failure_attempts = int(
            getattr(self, "_smart_schedule_failure_attempts", 0)) + 1
        self._smart_schedule_retry_after = now_ts + SMART_SCHEDULE_RETRY_S
        return f"failed_{category}"

    def _remove_smart_fallback(self, smart: dict, *, force=False) -> str:
        signature = getattr(self, "_smart_schedule_signature", None)
        removal_key = smart.get("job_id") or signature
        now_ts = smart.get("now", datetime.datetime.now(datetime.timezone.utc)).timestamp()
        if getattr(self, "_smart_remove_failure_key", None) != removal_key:
            self._smart_remove_failure_key = removal_key
            self._smart_remove_failure_attempts = 0
            self._smart_removed_schedule_ids = set()
            self._smart_removed_existing_owned_schedule = False
        if getattr(self, "_smart_remove_failure_attempts", 0) >= SMART_COMMAND_MAX_ATTEMPTS:
            return "remove_unconfirmed"
        if now_ts < getattr(self, "_smart_schedule_retry_after", 0.0):
            return "retry_backoff"
        if (signature is None and not force) or getattr(
                self, "_smart_removed_signature", None) == removal_key:
            return "not_installed"
        schedule_ids = ((SMART_LEGACY_OWNED_SCHEDULE_IDS
                         + (SMART_OWNED_SCHEDULE_ID,))
                        if force else (SMART_OWNED_SCHEDULE_ID,))
        removed_ids = getattr(self, "_smart_removed_schedule_ids", set())
        for schedule_id in schedule_ids:
            if schedule_id in removed_ids:
                continue
            result = self.tesla.remove_owned_charge_schedule(schedule_id)
            if not self._smart_command_ok(result):
                self._smart_remove_failure_attempts = int(
                    getattr(self, "_smart_remove_failure_attempts", 0)) + 1
                self._smart_schedule_retry_after = now_ts + SMART_SCHEDULE_RETRY_S
                return f"remove_{self._smart_command_category(result)}"
            if self._smart_command_category(result) == "ok":
                # An actual deletion (rather than schedule_not_found) is durable evidence that
                # an app-owned fallback existed and may own an outside-block charging session.
                self._smart_removed_existing_owned_schedule = True
            removed_ids.add(schedule_id)
            self._smart_removed_schedule_ids = removed_ids
        self._smart_schedule_signature = None
        self._smart_removed_signature = removal_key
        self._smart_schedule_retry_after = 0.0
        self._smart_remove_failure_key = None
        self._smart_remove_failure_attempts = 0
        return "removed"

    def _control_smart_charging(self, smart: dict) -> bool:
        """Execute stable smart-plan blocks while remaining subordinate to manual control."""
        job_id = smart.get("job_id", "")
        owns = bool(getattr(self, "_smart_owns_charge", False))

        # Live smart control is explicitly telemetry-confirmed. Without the Fleet stream we
        # cannot distinguish an accepted HTTP response from a command which actually changed
        # vehicle state within the required minute, so fail closed and leave legacy behavior
        # untouched whenever no applied smart job is present.
        if not self._telemetry_on():
            self._set_smart_state(
                "telemetry_required", reason="fleet_telemetry_disabled",
                job_id=job_id, fallback="not_reconciled")
            return False

        # Durable pause/edit/delete intent outranks snapshot age: an old active snapshot is
        # exactly what must be neutralized when replanning failed or the broker is offline.
        if smart.get("stale") and not smart.get("reconcile_status"):
            self._set_smart_state("stale_plan", reason="planner_snapshot_expired", job_id=job_id)
            if owns and self._charging_now():
                self._stop_charge("smart-charge plan became stale", force=True)
                if self._charge_mode is None:
                    self._smart_owns_charge = False
            return False

        if smart.get("paused") or smart.get("terminal") or not smart.get("actionable"):
            status = smart.get("reconcile_status") or (
                "paused" if smart.get("paused") else "completed")
            reason = smart.get("reconcile_reason") or "job_not_actionable"
            # A paused/terminal snapshot may be the first plan this process sees after a
            # restart. One idempotent removal of our deterministic ID is therefore warranted
            # even when the in-memory installed signature is unknown.
            fallback = self._remove_smart_fallback(smart, force=True)
            if owns and self._charging_now():
                self._stop_charge(f"smart-charge job {status}", force=False)
                if self._charge_mode is None:
                    self._smart_owns_charge = False
            self._set_smart_state(status, reason=reason, job_id=job_id,
                                  fallback=fallback)
            return False

        slot = smart.get("slot")
        charging = self._charging_now()
        owns = bool(getattr(self, "_smart_owns_charge", False))

        # A long-horizon one-time schedule cannot encode its calendar week. Clean up that exact
        # app-owned schedule before classifying a running session as external, because the bad
        # schedule may itself have started the session. Normal fallback installation remains
        # below the manual-ownership checks and therefore never mutates a genuine user charge.
        fallback = None
        if self._smart_fallback_is_beyond_tesla_week(smart):
            fallback = self._reconcile_smart_fallback(smart)
        if (getattr(self, "_smart_cleanup_requires_stop", False)
                and charging and slot is None):
            stopped = self._stop_charge(
                "invalid owned Tesla fallback was active", force=True)
            if (stopped and fallback in {
                    "fallback_waiting_for_representable_date_removed",
                    "fallback_waiting_for_representable_date_not_installed",
            }):
                self._smart_cleanup_requires_stop = False
            self._set_smart_state(
                "waiting", reason="invalid_fallback_removed",
                job_id=job_id, fallback=fallback)
            return False
        if (not charging
                and fallback == "fallback_waiting_for_representable_date_removed"):
            self._smart_cleanup_requires_stop = False

        # Fresh state says a charge which we started is now stopped before its block ended.
        # Without spending a read we cannot safely distinguish a Tesla-app stop from Maxem
        # temporarily removing all power, so manual/safety intent wins: suppress this block and
        # permit a later distinct block to resume normally.
        pending_start = getattr(self, "_smart_start_pending", None)
        if slot is not None and not charging and (
                owns or isinstance(pending_start, dict)):
            if isinstance(pending_start, dict):
                now_ts = smart["now"].timestamp()
                attempts = int(pending_start.get("attempts", 0))
                sent_at = _num(pending_start.get("sent_at"), 0.0)
                if now_ts - sent_at < SMART_COMMAND_ACK_TIMEOUT_S:
                    self._set_smart_state(
                        "starting", reason="start_confirmation_pending",
                        job_id=job_id, fallback="preserved")
                    return True
                if attempts < SMART_COMMAND_MAX_ATTEMPTS and self._cooldown_ok():
                    result = self.tesla.start_tesla_charge()
                    self._mark_command()
                    attempts += 1
                    self._smart_start_pending = {
                        "sent_at": now_ts,
                        "attempts": attempts,
                    }
                    if self._smart_command_ok(result):
                        self._smart_owns_charge = True
                        self._charge_mode = "smart"
                        self._set_smart_state(
                            "starting", reason="start_confirmation_retry",
                            job_id=job_id, fallback="preserved")
                        return True
                    if attempts < SMART_COMMAND_MAX_ATTEMPTS:
                        self._set_smart_state(
                            "starting", reason="start_retry_rejected",
                            job_id=job_id, fallback="preserved")
                        return True
                elif attempts < SMART_COMMAND_MAX_ATTEMPTS:
                    return True
                logging.warning(
                    "EvCharger: smart-charge start was not confirmed after %d attempts; "
                    "suppressing this block.", attempts)
            self._suppress_smart_block(smart, smart["now"])
            self._smart_owns_charge = False
            self._smart_start_pending = None
            self._charge_mode = None
            self._last_commanded_amps = None
            self._set_smart_state("manual_override", reason="owned_charge_stopped_externally",
                                  job_id=job_id, fallback="preserved")
            return False

        if slot is not None and self._smart_block_is_suppressed(smart):
            self._smart_owns_charge = False
            if self._charge_mode == "smart":
                self._charge_mode = None
            self._set_smart_state("manual_override", reason="charge_block_suppressed",
                                  job_id=job_id, fallback="preserved")
            return charging

        # A charge which this process did not start belongs to the user, Tesla app/schedule,
        # or another controller. Never stop it and never alter its requested current.
        if charging and not owns:
            if slot is not None:
                # Persist this exact block immediately. If the user subsequently stops their
                # externally-started session during the same slot, automation must not undo the
                # complete start/stop sequence on the next tick.
                self._suppress_smart_block(smart, smart["now"])
            self._set_smart_state("manual_override", reason="external_charge_in_progress",
                                  job_id=job_id, fallback="deferred_manual_override")
            return True

        if charging and owns:
            self._smart_start_pending = None

        if fallback is None:
            fallback = self._reconcile_smart_fallback(smart)

        limit_blocked = (fallback in {"limit_retry_backoff", "limit_unconfirmed"}
                         or fallback.startswith("limit_")
                         and fallback not in {"limit_pending"})
        if limit_blocked:
            # Never begin (or deliberately continue) a planned charge under a limit Tesla
            # rejected or that remained unobservable after all bounded attempts. In particular,
            # an older, higher vehicle limit must not silently override the user's job target.
            if owns and charging:
                self._stop_charge("smart-charge limit was not confirmed", force=True)
            self._set_smart_state(
                "command_failed", reason=fallback, job_id=job_id, fallback=fallback)
            return False

        if slot is None:
            if owns and charging:
                self._stop_charge("smart-charge block ended", force=False)
                if self._charge_mode is None:
                    self._smart_owns_charge = False
                self._set_smart_state("waiting", reason="between_charge_blocks",
                                      job_id=job_id, fallback=fallback)
                return False
            self._smart_owns_charge = False
            if self._charge_mode == "smart":
                self._charge_mode = None
            self._set_smart_state("waiting", reason="next_block_not_started",
                                  job_id=job_id, fallback=fallback)
            return False

        target = self._smart_target_amps(slot)
        if target <= 0:
            self._set_smart_state("waiting", reason="slot_target_below_one_amp",
                                  target_amps=target, job_id=job_id, fallback=fallback)
            return False

        if owns and charging:
            current_status, should_command = self._smart_current_ack(
                target, smart["now"].timestamp())
            if should_command and self._cooldown_ok():
                # Compare desired plan current with the last desired command, never with ABB
                # measured current; Maxem remains free to throttle actual delivery.
                if self.tesla.set_tesla_charge_amps(
                        target, installation_ceiling=self._smart_installation_ceiling()):
                    self._record_smart_current_command(
                        target, smart["now"].timestamp(), accepted=True)
                    self._mark_command()
                else:
                    self._record_smart_current_command(
                        target, smart["now"].timestamp(), accepted=False)
                    self._mark_command()
                    current_status = "rejected"
            reason = ("active_plan_block" if current_status == "confirmed"
                      else f"set_current_{current_status}")
            self._set_smart_state("charging", reason=reason,
                                  target_amps=target, job_id=job_id, fallback=fallback)
            return True

        if not self._cooldown_ok():
            self._set_smart_state("waiting", reason="command_cooldown",
                                  target_amps=target, job_id=job_id, fallback=fallback)
            return True
        current_status, should_command = self._smart_current_ack(
            target, smart["now"].timestamp())
        current_accepted_now = False
        if should_command:
            # Call the public setter exactly once. It intentionally owns Tesla's documented
            # sub-5A double-send workaround internally.
            current_accepted_now = bool(self.tesla.set_tesla_charge_amps(
                target, installation_ceiling=self._smart_installation_ceiling()))
            self._record_smart_current_command(
                target, smart["now"].timestamp(), accepted=current_accepted_now)
            if not current_accepted_now:
                self._mark_command()
                self._set_smart_state("command_failed", reason="set_current_rejected",
                                      target_amps=target, job_id=job_id, fallback=fallback)
                return True
        elif current_status not in {"confirmed"}:
            # Do not start with a current request that Tesla rejected or that exhausted its
            # acknowledgement attempts. An accepted command reaches this block only on a later
            # tick after a failed start; wait for its pushed confirmation before retrying start.
            self._set_smart_state("waiting", reason=f"set_current_{current_status}",
                                  target_amps=target, job_id=job_id, fallback=fallback)
            return True
        started = self.tesla.start_tesla_charge()
        self._mark_command()
        previous_start = getattr(self, "_smart_start_pending", None)
        self._smart_start_pending = {
            "sent_at": smart["now"].timestamp(),
            "attempts": int((previous_start or {}).get("attempts", 0)) + 1,
        }
        if self._smart_command_ok(started):
            self._smart_owns_charge = True
            self._charge_mode = "smart"
            self._set_smart_state("starting", reason="start_confirmation_pending",
                                  target_amps=target, job_id=job_id, fallback=fallback)
            return True
        self._set_smart_state("starting", reason="start_retry_rejected",
                              target_amps=target, job_id=job_id, fallback=fallback)
        return True

    def _status_confirmed_not_charging(self) -> bool:
        """True only when tesla.is_charging says False AND that reading is reasonably fresh.

        A stale/never-refreshed cache (e.g. a failed forced read right before this call, or the
        False default before the first status read completes) must NOT be trusted as proof the
        car isn't charging — only that we don't currently know. Paired with the local ABB meter,
        this decides whether there's genuinely nothing to stop.
        """
        if self.tesla.is_charging:
            return False
        last_update_ts = getattr(self.tesla, "last_update_ts", 0) or 0
        return (time.time() - last_update_ts) <= STALE_STATUS_MAX_AGE_S

    def _stop_charge(self, reason: str, force: bool = False) -> bool:
        """Stop the charge, with the LOCAL meter as the arbiter of success.

        If nothing is actually drawing (local meter ~0 and a FRESH confirmation that it's not
        flagged charging), there's nothing to stop, so we never wake/command the car ($0.02
        saved). Otherwise we issue the stop (which escalates with a wake+retry internally). We
        ONLY clear our charging state / zero the meter on a CONFIRMED 'ok'. On failure we
        deliberately leave the meter reading reality, so the next tick still sees the car drawing
        and re-issues; we back off to avoid hammering. ``force`` bypasses the command cooldown
        (deliberate stops).

        Retries are bounded to STOP_MAX_RETRIES: a stop is safety-critical and bypasses the
        Tesla spend budget entirely (``critical=True`` at the API layer), so an unreachable car
        stuck retrying forever could otherwise spend without limit. Once exhausted we stop
        auto-retrying and send a CRITICAL Pushover alert asking for manual intervention (Tesla
        app or physically unplugging the car) — see ``_notify_stop_escalation``.
        """
        now = time.time()
        if now < self._stop_backoff_until:
            return False
        if not force and not self._cooldown_ok():
            return False
        if self._stop_escalated:
            # Already exhausted STOP_MAX_RETRIES and alerted for manual intervention. Don't keep
            # spending critical (budget-bypassing) API calls on a car we've told the user about.
            return False
        # Nothing is drawing and we have a FRESH confirmation it's not charging -> nothing to
        # stop (no wake cost). A stale/unknown status is never treated as confirmation.
        if _num(self.charging_amps) < 1 and self._status_confirmed_not_charging():
            self._low_surplus_since = None
            self._charge_mode = None
            self._stop_attempt_count = 0
            self._stop_escalated = False
            return True
        attempt = self._stop_attempt_count + 1
        logging.info(f"EvCharger: {reason} — stopping charge (attempt {attempt}/{STOP_MAX_RETRIES}).")
        status = self.tesla.stop_tesla_charge()   # 'ok' | 'network' | 'failed'
        self._low_surplus_since = None
        self._mark_command()
        if status == 'ok':
            self.update_charging_amp_totals(0)     # confirmed stopped -> reflect zero draw
            self._charge_mode = None
            self._last_commanded_amps = None       # a fresh session re-commands from scratch
            self._stop_backoff_until = 0.0
            self._stop_attempt_count = 0
            self._stop_escalated = False
            return True
        # NOT confirmed stopped. Do NOT zero the meter — leave it showing the real draw so the
        # next tick detects the car is still charging and re-issues the stop.
        self._stop_attempt_count = attempt
        # 'network' is the only category stop_charge_robust() returns for a non-billable failure
        # (transport exception or a >=500 response — both refunded by tesla_budget). Anything
        # else ('failed') means Tesla accepted and billed the request but rejected the command.
        billable = "non-billable (<500 not reached; refunded)" if status == 'network' else "billable"
        logging.warning(f"EvCharger: stop attempt {attempt}/{STOP_MAX_RETRIES} not confirmed "
                        f"(reason={status}, {billable}); car may still be drawing.")
        if attempt >= STOP_MAX_RETRIES:
            self._stop_escalated = True
            self._notify_stop_escalation(status)
            return False
        self._stop_backoff_until = now + STOP_RETRY_BACKOFF_S
        logging.warning(f"EvCharger: retrying in ~{int(STOP_RETRY_BACKOFF_S / 60)}m.")
        self._notify_stop_failed(status)
        return False

    def _notify_stop_failed(self, status: str) -> None:
        now = time.time()
        if now - self._last_stop_alert_ts < STOP_ALERT_INTERVAL_S:
            return
        self._last_stop_alert_ts = now
        try:
            # Normal severity (not critical): a stop is now un-blockable by the budget, so this
            # only fires on a genuine transient network/transport failure — informational, and
            # the controller keeps retrying on its own (bounded — see STOP_MAX_RETRIES).
            pushover_notification(
                "EV charge stop not confirmed",
                f"Could not confirm the Tesla charge stopped ({status}). The car may still be "
                f"drawing power — it will keep retrying; stop it in the Tesla app if it persists.")
        except Exception as e:
            logging.info(f"EvCharger: could not send stop-failure alert: {e}")

    def _notify_stop_escalation(self, status: str) -> None:
        """CRITICAL alert once STOP_MAX_RETRIES automatic attempts have all failed. Automatic
        retrying stops here (see the ``_stop_escalated`` check in ``_stop_charge``) — bounding
        the safety-critical bypass so an unreachable car can't retry (and spend) forever. From
        this point the car needs manual intervention."""
        try:
            pushover_notification_critical(
                "EV charge stop FAILED — manual action needed",
                f"Could not stop the Tesla after {STOP_MAX_RETRIES} attempts (last reason: "
                f"{status}). Automatic retrying has stopped to avoid runaway API spend. Please "
                f"stop the charge manually — unplug the car or use the Tesla app.")
        except Exception as e:
            logging.info(f"EvCharger: could not send stop-escalation alert: {e}")

    def _start_surplus_charge(self) -> bool:
        if not self._cooldown_ok():
            return True
        target = int(_num(self.surplus_amps))
        logging.info(f"EvCharger: PV surplus — starting charge at ~{target} A.")
        self.set_surplus_amps(target)
        self.tesla.set_tesla_charge_amps(target)
        self.tesla.start_tesla_charge()
        self._last_commanded_amps = target
        self._charge_mode = 'surplus'
        self._mark_command()
        return True

    def _adjust_surplus_amps(self) -> bool:
        self._charge_mode = 'surplus'
        target = int(_num(self.surplus_amps))
        # Compare to what we LAST COMMANDED, not an asynchronously sampled meter value, to avoid
        # re-issuing set_charging_amps on measurement lag/noise every cooldown. Re-issue only
        # when the surplus-derived target actually moves.
        last = self._last_commanded_amps
        if (last is None or abs(target - last) >= AMP_ADJUST_MIN_DELTA) and self._cooldown_ok():
            logging.info(f"EvCharger: adjusting charge {last} A -> {target} A to match surplus.")
            self.set_surplus_amps(target)
            self.tesla.set_tesla_charge_amps(target)
            self._last_commanded_amps = target
            self.update_charging_amp_totals(target)
            self._mark_command()
        self._low_surplus_since = None
        return True

    def _maybe_stop_on_surplus_loss(self) -> bool:
        """We were surplus-charging and the surplus is gone. Ride out a short grace window
        (passing cloud) before stopping."""
        if not self._charging_now():
            self._low_surplus_since = None
            self._charge_mode = None
            return False
        now = time.time()
        if self._low_surplus_since is None:
            self._low_surplus_since = now
            logging.info(f"EvCharger: PV surplus dipped while surplus-charging; {SURPLUS_LOSS_GRACE_S:.0f}s grace before stopping.")
            return True
        if (now - self._low_surplus_since) >= SURPLUS_LOSS_GRACE_S:
            self._stop_charge("PV surplus did not recover", force=True)
            return False
        return True

    def dynamic_load_reservation_adjustment(self):
        ess_soc = _num(self.ess_soc)
        if ess_soc >= int(self.minimum_ess_soc) and not self.load_reservation_is_reduced:
            self.load_reservation = round((self.load_reservation / self.load_reservation_reduction_factor))
            self.load_reservation_is_reduced = True
            logging.info(f"EvCharger (dynamic load adjustment): Desired ESS SOC is reached at {round(ess_soc, 2)}%. applying the load"
                         f" reservation factor and setting to {self.load_reservation} Watts")

        elif ess_soc < int(self.minimum_ess_soc) and self.load_reservation_is_reduced:
            self.load_reservation = round((self.load_reservation * self.load_reservation_reduction_factor))
            self.load_reservation_is_reduced = False
            logging.info(f"EvCharger (dynamic load adjustment): ESS SOC is too low at {self.ess_soc}%. Restoring the load"
                         f"reservation to the default {self.load_reservation} Watts")

        else:
            logging.debug(f"EvCharger (dynamic load adjustment): No load adjustment is required. Current reservation is {self.load_reservation} Watts")

        publish_message("Tesla/vehicle0/solar/load_reservation", payload=f"{{\"value\": \"{self.load_reservation}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/solar/load_reservation_is_reduced", payload=f"{{\"value\": \"{self.load_reservation_is_reduced}\"}}", qos=0, retain=True)

    def set_surplus_amps(self, surplus_amps):
        self.global_state.set("surplus_amps", surplus_amps)
        publish_message("Tesla/vehicle0/solar/surplus_amps", payload=f"{{\"value\": \"{surplus_amps}\"}}", qos=0, retain=True)

        if surplus_amps > 0:
            publish_message("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"False\"}}", qos=0, retain=True)
        else:
            publish_message("Tesla/vehicle0/solar/insufficient_surplus", payload=f"{{\"value\": \"True\"}}", qos=0, retain=True)

    def set_surplus_watts(self, surplus_watts):
        self.global_state.set("surplus_watts", round(surplus_watts, 2))
        publish_message("Tesla/vehicle0/solar/surplus_watts", payload=f"{{\"value\": \"{surplus_watts}\"}}", qos=0, retain=True)
        publish_message("Tesla/vehicle0/solar/load_reservation", payload=f"{{\"value\": \"{self.load_reservation}\"}}", qos=0, retain=True)

    def update_charging_amp_totals(self, charging_amp_totals=None):
        # None => derive PER-PHASE (average) from the measured per-phase currents; an explicit 0
        # sets 0. STATE stays PER-PHASE for the surplus rate-matching math (watts/230/3).
        if charging_amp_totals is None:
            charging_amp_totals = (_num(self.l1_charging_amps)
                                   + _num(self.l2_charging_amps)
                                   + _num(self.l3_charging_amps)) / 3

        per_phase = round(_num(charging_amp_totals), 2)
        self.global_state.set("tesla_charging_amps_total", per_phase)
        # Do not publish Tesla/vehicle0/charging_amps here: this value can be a
        # requested target immediately after a command. The ABB event path is the
        # sole publisher of the shared measured-current topic.

    @staticmethod
    def is_the_sun_shining():
        return False if datetime.datetime.now().time().hour < 10 or datetime.datetime.now().time().hour >= 18 \
            else True

    def vehicle_status_msg(self):
        return f"EvCharger (vehicle): Charging: {self.tesla.is_charging}, Plugged: {self.tesla.is_plugged}, " \
               f"Car SOC: {self.tesla.vehicle_soc}%, Car SOC Setpoint: {self.tesla.vehicle_soc_setpoint}%, ESS SOC: {round(_num(self.ess_soc), 2)}%, " \
               f"Surplus: {self.surplus_watts}W / {self.surplus_amps}A" \
               f" ETA: {self.tesla.time_until_full}"

    def general_status_msg(self):
        return f"EvCharger (general): PV Surplus: {self.surplus_amps}A / {self.surplus_watts}W" \
                f" AC Loads: {self.acload_watts}W"

    @staticmethod
    def cleanup():
        logging.info("EvCharger: Topic Housecleaning...")
        # clear out topic which activates this UI widget in dashboard
        publish_message("Tesla", payload=None, qos=0, retain=False)
        # Deprecated
        # publish_message("Tesla/vehicle0/Ac/ac_loads", payload=None, qos=0, retain=False)
        # publish_message("Tesla/vehicle0/Ac/ac_in", payload=None, qos=0, retain=False)
        # publish_message("Tesla/vehicle0/Ac/tesla_load", payload=None, qos=0, retain=False)
