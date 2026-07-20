import json
import math
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from lib.appliance_mode import APPLIANCE_OPTIMIZATION_ENABLED
from lib.constants import logging
from lib.flexible_load_planner import plan_flexible_load
from lib.global_state import GlobalStateClient
from lib.helpers import is_truthy, publish_message
from lib.tibber_api import get_all_price_points, lowest_24h_prices, lowest_48h_prices

gs_client = GlobalStateClient()

global_ready_flags = {
    "Dishwasher": False,
    "Dryer": False,
}

PREFERRED_DISHWASHER_PROGRAM = 8203
SILENT_DRY_PROGRAM = 32068
APPLIANCE_READY_TIMEOUT_SECONDS = 90.0
APPLIANCE_ACK_TIMEOUT_SECONDS = 45.0
DISHWASHER_FALLBACK_RUNTIME_MINUTES = 60.0
DRYER_FALLBACK_RUNTIME_MINUTES = 150.0
DISHWASHER_ESTIMATED_POWER_W = 1200.0
DRYER_ESTIMATED_POWER_W = 900.0

_worker_guard = threading.Lock()
_active_workers = {}
_pending_plans = {}
_coordinator_commands = {}

TRACKED_KEYS = [
    "SelectedProgram",
    "RemoteControlStartAllowed",
    "DoorState",
    "PowerState",
    "FinishInRelative",
    "RemainingProgramTime",
    "EstimatedTotalProgramTime",
    "StartInRelative",
    "EnergyForecast",
    "OperationState",
    "DryingTarget",
    "RemoteControlLevel",
    "RemoteControlActive",
]


def price_deferral_enabled() -> bool:
    """Return the restart-frozen, season-independent appliance policy."""
    return bool(APPLIANCE_OPTIMIZATION_ENABLED)


def _plan_delay_seconds(device: str) -> int:
    plan = _pending_plans.get(device) or {}
    try:
        start = datetime.fromisoformat(str(plan["start"]).replace("Z", "+00:00"))
        now = datetime.now(start.tzinfo)
        return max(0, int(round((start - now).total_seconds())))
    except (KeyError, TypeError, ValueError):
        return determine_optimal_run_time()


def send_delayed_start_to_dishwasher():
    delay_seconds = _plan_delay_seconds("Dishwasher")

    # Convert delay_seconds into hours and minutes for friendly logging
    delay_time = timedelta(seconds=delay_seconds)
    hours, remainder = divmod(delay_time.total_seconds(), 3600)
    minutes = remainder // 60

    # Send the delayed start program command
    delayed_start_command = {
        "program": PREFERRED_DISHWASHER_PROGRAM,
        "options": [{"uid": 558, "value": delay_seconds}],
    }
    topic = "Cerbomoticzgx/homeconnect/dishwasher/activeProgram"
    publish_message(
        topic=topic,
        payload=json.dumps(delayed_start_command)
    )

    logging.info(f"Sent new start command to Dishwasher. Will start in {int(hours)} hr(s) {int(minutes)} min(s)")


def send_immediate_start_to_dishwasher():
    # Send the immediate start program command
    immediate_start_command = {
        "program": PREFERRED_DISHWASHER_PROGRAM,
        "options": [{"uid": 558, "value": 0}],
    }
    topic = "Cerbomoticzgx/homeconnect/dishwasher/activeProgram"
    publish_message(
        topic=topic,
        payload=json.dumps(immediate_start_command)
    )

    logging.info("Selected correct program and sent immediate start command to Dishwasher.")


def abort_dishwasher():
    logging.info("Sending abort command to Dishwasher...")
    abort_command = {"uid": 512, "value": True}
    publish_message(
        topic="Cerbomoticzgx/homeconnect/dishwasher/set",
        payload=json.dumps(abort_command)
    )
    logging.info("Sent abort command to Dishwasher.")


