import time
import threading
import schedule as scheduler

from lib.constants import logging
from lib.global_state import GlobalStateClient
from lib.solar_forecasting import get_victron_solar_forecast
from lib.energy_broker import retrieve_latest_tibber_pricing


STATE = GlobalStateClient()


def TaskScheduler():
    logging.info("TaskScheduler: Initializing...")

    main_thread = threading.Thread(target=scheduler_loop)
    main_thread.daemon = True
    main_thread.start()

    logging.info("TaskScheduler: Started.")

def scheduler_loop():
    # Scheduled Tasks
    scheduler.every(5).minutes.do(get_victron_solar_forecast)
    scheduler.every(5).minutes.do(retrieve_latest_tibber_pricing)

    for job in scheduler.get_jobs():
        logging.info(f"TaskScheduler: job: {job}")

    while True:
        scheduler.run_pending()
        time.sleep(1)
