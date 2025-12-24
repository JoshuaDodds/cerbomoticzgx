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

    # General always run tasks
    # Pricing and Forecasting Scheduled Tasks
    scheduler.every(15).minutes.do(get_victron_solar_forecast)
    scheduler.every(10).minutes.do(retrieve_latest_tibber_pricing)
    scheduler.every().hour.at(":01").do(retrieve_latest_tibber_pricing)
    scheduler.every().hour.at(":16").do(retrieve_latest_tibber_pricing)
    scheduler.every().hour.at(":31").do(retrieve_latest_tibber_pricing)
    scheduler.every().hour.at(":46").do(retrieve_latest_tibber_pricing)

    job_count = len(scheduler.get_jobs())
    logging.info(f"TaskScheduler: {job_count} jobs found and configured.")

    main_thread = threading.Thread(target=scheduler_loop)
    main_thread.daemon = True
    main_thread.start()

    logging.info("TaskScheduler: Started.")


def scheduler_loop():
    while True:
        scheduler.run_pending()
        time.sleep(1)
