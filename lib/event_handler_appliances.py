import json
import threading
import time
from datetime import datetime, timedelta

from lib.constants import logging
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message
from lib.tibber_api import lowest_24h_prices, lowest_48h_prices

gs_client = GlobalStateClient()

global_ready_flags = {
    "Dishwasher": False,
    "Dryer": False,
}

TRACKED_KEYS = [
    "SelectedProgram",
    "RemoteControlStartAllowed",
    "DoorState",
    "PowerState",
    "FinishInRelative",
    "OperationState",
    "DryingTarget",
    "RemoteControlLevel",
    "RemoteControlActive",
]


def send_delayed_start_to_dishwasher():
    logging.info("Sending delayed start command to Dishwasher...")

    delay_seconds = determine_optimal_run_time()

    # Send the delayed start program command
    delayed_start_command = {"program": 8203, "options": [{"uid": 558, "value": delay_seconds}]}
    topic = "Cerbomoticzgx/homeconnect/dishwasher/activeProgram"
    publish_message(
        topic=topic,
        payload=json.dumps(delayed_start_command)
    )
    logging.info(f"Sent start command to Dishwasher.")


def send_delayed_start_to_dryer():
    logging.info("Sending delayed start command to Dryer...")

    delay_seconds = determine_optimal_run_time()

    selected_program = int(gs_client.get('Dryer_SelectedProgram'))
    selected_program_runtime = int(gs_client.get('Dryer_FinishInRelative'))

    # Adjust to match dryer step size requirement for this value
    delay_seconds = round(delay_seconds / 60) * 60 + selected_program_runtime

    # Calculate the absolute start time
    current_time = datetime.now()
    start_time = current_time + timedelta(seconds=delay_seconds)

    # Check if start time is later than 8:30 PM
    if start_time.hour > 20 or (start_time.hour == 20 and start_time.minute > 30):
        logging.info("Calculated start time for Dryer is after 8:30 PM. Enforcing SilentDry programme.")
        # change program
        select_program_command = {"program": 32068}  # SilentDry program UID
        publish_message("Cerbomoticzgx/homeconnect/dryer/selectedProgram", payload=select_program_command)

        selected_program = int(gs_client.get('Dryer_SelectedProgram'))
        silent_dry_runtime = int(gs_client.get('Dryer_FinishInRelative'))
        delay_seconds = round(determine_optimal_run_time() / 60) * 60 + silent_dry_runtime

    # Send the delayed start program command
    delayed_start_command = {"program": selected_program, "options": [{"uid": 551, "value": delay_seconds}]}
    topic = "Cerbomoticzgx/homeconnect/dryer/activeProgram"
    publish_message(
        topic=topic,
        payload=json.dumps(delayed_start_command)
    )
    logging.info(f"Sent start command to Dryer.")


def handle_dryer_running_state():
    if gs_client.get('Dryer_RemoteControlStartAllowed'):
        try:
            # Abort the current program
            logging.info("Dryer is running. Sending abort command...")
            abort_command = {"uid": 512, "value": True}  # AbortProgram command
            publish_message(
                topic="Cerbomoticzgx/homeconnect/dryer/set",
                payload=json.dumps(abort_command)
            )
            logging.info("Sent abort command to Dryer.")

            # Start monitoring thread to wait for 'Ready' state
            wait_for_ready_state("Dryer", send_delayed_start_to_dryer)

        except Exception as e:
            logging.error(f"Unexpected error in handle_dryer_running_state(): {e}")


def handle_dishwasher_running_state():
    try:
        # Abort the current program
        logging.info("Dishwasher is running. Sending abort command...")
        abort_command = {"uid": 512, "value": True}  # AbortProgram command
        publish_message(
            topic="Cerbomoticzgx/homeconnect/dishwasher/set",
            payload=json.dumps(abort_command)
        )
        logging.info("Sent abort command to Dishwasher.")

        # Start monitoring thread to wait for 'Ready' state
        wait_for_ready_state("Dishwasher", send_delayed_start_to_dishwasher)

    except Exception as e:
        logging.error(f"Unexpected error in handle_dishwasher_running_state(): {e}")


def handle_dryer_event(payload):
    """
    Handles events for the dryer by processing the state and tracking relevant keys.
    :param payload: The payload containing the new state data.
    """
    try:
        new_state = payload if isinstance(payload, dict) else json.loads(payload)
        detect_changed_state_values("Dryer", new_state)
        store_appliance_state("Dryer", new_state)

    except Exception as e:
        logging.error(f"Unexpected error while handling dryer event: {e}")


