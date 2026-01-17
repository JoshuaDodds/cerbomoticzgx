import json

from lib.helpers import retrieve_message, publish_message
from lib.global_state import GlobalStateClient
from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.energy_broker import (
    manage_sale_of_stored_energy_to_the_grid,
    manage_grid_usage_based_on_current_price,
    set_charging_schedule,
    run_ai_optimizer)


STATE = GlobalStateClient()


def restore_and_publish(key, default=None, retain=True):
    value = (
        retrieve_message(key)
        or STATE.get(key)
        or retrieve_setting(key.upper())
        or default
    )
    publish_message(topic=f"Cerbomoticzgx/system/{key}", message=value, retain=retain)
    STATE.set(key, value)
    return value


def apply_energy_broker_logic():
    ACTIVE_MODULES = json.loads(retrieve_setting('ACTIVE_MODULES'))

    """Applies energy broker state and logic post startup."""
    if not ACTIVE_MODULES[0]['sync']['energy_broker']:
        return

    run_ai_optimizer()

    logging.info("Re-applying Energy Broker state and logic if set...")
    manage_sale_of_stored_energy_to_the_grid()
    manage_grid_usage_based_on_current_price()

    # Check and handle manual restart condition
    system_manual_restart = retrieve_message("Cerbomoticzgx/system/manual_restart")
    if system_manual_restart is False or system_manual_restart is None:
        logging.info("Updating the charging schedule based on currently available data...")
        set_charging_schedule("main.post_startup()")
    else:
        logging.info("Manual restart was requested. Skipping charge schedule update.")
        # Reset the manual restart flag
        publish_message("Cerbomoticzgx/system/manual_restart", message="False", retain=True)
