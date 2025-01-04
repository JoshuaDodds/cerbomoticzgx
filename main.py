import json
import threading
import time
import asyncio

from lib.constants import logging
from lib.config_retrieval import retrieve_setting
from lib.config_change_handler import ConfigWatcher, handle_env_change
from lib.victron_mqtt_client import mqtt_start, mqtt_stop
from lib.ev_charge_controller import EvCharger
from lib.task_scheduler import TaskScheduler
from lib.victron_integration import restore_default_battery_max_voltage
from lib.tibber_api import live_measurements, publish_pricing_data
from lib.helpers import publish_message, retrieve_message
from lib.global_state import GlobalStateDatabase, GlobalStateClient
from lib.solar_forecasting import get_victron_solar_forecast
from lib.energy_broker import (
    main as energybroker,
    get_todays_n_highest_prices,
    set_charging_schedule,
    manage_sale_of_stored_energy_to_the_grid,
    manage_grid_usage_based_on_current_price,
    retrieve_latest_tibber_pricing,
)

GlobalStateDB = GlobalStateDatabase()
STATE = GlobalStateClient()

ACTIVE_MODULES = json.loads(retrieve_setting('ACTIVE_MODULES'))
ESS_NET_METERING = bool(retrieve_setting('TIBBER_UPDATES_ENABLED')) or False


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

    # Set shutdown state to false (prevent a looping restart condition)
    publish_message("Cerbomoticzgx/system/shutdown", message="False", retain=True)

    # set higher than 0 zero cost at startup until actual pricing is retreived or auto sell/auto grid-assist might flap
    publish_message(topic='Tibber/home/price_info/now/total', message="0.35", retain=True)
    STATE.set('tibber_price_now', "0.35")


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
            try:
                post_startup()
                asyncio.run(live_measurements())  # This blocks & acts as the parent pid of the cerbomoticGx service

            except Exception as E:
                logging.error(f"Tibber: live measurements stopped with reason: {E}. Restarting...")
                time.sleep(5.0)
                asyncio.run(live_measurements())  # This would also block if reached and would be our service's parent pid

    except (KeyboardInterrupt, SystemExit):
        shutdown()


def post_startup():
    time.sleep(2)
    logging.info(f"post_startup() actions executing...")

    # Re-apply previously set Dynamic ESS preferences set in the previous run
    logging.info(f"post_startup(): Re-storing previous state if available...")

    AC_POWER_SETPOINT = retrieve_message('ac_power_setpoint') or STATE.get('ac_power_setpoint') or '0.0'
    DYNAMIC_ESS_BATT_MIN_SOC = retrieve_message('ess_net_metering_batt_min_soc') or STATE.get("ess_net_metering_batt_min_soc") or retrieve_setting('DYNAMIC_ESS_BATT_MIN_SOC')
    DYNAMIC_ESS_NET_METERING_ENABLED = retrieve_message('ess_net_metering_enabled') or STATE.get("ess_net_metering_enabled") or retrieve_setting('DYNAMIC_ESS_NET_METERING_ENABLED')
    GRID_CHARGING_ENABLED = retrieve_message('grid_charging_enabled') or STATE.get("grid_charging_enabled") or False
    GRID_CHARGING_ENABLED_BY_PRICE = retrieve_message('grid_charging_enabled_by_price') or STATE.get("grid_charging_enabled_by_price") or False
    ESS_NET_METERING_OVERRIDDEN = retrieve_message('ess_net_metering_overridden') or STATE.get("ess_net_metering_overridden") or False
    TESLA_CHARGE_REQUESTED = retrieve_message('tesla_charge_requested') or STATE.get("tesla_charge_requested") or False

    # this one is victron maintained, so we just update our own state with what it is currently set to
    STATE.set('ac_power_setpoint', AC_POWER_SETPOINT)

    publish_message(topic='Tesla/settings/grid_charging_enabled', message=GRID_CHARGING_ENABLED, retain=True)
    STATE.set('grid_charging_enabled', str(GRID_CHARGING_ENABLED))

    publish_message(topic='Tesla/settings/grid_charging_enabled_by_price', message=GRID_CHARGING_ENABLED, retain=True)
    STATE.set('grid_charging_enabled_by_price', str(GRID_CHARGING_ENABLED_BY_PRICE))

    publish_message(topic='Tesla/vehicle0/control/charge_requested', message=TESLA_CHARGE_REQUESTED, retain=True)
    STATE.set('tesla_charge_requested', TESLA_CHARGE_REQUESTED)

    publish_message(topic='Cerbomoticzgx/system/EssNetMeteringBattMinSoc', message=str(DYNAMIC_ESS_BATT_MIN_SOC), retain=True)
    STATE.set('ess_net_metering_batt_min_soc', str(DYNAMIC_ESS_BATT_MIN_SOC))

    publish_message(topic='Cerbomoticzgx/system/EssNetMeteringEnabled', message=DYNAMIC_ESS_NET_METERING_ENABLED, retain=True)
    STATE.set('ess_net_metering_enabled', DYNAMIC_ESS_NET_METERING_ENABLED)

    publish_message(topic='Cerbomoticzgx/system/EssNetMeteringOverridden', message=ESS_NET_METERING_OVERRIDDEN, retain=True)
    STATE.set('ess_net_metering_overridden', ESS_NET_METERING_OVERRIDDEN)

    # clear the energy sale scheduling status message
    logging.info(f"post_startup(): Retrieving latest pricing data...")
    get_todays_n_highest_prices(0, 100)

    # update tibber pricing info and solar forecast
    logging.info(f"post_startup(): Publishing latest pricing and solar forecast data to data bus...")
    publish_pricing_data(__name__)
    get_victron_solar_forecast()
    retrieve_latest_tibber_pricing()

    # Make sure we apply energy broker logic post startup to recover if the service restarts while in a
    # managed state.
    if ACTIVE_MODULES[0]['sync']['energy_broker']:
        logging.info(f"post_startup(): Re-applying Energy Broker state and logic if set...")
        manage_sale_of_stored_energy_to_the_grid()
        manage_grid_usage_based_on_current_price()

        # re-run the charging scheduler based on current info and pricing if this was not a manual restart request
        system_manual_restart = retrieve_message("Cerbomoticzgx/system/manual_restart")

        if system_manual_restart is False or system_manual_restart is None:
            logging.info(f"post_startup(): Updating the charging schedule based on currently available data...")
            set_charging_schedule("main.post_startup()")
        else:
            logging.info(f"post_startup(): Manual restart was requested. Skipping charge schedule update.")
            # set the system_manual_restart value back to False
            publish_message("Cerbomoticzgx/system/manual_restart", message="False", retain=True)

    # Start the general scheduled tasks
    TaskScheduler()

    # Start the .env config watcher
    config_watcher = ConfigWatcher(handler=handle_env_change)
    config_watcher.start()

    logging.info(f"post_startup() actions complete.")


if __name__ == "__main__":
    main()
