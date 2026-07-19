import json
import threading
import time
import asyncio

from lib.helpers.startup_helpers import restore_and_publish, apply_energy_broker_logic
from lib.constants import logging
from lib.config_retrieval import retrieve_setting
from lib.config_change_handler import ConfigWatcher, handle_env_change
from lib.victron_mqtt_client import mqtt_start, mqtt_stop
from lib.ev_charge_controller import EvCharger
from lib.task_scheduler import TaskScheduler
from lib.victron_integration import restore_default_battery_max_voltage, set_minimum_ess_soc
from lib.tibber_api import live_measurements, publish_pricing_data
from lib.helpers import publish_message, retrieve_message, is_truthy
from lib.global_state import GlobalStateDatabase, GlobalStateClient
from lib.solar_forecasting import get_victron_solar_forecast
from lib.energy_broker import (
    main as energybroker,
    get_todays_n_highest_prices,
    retrieve_latest_tibber_pricing,
)

GlobalStateDB = GlobalStateDatabase()
STATE = GlobalStateClient()

ACTIVE_MODULES = json.loads(retrieve_setting('ACTIVE_MODULES'))
HOME_CONNECT_APPLIANCE_SCHEDULING = is_truthy(retrieve_setting("HOME_CONNECT_APPLIANCE_SCHEDULING"))

def ev_charge_controller(): EvCharger().main()

def energy_broker(): energybroker()

def mqtt_client(loop):
    asyncio.set_event_loop(loop)
    asyncio.run(mqtt_start())

def sync_tasks_start():
    try:
        for module, service in ACTIVE_MODULES[0]['sync'].items():
            if service:
                logging.info(f"Starting {module}")
                exec(f"{module}()")

            # Clean up any mqtt topics related to the ev charge module if it's not active or the UI will show it
            if not ACTIVE_MODULES[0]['sync']['ev_charge_controller']:
                EvCharger.cleanup()

    except Exception as E:
        logging.error(f"sync_tasks_start (error): {E}")

def shutdown():
    logging.info("main(): Cleaning up and exiting...")

    if retrieve_setting('VICTRON_OPTIMIZED_CHARGING') == '1':
        restore_default_battery_max_voltage()

    mqtt_stop()

    # publish message to broker that we are shutting down
    publish_message("Cerbomoticzgx/system/shutdown", message="True", retain=True)

def init():
    if retrieve_message("Cerbomoticzgx/system/shutdown"):
        # let post_startup() know that this is a manually requested restart
        publish_message("Cerbomoticzgx/system/manual_restart", message="True", retain=True)

    # /api/restart publishes a retained True; clear it on boot so MQTT replay
    # cannot trigger a looping restart condition.
    publish_message("Cerbomoticzgx/system/shutdown", message="False", retain=True)

    # set higher than 0 zero cost at startup until actual pricing is retreived or auto sell/auto grid-assist might flap
    publish_message(topic='Tibber/home/price_info/now/total', message="0.35", retain=True)
    STATE.set('tibber_price_now', "0.35")

    # Re-assert the real Victron setting in the unconditional startup path and
    # before the one-second post-startup delay. GlobalState is recreated at
    # startup, and a missing key reads as numeric zero; without a forced write a
    # stale 40% winter limit could masquerade as the desired 0%.
    try:
        set_minimum_ess_soc(force=True)
    except Exception as e:
        logging.error(
            "init(): unable to apply Victron hardware minimum SoC; "
            "the next optimizer cycle will retry: %s",
            e,
        )


