import json
import threading
import time
import asyncio

from lib.constants import logging, dotenv_config
from lib.mqtt_client import mqtt_start, mqtt_stop
from lib.ev_charge_controller import EvCharger
from lib.energy_broker import main as energybroker, get_todays_n_highest_prices
from lib.victron_integration import restore_default_battery_max_voltage
from lib.tibber_api import live_measurements, publish_pricing_data
from lib.helpers import publish_message, retrieve_message
from lib.global_state import GlobalStateDatabase, GlobalStateClient
from lib.energy_broker import (
    manage_sale_of_stored_energy_to_the_grid,
    manage_grid_usage_based_on_current_price,
)


ACTIVE_MODULES = json.loads(dotenv_config('ACTIVE_MODULES'))
ESS_NET_METERING = bool(dotenv_config('TIBBER_UPDATES_ENABLED')) or False
GlobalStateDB = GlobalStateDatabase()
STATE = GlobalStateClient()


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

    except Exception as E:
        logging.error(f"sync_tasks_start (error): {E}")

def shutdown():
    logging.info("main(): Cleaning up and exiting...")

    if dotenv_config('VICTRON_OPTIMIZED_CHARGING') == '1':
        restore_default_battery_max_voltage()

    mqtt_stop()

    # publish message to broker that we are shutting down
    publish_message("Cerbomoticzgx/system/shutdown", message="True", retain=True)

def init():
    # clear any previously published shutdown directives
    publish_message("Cerbomoticzgx/system/shutdown", message="False", retain=True)


def main():
    try:
        init()

        # start sync tasks
        sync_tasks_start()

        # start async tasks
        if ACTIVE_MODULES[0]['async']['mqtt_client'] and not ACTIVE_MODULES[0]['async']['tibber_api']:
            asyncio.run(mqtt_start())

        elif ACTIVE_MODULES[0]['async']['mqtt_client'] and ACTIVE_MODULES[0]['async']['tibber_api']:
            mqtt_loop = asyncio.new_event_loop()
            mqtt_thread = threading.Thread(target=mqtt_client, args=(mqtt_loop,))
            mqtt_thread.start()
            post_startup()

        if ACTIVE_MODULES[0]['async']['tibber_api']:
            try:
                asyncio.run(live_measurements())
            except Exception as E:
                logging.error(f"Tibber: live measurements stopped with reason: {E}. Restarting...")
                time.sleep(5.0)
                asyncio.run(live_measurements())

        post_startup()

    except (KeyboardInterrupt, SystemExit):
        shutdown()


def post_startup():
    # set higher than 0 zero cost at startup until actual pricing is retreived
    STATE.set('tibber_price_now', '0.010')

    # Re-apply previously set Dynamic ESS preferences set in the previous run
    AC_POWER_SETPOINT = retrieve_message('ac_power_setpoint') or '0.0'
    DYNAMIC_ESS_BATT_MIN_SOC = retrieve_message('ess_net_metering_batt_min_soc') or dotenv_config('DYNAMIC_ESS_BATT_MIN_SOC')
    DYNAMIC_ESS_NET_METERING_ENABLED = retrieve_message('ess_net_metering_enabled') or bool(dotenv_config('DYNAMIC_ESS_NET_METERING_ENABLED'))
    GRID_CHARGING_ENABLED = retrieve_message('grid_charging_enabled') or False

    STATE.set('ac_power_setpoint', AC_POWER_SETPOINT)

    publish_message(topic='Tesla/settings/grid_charging_enabled', message=str(GRID_CHARGING_ENABLED), retain=True)
    STATE.set('grid_charging_enabled', str(GRID_CHARGING_ENABLED))

    publish_message(topic='Cerbomoticzgx/system/EssNetMeteringBattMinSoc', message=str(DYNAMIC_ESS_BATT_MIN_SOC), retain=True)
    STATE.set('ess_net_metering_batt_min_soc', str(DYNAMIC_ESS_BATT_MIN_SOC))

    publish_message(topic='Cerbomoticzgx/system/EssNetMeteringEnabled', message=str(DYNAMIC_ESS_NET_METERING_ENABLED), retain=True)
    STATE.set('ess_net_metering_enabled', str(DYNAMIC_ESS_NET_METERING_ENABLED))

    publish_message(topic='Cerbomoticzgx/system/EssNetMeteringOverridden', message="False", retain=True)
    STATE.set('ess_net_metering_overridden', 'False')

    # clear the energy sale scheduling status message
    get_todays_n_highest_prices(0, 100)

    # update tibber pricing info
    publish_pricing_data(__name__)

    # Make sure we apply energy broker logic post startup to recover if the service restarts while in a
    # managed state.
    manage_sale_of_stored_energy_to_the_grid()
    manage_grid_usage_based_on_current_price()

    logging.info(f"post_startup() actions complete.")


if __name__ == "__main__":
    main()
