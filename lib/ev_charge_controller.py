import datetime
import time
import urllib3
import pytz
import threading

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.tesla_api import TeslaApi
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message, is_truthy
from lib.notifications import pushover_notification, pushover_notification_critical


# Charge-control tuning.
SURPLUS_MIN_AMPS = 2          # below this there isn't enough PV to bother charging
SURPLUS_LOSS_GRACE_S = 60     # ride out passing clouds before stopping a surplus charge
COMMAND_COOLDOWN_S = 90       # min spacing between start/stop/amp commands (anti-chatter + budget)
AMP_ADJUST_MIN_DELTA = 1      # only re-issue a set-amps command when it moves by >= this
STOP_RETRY_BACKOFF_S = 120    # after a network-rejected stop, wait before retrying
STOP_ALERT_INTERVAL_S = 900   # min spacing between "could not stop the car" Pushover alerts
STOP_MAX_RETRIES = 5          # bounded auto-retry attempts before escalating to a human (audit
                               # finding: an uncapped critical-bypass retry loop can blow past
                               # the Tesla budget guard entirely if the car stays unreachable)
STALE_STATUS_MAX_AGE_S = 300  # a cached tesla.is_charging older than this is UNKNOWN, not
                               # authoritative "not charging" (audit finding: a stale cache +
                               # the under-reading local meter can agree on "nothing to stop"
                               # while the car is still actually drawing)