def post_startup():
    time.sleep(1)

    if HOME_CONNECT_APPLIANCE_SCHEDULING:
        logging.info(f"HomeConnect Appliance Scheduling module is enabled.")
    else:
        logging.info(f"HomeConnect Appliance Scheduling module is disabled.")

    logging.info(f"post_startup() actions executing...")

    # Re-apply state/configuration from previous run or sane defaults
    logging.info(f"post_startup(): Re-storing previous state if available...")

    restore_and_publish('ac_power_setpoint', default='0.0')
    restore_and_publish('ess_net_metering_batt_min_soc', default='80.0')
    restore_and_publish('ess_net_metering_enabled', default=False)
    restore_and_publish('ess_net_metering_overridden', default=False)
    restore_and_publish('ai_ess_override_enabled', default=False)
    restore_and_publish('grid_charging_enabled', default=False)
    restore_and_publish('grid_charging_enabled_by_price', default=False)
    restore_and_publish('ev_charge_requested', default=False)

    # Start the read-only dashboard EARLY (if enabled), BEFORE any network-bound
    # pricing/forecast work, so the web server is available immediately and can
    # never be delayed by a slow/unreachable Tibber or VRM. Guarded — a frontend
    # failure can never crash the controller.
    if str(retrieve_setting('FRONTEND_ENABLED') or '').strip().lower() in ('1', 'true', 'yes', 'on'):
        try:
            from frontend.server import run_in_thread
            run_in_thread()
            logging.info("Frontend dashboard started in-process (FRONTEND_ENABLED).")
        except Exception as FrontendError:
            logging.warning(f"Frontend dashboard failed to start; continuing without it: {FrontendError}")

    # Pricing/forecast warm-up hits Tibber + VRM and can block for tens of seconds
    # (or fail) during a third-party outage. Run it in a background daemon thread so
    # it can NEVER block startup, the scheduler, or the web server. Each step is
    # isolated so one failure doesn't skip the rest; the scheduler refreshes all of
    # this periodically, so a failed warm-up self-heals on the next cycle.
    def _startup_warm_up():
        logging.info("post_startup(): warming up pricing + solar forecast (background)…")
        for label, fn in (
            ("today's highest prices", lambda: get_todays_n_highest_prices(0, 100)),
            ("publish pricing", lambda: publish_pricing_data(__name__)),
            ("solar forecast", get_victron_solar_forecast),
            ("latest pricing", retrieve_latest_tibber_pricing),
            ("energy-broker recovery", apply_energy_broker_logic),
        ):
            try:
                fn()
            except Exception as e:
                logging.warning("post_startup warm-up '%s' failed (recovers on schedule): %s", label, e)
        logging.info("post_startup(): warm-up complete.")

    threading.Thread(target=_startup_warm_up, name="startup-warmup", daemon=True).start()

    # Start the Tesla Fleet Telemetry bridge if enabled. It subscribes to the in-cluster
    # fleet-telemetry MQTT firehose and republishes normalized Tesla/vehicle0/* state +
    # tesla_* GlobalState keys, so the EV controller/GUI read PUSHED data instead of polling
    # vehicle_data. Fully inert (returns None) when TESLA_TELEMETRY_ENABLED is off.
    try:
        from lib.tesla_telemetry_bridge import start_bridge_if_enabled
        if start_bridge_if_enabled(retrieve_setting):
            logging.info("Tesla Fleet Telemetry bridge started (TESLA_TELEMETRY_ENABLED).")
    except Exception as e:
        logging.warning("Tesla telemetry bridge failed to start; continuing without it: %s", e)

    # Start service scheduled tasks + the .env config watcher (independent of the
    # warm-up above, so they come up immediately).
    TaskScheduler()
    config_watcher = ConfigWatcher(handler=handle_env_change)
    config_watcher.start()

    logging.info(f"post_startup() actions complete. v{retrieve_setting('VERSION')} Initialization complete.")


def main():
    try:
        init()

        # start sync tasks
        sync_tasks_start()

        # start async tasks
        if ACTIVE_MODULES[0]['async']['mqtt_client'] and not ACTIVE_MODULES[0]['async']['tibber_api']:
            asyncio.run(mqtt_start())
            post_startup()

        elif ACTIVE_MODULES[0]['async']['mqtt_client'] and ACTIVE_MODULES[0]['async']['tibber_api']:
            mqtt_loop = asyncio.new_event_loop()
            mqtt_thread = threading.Thread(target=mqtt_client, args=(mqtt_loop,))
            mqtt_thread.start()

        if ACTIVE_MODULES[0]['async']['tibber_api']:
            post_startup()
            # The live feed blocks & acts as the parent pid of the service. A
            # transient Tibber transport error must not hard-crash the whole
            # controller, so retry with exponential backoff instead of a single
            # retry. Recoverable transport errors are handled inside
            # live_measurements() (which requests a supervised restart); this loop
            # is the safety net for anything that still bubbles up.
            backoff = 5.0
            while True:
                started = time.monotonic()
                try:
                    asyncio.run(live_measurements())
                    # A clean return means a handled transport error requested a
                    # restart; loop to re-establish the feed.
                    logging.warning("Tibber: live feed ended; re-establishing in %.0fs...", backoff)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as E:
                    logging.error("Tibber: live measurements stopped with reason: %s. Retrying in %.0fs...", E, backoff)
                # A feed that ran for a meaningful duration was healthy; reset the
                # backoff so an isolated drop reconnects quickly. Only escalate on
                # rapid, repeated failures.
                if time.monotonic() - started > 120.0:
                    backoff = 5.0
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 60.0)

    except (KeyboardInterrupt, SystemExit):
        shutdown()


if __name__ == "__main__":
    main()