def handle_dishwasher_event(payload):
    try:
        new_state = payload if isinstance(payload, dict) else json.loads(payload)
        detect_changed_state_values("Dishwasher", new_state)
        store_appliance_state("Dishwasher", new_state)

    except Exception as e:
        logging.error(f"Error in handle_dishwasher_event: {e}")


def handle_user_intervention(device, current_state, new_state):
    current_operation = current_state.get("OperationState")
    new_operation = new_state.get("OperationState")

    # User cancels a delayed start
    if current_operation == "DelayedStart" and new_operation == "Ready":
        logging.info(f"{device} delayed start was canceled by user.")
        user_intervention_count = int(gs_client.get(f"{device}_UserInterventionCount") or 0)
        user_intervention_count += 1
        gs_client.set(f"{device}_UserInterventionCount", user_intervention_count)
        logging.debug(f"{device} UserInterventionCount: {user_intervention_count}")
        return False  # Indicating further handling is not needed.

    # User starts the appliance from Ready to Run
    if current_operation == "Ready" and new_operation == "Run":
        logging.info(f"{device} was manually started by user.")
        user_intervention_count = int(gs_client.get(f"{device}_UserInterventionCount") or 0)
        user_intervention_count += 1
        gs_client.set(f"{device}_UserInterventionCount", user_intervention_count)
        logging.debug(f"{device} UserInterventionCount: {user_intervention_count}")

        # Allow immediate run if the user intervened multiple times
        if user_intervention_count >= 2:
            logging.info(f"{device} user intervened multiple times. Allowing immediate run.")
            gs_client.set(f"{device}_UserInterventionCount", 0)  # Reset count
            return True  # Allow the run.

    # Reset the count ONLY after a successful uninterrupted delayed start run
    if current_operation == "DelayedStart" and new_operation == "Run":
        logging.info(f"{device} is starting a scheduled operation. Resetting intervention count.")
        gs_client.set(f"{device}_UserInterventionCount", 0)
        return False  # Normal operation; no special handling.

    return False  # No special handling needed.


def detect_changed_state_values(device, new_state):
    current_state = retrieve_appliance_state(device)
    changes = {}

    logging.debug(f"Current state for {device}: {current_state}")
    logging.debug(f"New state for {device}: {new_state}")

    for key in TRACKED_KEYS:
        if key not in new_state or key not in current_state:
            logging.debug(f"Skipping comparison for {key} as it's missing in either state.")
            continue

        new_value = new_state[key]
        current_value = current_state[key]

        logging.debug(f"Comparing {key}: current={current_value}, new={new_value}")

        if str(current_value) != str(new_value):
            changes[key] = new_value
            logging.debug(f"{device} state change detected: {key}: {current_value} -> {new_value}")

            # Reset user intervention counter if PowerState changes to Off (todo: this might be confusing.  Its on hold for now)
            # if key == "PowerState" and new_value == "Off":
            #     logging.info(f"{device} power turned off. Resetting UserInterventionCount.")
            #     gs_client.set(f"{device}_UserInterventionCount", 0)
            #     continue

            # Call user intervention handler
            if key == "OperationState" and handle_user_intervention(device, current_state, new_state):
                logging.info(f"Allowing Immediate run for {device}.")
                return  # Skip further handling to allow immediate run.

            # Detect and handle appliances that start to run without a schedule set
            if key == "OperationState" and new_value == "Run":
                if not ((current_value in ["0", 0, "DelayedStart", "Pause"]) and new_value == "Run"):
                    logging.info(f"{device} has started without scheduling. Checking if there is a better time to run...")
                    if device == "Dishwasher":
                        handle_dishwasher_running_state()
                    if device == "Dryer":
                        handle_dryer_running_state()

    if changes:
        store_appliance_state(device, changes)
    else:
        logging.debug(f"No changes detected for {device}.")


def store_appliance_state(device, state):
    """
    Stores only the relevant keys from the appliance state in GlobalState.
    :param device: The name of the device (e.g., "Dryer", "Dishwasher").
    :param state: Dictionary of the state to store.
    """
    try:
        for key in TRACKED_KEYS:  # Iterate only over tracked keys
            if key in state:  # Store the key only if it exists in the incoming payload
                value = state[key]
                gs_client.set(f"{device}_{key}", value)
                logging.debug(f"Stored {device}_{key} = {value}")
    except Exception as e:
        logging.error(f"Failed to store state for {device}: {e}")