# Surplus-driven "is the car here?" discovery wakes are rate-limited so an away/asleep car
# can't drain the wake budget; the tesla_budget guard is the hard backstop on top.
DISCOVERY_WAKE_INTERVAL_S = 3600     # at most one surplus-discovery wake per hour
DISCOVERY_AWAY_BACKOFF_S = 10800     # after finding the car NOT HOME, wait 3h before another wake
DISCOVERY_HOME_UNPLUGGED_BACKOFF_S = 1200  # home but not plugged -> recheck sooner (20m)


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
        self._last_discovery_wake_ts = 0.0 # rate-limit surplus-driven discovery wakes
        self._discovery_backoff_until = 0.0  # longer backoff after finding the car away
        self._last_status_state = None       # last logged state, so we only log on change

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
        """
        if self._intent_on():                 # dedicated EV-charge intent (decoupled from grid-assist)
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
            intent = self._intent_on()
            intent_on_edge = intent and not self._intent_was_on       # user just switched charging ON
            self._intent_off_edge = self._intent_was_on and not intent  # ...or OFF
            self._intent_was_on = intent

            # Engage if something wants a charge OR the user just switched intent off (so we can
            # stop the car). Otherwise stay dormant and make zero Tesla API calls.
            if not (self._local_engagement_signal() or self._intent_off_edge):
                self._log_status("dormant", self._dormant_reason())
                self._reschedule(30.0)
                return

            self.dynamic_load_reservation_adjustment()

            # Decide whether this tick may WAKE the car (the expensive call). Justified by:
            #  * a fresh intent toggle (on OR off) -> check/act now; or
            #  * ongoing intent / enough PV surplus -> a rate-limited discovery wake.
            # All wakes remain hard-capped by the tesla_budget guard.
            surplus = self._surplus_available()
            force = wake = False
            if intent_on_edge or self._intent_off_edge:
                force = wake = True
                if intent_on_edge:
                    logging.info("EvCharger: EV-charge request toggled on — checking vehicle now.")
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

    def _intent_on(self) -> bool:
        """Explicit intent to charge the car, read from the DEDICATED EV-charge flag
        (EV_CHARGE_INTENT_KEY). Fully decoupled from the ESS grid-assist toggle
        ('grid_charging_enabled'): toggling grid-assist must never start or stop the car.
        We also do NOT read 'tesla_charge_requested' (our own start/stop set it, which latched
        intent permanently on). Until a dedicated EV-charge button sets this key it stays off,
        so only PV-surplus charging can engage the car."""
        return is_truthy(self.global_state.get(EV_CHARGE_INTENT_KEY), False)

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

    def _control_charging(self) -> bool:
        """Decide and act on the car's charge. Returns True while actively charging.

        Two mutually-exclusive modes, never intermixed:
          * EV-CHARGE REQUEST (intent on) is an express override: charge at the car's own
            rate and IGNORE all PV-surplus logic. Stops only when the request is switched off
            or the car is full. Driven by the dedicated EV-charge flag, NOT grid-assist.
          * SURPLUS (intent off) charges only from genuine exportable PV surplus, matches the
            current to it, and stops when the surplus is gone (after a short cloud grace).
        Only home + plugged + non-supercharging cars are ever commanded.
        """
        t = self.tesla
        commandable = bool(t.is_home and t.is_plugged and not t.is_supercharging)
        if not commandable:
            self._low_surplus_since = None
            self._charge_mode = None
            if _num(self.charging_amps) < 1:
                self.update_charging_amp_totals(0)
            return False

        # 1) Express OFF: the EV-charge request just switched off -> HARD stop now. A fresh,
        #    deliberate off-edge is a new stop request, so it gets its own bounded attempts
        #    rather than staying silently suppressed by a stale escalation.
        if self._intent_off_edge:
            self._stop_attempt_count = 0
            self._stop_escalated = False
            self._stop_charge("EV-charge request turned off", force=True)
            return False

        # 2) Car reached its SoC limit -> stop.
        if t.is_full:
            if self._charging_now():
                self._stop_charge("car at its SoC limit", force=True)
            self._low_surplus_since = None
            self._charge_mode = None
            return False

        # 3) EV-CHARGE REQUEST ON = express override: charge at the car's own rate; ignore
        #    PV surplus entirely. Driven by the dedicated flag, NOT grid-assist.
        if self._intent_on():
            self._low_surplus_since = None
            self._charge_mode = 'grid'
            if not self._charging_now() and self._cooldown_ok():
                logging.info("EvCharger: EV-charge request on — starting charge (car's own rate).")
                self.tesla.start_tesla_charge()
                self._mark_command()
            return True

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

    def _status_confirmed_not_charging(self) -> bool:
        """True only when tesla.is_charging says False AND that reading is reasonably fresh.

        A stale/never-refreshed cache (e.g. a failed forced read right before this call, or the
        False default before the first status read completes) must NOT be trusted as proof the
        car isn't charging — only that we don't currently know. Paired with the local meter
        (which under-reads, audit M1, but never over-reads), this is what decides whether there's
        genuinely nothing to stop.
        """
        if self.tesla.is_charging:
            return False
        last_update_ts = getattr(self.tesla, "last_update_ts", 0) or 0
        return (time.time() - last_update_ts) <= STALE_STATUS_MAX_AGE_S

    def _stop_charge(self, reason: str, force: bool = False) -> None:
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
            return
        if not force and not self._cooldown_ok():
            return
        if self._stop_escalated:
            # Already exhausted STOP_MAX_RETRIES and alerted for manual intervention. Don't keep
            # spending critical (budget-bypassing) API calls on a car we've told the user about.
            return
        # Nothing is drawing and we have a FRESH confirmation it's not charging -> nothing to
        # stop (no wake cost). A stale/unknown status is never treated as confirmation.
        if _num(self.charging_amps) < 1 and self._status_confirmed_not_charging():
            self._low_surplus_since = None
            self._charge_mode = None
            self._stop_attempt_count = 0
            self._stop_escalated = False
            return
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
            return
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
            return
        self._stop_backoff_until = now + STOP_RETRY_BACKOFF_S
        logging.warning(f"EvCharger: retrying in ~{int(STOP_RETRY_BACKOFF_S / 60)}m.")
        self._notify_stop_failed(status)

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
        # Compare to what we LAST COMMANDED, not the local meter: the Victron evcharger meter
        # under-reads (audit M1), so comparing to `measured` would re-issue set_charging_amps
        # every cooldown forever and burn a billable read each time. Re-issue only when the
        # surplus-derived target actually moves.
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
        # DISPLAY: in telemetry mode the fleet-telemetry bridge owns Tesla/vehicle0/charging_amps
        # with the car's OWN accurate per-phase current (the local Victron meter under-reads ~3x),
        # so we must NOT overwrite it here. Only publish in legacy polling mode.
        if not self._telemetry_on():
            publish_message("Tesla/vehicle0/charging_amps",
                            payload=f"{{\"value\": \"{per_phase}\"}}", qos=0, retain=True)

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
