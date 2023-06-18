import json
import threading
import time
import asyncio

from lib.constants import logging, dotenv_config
from lib.mqtt_client import mqtt_start, mqtt_stop
from lib.ev_charge_controller import EvCharger
from lib.energy_broker import main as energybroker
from lib.victron_integration import restore_default_battery_max_voltage
from lib.tibber_api import live_measurements, publish_pricing_data
from lib.helpers import publish_message
from lib.global_state import GlobalStateDatabase
from lib.energy_broker import manage_sale_of_stored_energy_to_the_grid, manage_grid_usage_based_on_current_price


ACTIVE_MODULES = json.loads(dotenv_config('ACTIVE_MODULES'))
ESS_NET_METERING = bool(dotenv_config('TIBBER_UPDATES_ENABLED')) or False
GlobalState = GlobalStateDatabase()

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
    publish_message(f"Cerbomoticzgx/system/EssNetMeteringEnabled", message=f"{ESS_NET_METERING}", retain=True)

def post_startup():
    # update tibber pricing info
    publish_pricing_data(__name__)
    # Make sure we apply energy broker logic post startup to recover if the service restarts while in a
    # managed state.
    manage_sale_of_stored_energy_to_the_grid()
    manage_grid_usage_based_on_current_price()

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


if __name__ == "__main__":
    main()
