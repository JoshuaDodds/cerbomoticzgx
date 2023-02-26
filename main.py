import json
import threading
import time
import asyncio

from lib.constants import logging, dotenv_config
from lib.mqtt_client import mqtt_start, mqtt_stop
from lib.ev_charge_controller import EvCharger
from lib.energy_broker import main as energybroker
from lib.victron_integration import restore_default_battery_max_voltage
from lib.tibber_api import live_measurements
from lib.helpers import publish_message


ACTIVE_MODULES = json.loads(dotenv_config('ACTIVE_MODULES'))
EvChargeControl = EvCharger()


def ev_charge_controller(): EvChargeControl.main()

def energy_broker(): energybroker()

def mqtt_client(loop):
    asyncio.set_event_loop(loop)
    asyncio.run(mqtt_start(EvChargeControl))

def shutdown():
    logging.info("main(): Cleaning up and exiting...")

    if dotenv_config('VICTRON_OPTIMIZED_CHARGING') == '1':
        restore_default_battery_max_voltage()

    EvChargeControl.__del__()

    mqtt_stop()

    # publish message to broker that we are shutting down
    publish_message("Cerbomoticzgx/system/shutdown", "True")

def sync_tasks_start():
    try:
        for module, service in ACTIVE_MODULES[0]['sync'].items():
            if service:
                logging.info(f"Starting {module}")
                exec(f"{module}()")

    except Exception as E:
        logging.error(f"sync_tasks_start (error): {E}")

def main():
    try:
        # clear any previously published shutdown directives
        publish_message("Cerbomoticzgx/system/shutdown", "False")

        # start sync tasks
        sync_tasks_start()

        # start async tasks
        if ACTIVE_MODULES[0]['async']['mqtt_client'] and not ACTIVE_MODULES[0]['async']['tibber_api']:
            asyncio.run(mqtt_start(EvChargeControl))

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

    except (KeyboardInterrupt, SystemExit):
        shutdown()


if __name__ == "__main__":
    main()