def retrieve_appliance_state(device):
    """
    Retrieves the tracked appliance state from GlobalState.
    :param device: The name of the device (e.g., "Dryer", "Dishwasher").
    :return: A dictionary containing the tracked state.
    """
    state = {}

    for key in TRACKED_KEYS:
        value = gs_client.get(f"{device}_{key}")
        if value is not None:
            state[key] = value  # The value is already correctly typecast by gs.get()
        else:
            logging.debug(f"{device}_{key} not found in GlobalState. Assuming key not initialized.")

    logging.debug(f"Retrieved state for {device}: {state}")
    return state


def calculate_delay_in_seconds(optimal_time):
    """
    Calculates the delay in seconds from the current time to the target time.
    :param optimal_time: List containing [day, hour, level, price].
    :return: Delay in seconds as an integer.
    """
    current_time = datetime.now()
    target_day, target_hour, _, _ = optimal_time  # Unpack the target day and hour

    # Determine target date and time
    target_date = current_time + timedelta(days=target_day)
    target_datetime = datetime(
        year=target_date.year,
        month=target_date.month,
        day=target_date.day,
        hour=target_hour,
        minute=0,
        second=0
    )

    # Calculate delay in seconds
    delay_seconds = (target_datetime - current_time).total_seconds()
    if delay_seconds < 0:
        raise ValueError("Calculated delay time is negative. Check optimal_time.")

    return int(delay_seconds)


def check_optimal_run_time(prices):
    """
    Finds the optimal time to run based on the pricing data.
    :param prices: List of price slots in the format [day, hour, level, price].
    :return: Optimal slot [day, hour, level, price] or None.
    """
    try:
        for price_data in prices:
            # Validate the structure of the price data
            if len(price_data) != 4:
                logging.error(f"Invalid price data format: {price_data}. Skipping.")
                continue

            day, hour, level, price = price_data

            # Add any additional validation here if needed
            if price >= 0:  # Example condition
                return price_data

        logging.info("No optimal time found in pricing data.")
        return None
    except Exception as e:
        logging.error(f"Unexpected error in check_optimal_run_time: {e}")
        return None


def determine_optimal_run_time(price_cap=0.38):
    """
    Determines the optimal time to run based on pricing and time of day.
    Returns the optimal time slot and calculated delay seconds.
    """
    logging.debug("Determining optimal run time...")
    current_time = datetime.now().hour
    current_datetime = datetime.now()

    # Filter pricing based on time of day
    # Before 7 PM: find the cheapest in the next 4-5 hours
    if current_time < 19:
        combined_prices = lowest_24h_prices(price_cap=price_cap, max_items=8)
        filtered_prices = [slot for slot in combined_prices if
                           0 <= (slot[0] * 24 + slot[1] - current_datetime.hour) <= 5]
    # After 7 PM: find the cheapest until 5:30 AM the next day
    else:
        combined_prices = lowest_48h_prices(price_cap=price_cap, max_items=8)
        filtered_prices = [slot for slot in combined_prices if
                           0 <= (slot[0] * 24 + slot[1] - current_datetime.hour) <= 10.5]

    # Find the cheapest time
    optimal_time = check_optimal_run_time(prices=filtered_prices)

    if not optimal_time:
        logging.info("No optimal time found. Scheduling immediate run.")
        return 0  # Immediate run
    else:
        delay_seconds = calculate_delay_in_seconds(optimal_time)
        return delay_seconds


def wait_for_ready_state(device, callback):
    """
    Waits for the appliance to transition to the 'Ready' state and executes a callback.
    Runs in a separate thread to avoid blocking the main process.

    :param device: The name of the device (e.g., "Dishwasher").
    :param callback: Function to call when the device is in the 'Ready' state.
    """
    def monitor_ready_state():
        logging.info(f"Starting monitoring thread for {device} readiness...")
        while True:
            try:
                operation_state = retrieve_appliance_state(device).get("OperationState")
                logging.debug(f"Current {device} OperationState: {operation_state}")

                if operation_state == "Ready":
                    logging.info(f"{device} is now in the 'Ready' state and should be ready for commands.")
                    global_ready_flags[device] = True
                    callback()
                    break

                if operation_state not in ["Aborting", "Run"]:
                    logging.warning(f"Unexpected state detected for {device}: {operation_state}. Exiting monitoring.")
                    break

                time.sleep(2)  # Check again after 2 seconds
            except Exception as e:
                logging.error(f"Error while monitoring {device} state: {e}")
                break

        logging.debug(f"Monitoring thread for {device} readiness has exited.")

    # Start the thread
    monitoring_thread = threading.Thread(target=monitor_ready_state, daemon=True)
    monitoring_thread.start()