def send_delayed_start_to_dryer():
    plan = _pending_plans.get("Dryer") or {}
    start_delay_seconds = _plan_delay_seconds("Dryer")
    try:
        selected_program = int(plan.get("program") or _program_id("Dryer"))
    except (TypeError, ValueError):
        selected_program = 0
    if selected_program <= 0:
        raise ValueError("Dryer selected program unavailable")
    runtime_seconds = int(round(float(
        plan.get("runtime_minutes", DRYER_FALLBACK_RUNTIME_MINUTES) * 60.0
    )))

    try:
        end = datetime.fromisoformat(str(plan["end"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        end = datetime.now().astimezone() + timedelta(
            seconds=start_delay_seconds + runtime_seconds)
    if end.hour > 20 or (end.hour == 20 and end.minute > 30):
        logging.info(
            "Dryer completion is after 8:30 PM. Enforcing SilentDry programme.")
        selected_program = SILENT_DRY_PROGRAM
        # The activeProgram command below selects the programme and schedule as
        # one Home Connect operation.  A separate fire-and-forget selectedProgram
        # publish could race this command and expose stale runtime metadata.
        plan["program"] = selected_program

    # Home Connect UID 551 is FinishInRelative, not StartInRelative.
    finish_in_seconds = round(start_delay_seconds / 60) * 60 + runtime_seconds

    # Get hours and minutes for friendly logging
    delay_time = timedelta(seconds=start_delay_seconds)
    hours, remainder = divmod(delay_time.total_seconds(), 3600)
    minutes = remainder // 60

    # Send the delayed start program command
    delayed_start_command = {
        "program": selected_program,
        "options": [{"uid": 551, "value": finish_in_seconds}],
    }
    topic = "Cerbomoticzgx/homeconnect/dryer/activeProgram"
    publish_message(
        topic=topic,
        payload=json.dumps(delayed_start_command)
    )
    logging.info(
        f"Sent new start command to Dryer. Will start in "
        f"{int(hours)} hr(s) {int(minutes)} min(s)")


def abort_dryer():
    logging.info("Dryer is running. Sending abort command...")
    publish_message(
        topic="Cerbomoticzgx/homeconnect/dryer/set",
        payload=json.dumps({"uid": 512, "value": True}),
    )


def _positive_minutes(device, keys, fallback):
    for key in keys:
        try:
            seconds = float(gs_client.get(f"{device}_{key}"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return max(15.0, seconds / 60.0)
    return float(fallback)


def _program_id(device):
    try:
        value = int(gs_client.get(f"{device}_SelectedProgram") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _completion_after_quiet_threshold(plan):
    try:
        end = datetime.fromisoformat(str(plan["end"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return end.hour > 20 or (end.hour == 20 and end.minute > 30)


def _prepare_appliance_plan(device):
    """Build a valid replacement before the running program is aborted."""
    now = datetime.now().astimezone()
    if device == "Dishwasher":
        selected = _program_id("Dishwasher")
        runtime = _positive_minutes(
            device,
            ("RemainingProgramTime", "EstimatedTotalProgramTime")
            if selected == PREFERRED_DISHWASHER_PROGRAM else (),
            DISHWASHER_FALLBACK_RUNTIME_MINUTES,
        )
        power_w = DISHWASHER_ESTIMATED_POWER_W
        program = PREFERRED_DISHWASHER_PROGRAM
    else:
        runtime = _positive_minutes(
            device, ("FinishInRelative", "RemainingProgramTime"),
            DRYER_FALLBACK_RUNTIME_MINUTES)
        power_w = DRYER_ESTIMATED_POWER_W
        program = _program_id("Dryer")
        if program <= 0:
            logging.warning("Appliance scheduler: Dryer selected program unavailable.")
            return None

    if not price_deferral_enabled():
        return {
            "device": device,
            "decision": "immediate",
            "reason": "appliance_optimization_disabled",
            "start": now.isoformat(),
            "end": (now + timedelta(minutes=runtime)).isoformat(),
            "runtime_minutes": runtime,
            "load_kw": power_w / 1000.0,
            "program": program,
            "load_profile": [],
        }

    try:
        price_points = get_all_price_points()
        plan = plan_flexible_load(
            device=device.lower(),
            earliest_start=now,
            runtime_minutes=runtime,
            power_w=power_w,
            price_points=price_points,
        )
        if (
            device == "Dryer"
            and plan.get("decision") == "delayed"
            and _completion_after_quiet_threshold(plan)
        ):
            # SilentDry can run longer than the programme the user first chose.
            # Re-price with a conservative runtime before aborting so both the
            # 05:30 deadline and the optimizer reservation remain trustworthy.
            runtime = max(runtime, DRYER_FALLBACK_RUNTIME_MINUTES)
            plan = plan_flexible_load(
                device=device.lower(),
                earliest_start=now,
                runtime_minutes=runtime,
                power_w=power_w,
                price_points=price_points,
            )
            if plan.get("decision") == "delayed":
                program = SILENT_DRY_PROGRAM
                plan["runtime_source"] = "silent_dry_conservative_fallback"
    except Exception as error:
        logging.error(
            "Appliance scheduler: unable to price-plan %s before abort: %s",
            device,
            error,
        )
        # The preferred dishwasher programme is a separate household contract
        # from price deferral.  If price data is unavailable, retain that contract
        # with an immediate replacement.  A dryer already has the user's selected
        # programme, so its safest fallback is to leave the current run untouched.
        if device == "Dryer":
            return None
        return {
            "device": device,
            "decision": "immediate",
            "reason": "price_plan_unavailable",
            "start": now.isoformat(),
            "end": (now + timedelta(minutes=runtime)).isoformat(),
            "runtime_minutes": runtime,
            "load_kw": power_w / 1000.0,
            "program": program,
            "load_profile": [],
        }
    plan["device"] = device
    plan["program"] = program
    plan["source"] = "appliance_optimizer"
    return plan


def _remote_start_available(device):
    state = retrieve_appliance_state(device)
    allowed = state.get("RemoteControlStartAllowed")
    active = state.get("RemoteControlActive")
    door = str(state.get("DoorState") or "").lower()
    operation = state.get("OperationState")
    return bool(
        is_truthy(allowed)
        and is_truthy(active)
        and door == "closed"
        and operation == "Run"
    )


def _wait_for_operation_state(
        device, expected, timeout_seconds=APPLIANCE_ACK_TIMEOUT_SECONDS,
        poll_interval=2.0, expected_program=None):
    """Wait for an operation transition after issuing an appliance command.

    ``SelectedProgram`` is diagnostic only.  Home Connect may continue to
    report the programme selected in the appliance UI after an atomic
    ``activeProgram`` command has started another programme.  The operation
    transition is therefore the authoritative acknowledgement.
    """
    coordinator_command = _coordinator_commands.get(device) or {}
    if (
        coordinator_command.get("expected_operation") == expected
        and coordinator_command.get("acknowledged") is True
    ):
        return True
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() <= deadline:
        coordinator_command = _coordinator_commands.get(device) or {}
        if (
            coordinator_command.get("expected_operation") == expected
            and coordinator_command.get("acknowledged") is True
        ):
            return True
        state = retrieve_appliance_state(device)
        operation_matches = state.get("OperationState") == expected
        if operation_matches:
            observed_program = state.get("SelectedProgram")
            if (
                expected_program is not None
                and observed_program not in (None, 0, "0", "")
                and str(observed_program) != str(expected_program)
            ):
                # Live Home Connect telemetry can retain the UI-selected programme
                # even after an activeProgram command starts a different programme.
                # OperationState is the delivery acknowledgement; retrying a
                # running appliance because SelectedProgram is stale is unsafe.
                logging.warning(
                    "Appliance scheduler: %s reached %s after programme %s was "
                    "requested, but SelectedProgram still reports %s.",
                    device,
                    expected,
                    expected_program,
                    observed_program,
                )
            return True
        time.sleep(max(0.0, float(poll_interval)))
    return False


def _request_optimizer_replan():
    def run():
        try:
            from lib.energy_broker import run_ai_optimizer
            run_ai_optimizer()
        except Exception as error:
            logging.warning("Appliance scheduler: optimizer replan request failed: %s", error)

    threading.Thread(
        target=run, name="appliance-optimizer-replan", daemon=True).start()


def _set_schedule_status(device, status, detail=None):
    gs_client.set(f"{device}_SchedulerStatus", status)
    if detail is not None:
        gs_client.set(f"{device}_SchedulerDetail", detail)


def _persist_delayed_plan(device, plan):
    from lib import appliance_reservations

    appliance_reservations.upsert(plan)
    _set_schedule_status(device, "DelayedStart", plan.get("start"))
    _request_optimizer_replan()


def _remove_reservation(device):
    from lib import appliance_reservations

    if appliance_reservations.remove(device):
        _request_optimizer_replan()


def _coordinated_callback(device, callback, expected_operation):
    """Mark the next device transition as coordinator-owned before publishing."""
    @wraps(callback)
    def run():
        _coordinator_commands[device] = {
            "expected_operation": expected_operation,
            "issued_at": time.monotonic(),
            "acknowledged": False,
        }
        callback()

    return run


def _attempt_immediate_fallback(device, plan):
    """Issue one best-effort immediate start after an acknowledged abort."""
    _remove_reservation(device)
    if retrieve_appliance_state(device).get("OperationState") != "Ready":
        return False
    _coordinator_commands[device] = {
        "expected_operation": "Run",
        "issued_at": time.monotonic(),
        "acknowledged": False,
    }
    try:
        if device == "Dishwasher":
            send_immediate_start_to_dishwasher()
        else:
            # FinishInRelative equal to runtime means start now.
            fallback = dict(plan)
            fallback["start"] = datetime.now().astimezone().isoformat()
            fallback["decision"] = "immediate"
            _pending_plans[device] = fallback
            send_delayed_start_to_dryer()
        fallback_plan = _pending_plans.get(device) or plan
        if _wait_for_operation_state(
            device, "Run", expected_program=fallback_plan.get("program")):
            _set_schedule_status(device, "ImmediateFallback")
            return True
        logging.critical(
            "Appliance scheduler: %s immediate fallback was not acknowledged; "
            "manual attention may be required.",
            device,
        )
        _set_schedule_status(device, "FallbackFailed")
        return False
    finally:
        _coordinator_commands.pop(device, None)


def _reschedule_worker(device):
    try:
        plan = _prepare_appliance_plan(device)
    except Exception as error:
        logging.error("Appliance scheduler: %s planning failed safely: %s", device, error)
        _set_schedule_status(device, "PlanUnavailable", str(error))
        return
    if plan is None:
        _set_schedule_status(device, "PlanUnavailable")
        return

    delayed = plan.get("decision") == "delayed"
    if device == "Dryer" and not delayed:
        _set_schedule_status(device, "Immediate", plan.get("reason"))
        return
    if (
        device == "Dishwasher"
        and not delayed
        and _program_id("Dishwasher") == PREFERRED_DISHWASHER_PROGRAM
    ):
        _remove_reservation(device)
        _set_schedule_status(device, "Immediate", "preferred_program_already_running")
        return

    if not _remote_start_available(device):
        logging.warning(
            "Appliance scheduler: %s remote start is unavailable; leaving current run untouched.",
            device,
        )
        _set_schedule_status(device, "RemoteStartUnavailable")
        return

    callback = (
        send_delayed_start_to_dishwasher if device == "Dishwasher" and delayed
        else send_immediate_start_to_dishwasher if device == "Dishwasher"
        else send_delayed_start_to_dryer
    )
    expected = "DelayedStart" if delayed else "Run"
    coordinated_callback = _coordinated_callback(device, callback, expected)
    _pending_plans[device] = plan
    try:
        abort_dishwasher() if device == "Dishwasher" else abort_dryer()
        _set_schedule_status(device, "Aborting")
        if not wait_for_ready_state(
            device,
            coordinated_callback,
            timeout_seconds=APPLIANCE_READY_TIMEOUT_SECONDS,
            departure_observed=True,
        ):
            # A false result can mean either that abort never completed or that
            # Ready was reached but the replacement callback failed.  Only the
            # latter is safe to retry, and then only once with an immediate start.
            if retrieve_appliance_state(device).get("OperationState") == "Ready":
                _attempt_immediate_fallback(device, plan)
                return
            _set_schedule_status(device, "ReadyTimeout")
            return
        accepted_plan = _pending_plans.get(device) or plan
        if _wait_for_operation_state(
            device,
            expected,
            expected_program=accepted_plan.get("program"),
        ):
            if delayed:
                _persist_delayed_plan(device, accepted_plan)
            else:
                _remove_reservation(device)
                _set_schedule_status(device, "Immediate")
            return

        logging.error(
            "Appliance scheduler: %s did not acknowledge %s; attempting one immediate fallback.",
            device,
            expected,
        )
        if not _attempt_immediate_fallback(device, plan):
            _set_schedule_status(device, "FallbackFailed")
    except Exception as error:
        logging.error("Appliance scheduler: %s coordination failed: %s", device, error)
        _set_schedule_status(device, "Failed", str(error))
    finally:
        _coordinator_commands.pop(device, None)
        _pending_plans.pop(device, None)


def _start_reschedule_worker(device):
    with _worker_guard:
        existing = _active_workers.get(device)
        if existing is not None and existing.is_alive():
            logging.info("Appliance scheduler: %s worker already active; ignoring duplicate.", device)
            return False

        def run():
            try:
                _reschedule_worker(device)
            finally:
                with _worker_guard:
                    _active_workers.pop(device, None)

        worker = threading.Thread(
            target=run,
            name=f"appliance-scheduler-{device.lower()}",
            daemon=True,
        )
        _active_workers[device] = worker
        worker.start()
        return True


def handle_dryer_running_state():
    if not price_deferral_enabled():
        logging.info("Appliance scheduler: price deferral disabled; allowing Dryer to run.")
        return
    _start_reschedule_worker("Dryer")


def handle_dishwasher_running_state():
    # Preferred-program enforcement remains active whenever Home Connect handling
    # is enabled; only the choice between immediate and delayed is feature-gated.
    _start_reschedule_worker("Dishwasher")


def handle_dryer_event(payload, *, automation_enabled=True):
    """
    Handles events for the dryer by processing the state and tracking relevant keys.
    :param payload: The payload containing the new state data.
    """
    try:
        new_state = payload if isinstance(payload, dict) else json.loads(payload)
        detect_changed_state_values(
            "Dryer", new_state, automation_enabled=automation_enabled)

    except Exception as e:
        logging.error(f"Unexpected error while handling dryer event: {e}")


def handle_dishwasher_event(payload, *, automation_enabled=True):
    try:
        new_state = payload if isinstance(payload, dict) else json.loads(payload)
        detect_changed_state_values(
            "Dishwasher", new_state, automation_enabled=automation_enabled)

    except Exception as e:
        logging.error(f"Error in handle_dishwasher_event: {e}")


def handle_user_intervention(device, current_state, new_state):
    current_operation = current_state.get("OperationState")
    new_operation = new_state.get("OperationState")

    coordinator_command = _coordinator_commands.get(device) or {}
    if (
        current_operation == "Ready"
        and new_operation == coordinator_command.get("expected_operation") == "Run"
        and coordinator_command.get("acknowledged") is not True
    ):
        coordinator_command["acknowledged"] = True
        logging.info(
            "%s started from the appliance scheduler command; preserving the "
            "user-intervention count.",
            device,
        )
        return True

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
        if _is_materially_early_scheduled_start(device):
            logging.info(
                "%s delayed start was manually started early; honoring override.",
                device,
            )
            _remove_reservation(device)
            gs_client.set(f"{device}_UserInterventionCount", 0)
            _set_schedule_status(device, "ManualOverride")
            return True
        logging.info(f"{device} is starting a scheduled operation. Resetting intervention count.")
        gs_client.set(f"{device}_UserInterventionCount", 0)
        return False  # Normal operation; no special handling.

    return False  # No special handling needed.


def _is_materially_early_scheduled_start(device, *, tolerance_seconds=120.0):
    value = gs_client.get(f"{device}_SchedulerDetail")
    try:
        expected = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if expected.tzinfo is None or expected.utcoffset() is None:
        return False
    return datetime.now(expected.tzinfo).timestamp() < (
        expected.timestamp() - max(0.0, float(tolerance_seconds)))


def _release_reservation_for_transition(device, current_operation, new_operation):
    """Remove forecast work only once it is cancelled or no longer running."""
    terminal = {"Finished", "Inactive", "Ready"}
    if current_operation in {"DelayedStart", "Run", "Pause"} and new_operation in terminal:
        _remove_reservation(device)
    if current_operation in {"Run", "Pause"} and new_operation in {"Finished", "Inactive"}:
        gs_client.set(f"{device}_UserInterventionCount", 0)


def detect_changed_state_values(device, new_state, *, automation_enabled=True):
    current_state = retrieve_appliance_state(device)
    changes = {}
    schedule_unscheduled_run = False

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

            if key == "OperationState":
                coordinator_command = _coordinator_commands.get(device) or {}
                if (
                    current_value == "Ready"
                    and new_value == coordinator_command.get("expected_operation")
                    and new_value == "DelayedStart"
                    and coordinator_command.get("acknowledged") is not True
                ):
                    coordinator_command["acknowledged"] = True
                _release_reservation_for_transition(
                    device, current_value, new_value)
                if (
                    automation_enabled
                    and handle_user_intervention(device, current_state, new_state)
                ):
                    logging.info(
                        "%s Run transition accepted without another scheduling pass.",
                        device,
                    )
                    automation_enabled = False

            # Detect and handle appliances that start to run without a schedule set
            if key == "OperationState" and new_value == "Run":
                if not ((current_value in ["0", 0, "DelayedStart", "Pause"]) and new_value == "Run"):
                    if automation_enabled:
                        logging.info(
                            "%s has started without scheduling. Checking if there "
                            "is a better time to run...",
                            device,
                        )
                        schedule_unscheduled_run = True

    if new_state.get("OperationState") in {"Finished", "Inactive", "Ready"}:
        # Reconcile a persisted reservation even when the service missed the
        # transition or automation is now disabled.
        _remove_reservation(device)

    # Commit the complete incoming snapshot before a worker is allowed to read
    # programme/runtime/remote-start metadata.  The old snapshot above remains
    # authoritative only for transition detection.
    store_appliance_state(device, new_state)
    if not changes:
        logging.debug(f"No changes detected for {device}.")
    if schedule_unscheduled_run:
        if device == "Dishwasher":
            handle_dishwasher_running_state()
        elif device == "Dryer":
            handle_dryer_running_state()


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


def wait_for_ready_state(device, callback, *, timeout_seconds=APPLIANCE_READY_TIMEOUT_SECONDS,
                         poll_interval=2.0, departure_observed=False):
    """Await a *new* Ready state after abort, then issue one replacement command.

    This runs synchronously inside the appliance's already-background worker.  A
    retained/stale ``Ready`` snapshot must not satisfy the wait: the worker first
    has to observe the appliance leave Ready as acknowledgement that the abort is
    actually in progress.  The bound prevents leaked monitoring threads.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    observed_departure = bool(departure_observed)
    logging.info("Appliance scheduler: waiting for %s abort acknowledgement.", device)
    while time.monotonic() <= deadline:
        try:
            operation_state = retrieve_appliance_state(device).get("OperationState")
            if operation_state != "Ready":
                observed_departure = True
            elif observed_departure:
                global_ready_flags[device] = True
                try:
                    callback()
                except Exception as error:
                    logging.error(
                        "Appliance scheduler: %s replacement command failed: %s",
                        device,
                        error,
                    )
                    return False
                return True
        except Exception as error:
            logging.warning(
                "Appliance scheduler: unable to read %s readiness: %s", device, error)
        time.sleep(max(0.0, float(poll_interval)))

    logging.error(
        "Appliance scheduler: %s did not complete its abort within %.1fs.",
        device,
        float(timeout_seconds),
    )
    return False
