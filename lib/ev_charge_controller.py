import datetime
import time
import urllib3
import pytz
import threading

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.tesla_api import TeslaApi
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message
from lib.notifications import pushover_notification_critical


# Charge-control tuning.
SURPLUS_MIN_AMPS = 2          # below this there isn't enough PV to bother charging
SURPLUS_LOSS_GRACE_S = 60     # ride out passing clouds before stopping a surplus charge
COMMAND_COOLDOWN_S = 90       # min spacing between start/stop/amp commands (anti-chatter + budget)
AMP_ADJUST_MIN_DELTA = 1      # only re-issue a set-amps command when it moves by >= this
STOP_RETRY_BACKOFF_S = 120    # after a network-rejected stop, wait before retrying
STOP_ALERT_INTERVAL_S = 900   # min spacing between "could not stop the car" Pushover alerts
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

        self.grid_charging_enabled = self.global_state.get('grid_charging_enabled')
        self.load_reservation = int(retrieve_setting("LOAD_RESERVATION"))  # see example .env.example file
        self.load_reservation_is_reduced = False
        self.load_reservation_reduction_factor = float(retrieve_setting("LOAD_REDUCTION_FACTOR"))
        self.minimum_ess_soc = int(retrieve_setting("MINIMUM_ESS_SOC"))  # see example .env.example file

        self.tesla = TeslaApi()

        # Charge-control state.
        self._last_command_ts = 0.0        # for the command cooldown
        self._low_surplus_since = None     # start of the current surplus-loss grace window
        self._intent_was_on = False        # to detect the grid-assist / charge-request transition
        self._intent_off_edge = False      # set the tick intent switches OFF -> stop the charge now
        self._charge_mode = None           # 'grid' (express override) | 'surplus' | None
        self._stop_backoff_until = 0.0     # don't retry a rejected stop command every tick
        self._last_stop_alert_ts = 0.0     # rate-limit the "could not stop" Pushover alert
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
          * explicit intent: grid-assist or a Tesla charge request is toggled on; or
          * PV surplus: the sun is up, the house battery is already at/above its target
            SoC, and there is >= 2 A of exportable surplus (i.e. PV is spilling to grid); or
          * the car is already drawing power locally (measured charger amps) — we may need
            to adjust the rate or stop.
        This is the primary cost-avoidance layer; the tesla_budget guard is the backstop.
        """
        gs = self.global_state
        if gs.get('tesla_charge_requested') or gs.get('grid_charging_enabled'):
            return True
        if (self.is_the_sun_shining()
                and _num(self.ess_soc) >= self.minimum_ess_soc
                and _num(self.surplus_amps) >= SURPLUS_MIN_AMPS):
            return True
        # Stay engaged whenever we believe the car is charging (local amps OR cached Tesla
        # state) so we can still manage/stop it — e.g. when grid-assist is switched off.
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
                    logging.info("EvCharger: charge request / grid-assist toggled on — checking vehicle now.")
            elif (intent or surplus) and self._should_discovery_wake():
                force = wake = True
                self._last_discovery_wake_ts = time.time()
                why = "charge intent on" if intent else f"PV surplus {int(_num(self.surplus_amps))}A"
                logging.info(f"EvCharger: {why} — waking vehicle to check home/plugged state.")

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
    def _intent_on(self) -> bool:
        """Explicit intent to charge: grid-assist or a Tesla charge request is toggled on."""
        gs = self.global_state
        return bool(gs.get('tesla_charge_requested') or gs.get('grid_charging_enabled'))

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
          * GRID-ASSIST (intent on) is an express override: charge from grid at the car's
            own rate and IGNORE all PV-surplus logic. Stops only when intent is switched off
            or the car is full.
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

        # 1) Express OFF: grid-assist / charge request just switched off -> HARD stop now.
        if self._intent_off_edge:
            self._stop_charge("grid-assist / charge request turned off", force=True)
            return False

        # 2) Car reached its SoC limit -> stop.
        if t.is_full:
            if self._charging_now():
                self._stop_charge("car at its SoC limit", force=True)
            self._low_surplus_since = None
            self._charge_mode = None
            return False

        # 3) GRID-ASSIST ON = express override: charge from grid; ignore PV surplus entirely.
        if self._intent_on():
            self._low_surplus_since = None
            self._charge_mode = 'grid'
            if not self._charging_now() and self._cooldown_ok():
                logging.info("EvCharger: grid-assist on — starting charge (grid, car's own rate).")
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

    def _stop_charge(self, reason: str, force: bool = False) -> None:
        """Attempt to stop the charge. ``force`` bypasses the command cooldown (deliberate
        stops). tesla stop escalates internally: an asleep car is woken and retried at once
        (a charging car draws fast). A genuine network failure backs off AND alerts the user
        via Pushover, since the car may still be pulling power and needs manual intervention."""
        now = time.time()
        if now < self._stop_backoff_until:
            return
        if not force and not self._cooldown_ok():
            return
        logging.info(f"EvCharger: {reason} — stopping charge.")
        status = self.tesla.stop_tesla_charge()   # 'ok' | 'network' | 'failed'
        self.update_charging_amp_totals(0)
        self._low_surplus_since = None
        self._mark_command()
        if status == 'ok':
            self._charge_mode = None
            self._stop_backoff_until = 0.0
            return
        # Could NOT stop the car (network/transport). It may still be charging.
        self._stop_backoff_until = now + STOP_RETRY_BACKOFF_S
        logging.warning(f"EvCharger: FAILED to stop the car charge ({status}); "
                        f"retrying in ~{int(STOP_RETRY_BACKOFF_S / 60)}m.")
        self._notify_stop_failed(status)

    def _notify_stop_failed(self, status: str) -> None:
        now = time.time()
        if now - self._last_stop_alert_ts < STOP_ALERT_INTERVAL_S:
            return
        self._last_stop_alert_ts = now
        try:
            pushover_notification_critical(
                "EV charge STOP failed",
                f"Could not stop the Tesla charge ({status}). The car may still be drawing power — "
                f"please stop it manually in the Tesla app.")
        except Exception as e:
            logging.info(f"EvCharger: could not send stop-failure alert: {e}")

    def _start_surplus_charge(self) -> bool:
        if not self._cooldown_ok():
            return True
        target = int(_num(self.surplus_amps))
        logging.info(f"EvCharger: PV surplus — starting charge at ~{target} A.")
        self.set_surplus_amps(target)
        self.tesla.set_tesla_charge_amps(target)
        self.tesla.start_tesla_charge()
        self._charge_mode = 'surplus'
        self._mark_command()
        return True

    def _adjust_surplus_amps(self) -> bool:
        self._charge_mode = 'surplus'
        target = int(_num(self.surplus_amps))
        measured = round(_num(self.charging_amps))
        if abs(target - measured) >= AMP_ADJUST_MIN_DELTA and self._cooldown_ok():
            logging.info(f"EvCharger: adjusting charge {measured} A -> {target} A to match surplus.")
            self.set_surplus_amps(target)
            self.tesla.set_tesla_charge_amps(target)
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
        # None => derive from the measured per-phase currents; an explicit 0 must set 0
        # (the old `if not charging_amp_totals` wrongly re-derived when stopping).
        if charging_amp_totals is None:
            charging_amp_totals = (_num(self.l1_charging_amps)
                                   + _num(self.l2_charging_amps)
                                   + _num(self.l3_charging_amps)) / 3

        self.global_state.set("tesla_charging_amps_total", round(_num(charging_amp_totals), 2))
        publish_message("Tesla/vehicle0/charging_amps", payload=f"{{\"value\": \"{self.charging_amps}\"}}", qos=0, retain=True)

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
