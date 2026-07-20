"""TDD coverage for season-independent Home Connect appliance scheduling.

These tests deliberately exercise policy separately from MQTT transport.  A manual
second start is the household override, while a start installed by Home Connect's
``DelayedStart`` state must never be mistaken for a new manual intervention.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest


class FakeState:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key, 0)

    def set(self, key, value):
        self.values[key] = value


def _quarter_hour_prices(start, hours, default=0.40, overrides=None):
    """Build contiguous native-price points accepted by the flexible planner."""
    overrides = overrides or {}
    points = []
    for index in range(hours * 4):
        slot_start = start + timedelta(minutes=15 * index)
        points.append({"start": slot_start, "total": overrides.get(slot_start, default)})
    return points


def test_home_connect_master_gate_disables_intervention_but_keeps_lifecycle_tracking(monkeypatch):
    from lib import event_handler

    handled = []
    monkeypatch.setattr(event_handler, "HOME_CONNECT_APPLIANCE_SCHEDULING", False)
    monkeypatch.setattr(
        event_handler,
        "handle_dishwasher_event",
        lambda value, automation_enabled=True: handled.append(
            ("dw", value, automation_enabled)),
    )
    monkeypatch.setattr(
        event_handler,
        "handle_dryer_event",
        lambda value, automation_enabled=True: handled.append(
            ("dryer", value, automation_enabled)),
    )

    event_handler.Event("unused", "payload").dishwasher_state()
    event_handler.Event("unused", "payload").dryer_state()

    assert handled == [
        ("dw", "payload", False),
        ("dryer", "payload", False),
    ]


@pytest.mark.parametrize(
    ("winter_mode", "feature_enabled", "expected"),
    [
        (False, False, False),
        (False, True, True),
        (True, False, False),
        (True, True, True),
    ],
)
def test_price_deferral_is_independent_of_ess_season(
    monkeypatch, winter_mode, feature_enabled, expected
):
    from lib import event_handler_appliances as appliances

    monkeypatch.setattr(appliances, "WINTER_MODE", winter_mode, raising=False)
    monkeypatch.setattr(
        appliances, "APPLIANCE_OPTIMIZATION_ENABLED", feature_enabled, raising=False
    )

    assert appliances.price_deferral_enabled() is expected


def test_dishwasher_favorite_program_is_enforced_for_immediate_and_delayed(monkeypatch):
    from lib import event_handler_appliances as appliances

    published = []
    monkeypatch.setattr(
        appliances,
        "publish_message",
        lambda topic, payload=None, **kwargs: published.append((topic, json.loads(payload))),
    )
    monkeypatch.setattr(appliances, "determine_optimal_run_time", lambda *args, **kwargs: 900)

    appliances.send_immediate_start_to_dishwasher()
    appliances.send_delayed_start_to_dishwasher()

    assert published == [
        (
            "Cerbomoticzgx/homeconnect/dishwasher/activeProgram",
            {"program": 8203, "options": [{"uid": 558, "value": 0}]},
        ),
        (
            "Cerbomoticzgx/homeconnect/dishwasher/activeProgram",
            {"program": 8203, "options": [{"uid": 558, "value": 900}]},
        ),
    ]


def test_silent_dry_is_selected_atomically_with_delayed_start(monkeypatch):
    from lib import event_handler_appliances as appliances

    now = datetime.now().astimezone()
    start = now + timedelta(hours=1)
    end = start.replace(hour=21, minute=30)
    if end <= start:
        end += timedelta(days=1)
    plan = {
        "device": "Dryer",
        "decision": "delayed",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "runtime_minutes": 150,
        "program": 12345,
    }
    published = []
    monkeypatch.setattr(appliances, "_pending_plans", {"Dryer": plan})
    monkeypatch.setattr(
        appliances,
        "publish_message",
        lambda topic, payload=None, **kwargs: published.append(
            (topic, json.loads(payload))),
    )

    appliances.send_delayed_start_to_dryer()

    assert len(published) == 1
    assert published[0][0] == "Cerbomoticzgx/homeconnect/dryer/activeProgram"
    assert published[0][1]["program"] == appliances.SILENT_DRY_PROGRAM
    assert plan["program"] == appliances.SILENT_DRY_PROGRAM


def test_silent_dry_is_replanned_with_conservative_runtime_before_abort(monkeypatch):
    from lib import event_handler_appliances as appliances

    now = datetime.now().astimezone()
    runtimes = []
    state = FakeState({
        "Dryer_SelectedProgram": 12345,
        "Dryer_FinishInRelative": 90 * 60,
    })
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(appliances, "price_deferral_enabled", lambda: True)
    monkeypatch.setattr(appliances, "get_all_price_points", lambda: ["prices"])

    def planner(**kwargs):
        runtimes.append(kwargs["runtime_minutes"])
        runtime = kwargs["runtime_minutes"]
        return {
            "decision": "delayed",
            "start": (now + timedelta(hours=1)).isoformat(),
            "end": (now.replace(hour=21, minute=30) + timedelta(days=1)).isoformat(),
            "runtime_minutes": runtime,
            "load_kw": 0.9,
            "load_profile": [],
        }

    monkeypatch.setattr(appliances, "plan_flexible_load", planner)

    plan = appliances._prepare_appliance_plan("Dryer")

    assert runtimes == [90.0, appliances.DRYER_FALLBACK_RUNTIME_MINUTES]
    assert plan["program"] == appliances.SILENT_DRY_PROGRAM
    assert plan["runtime_source"] == "silent_dry_conservative_fallback"


@pytest.mark.parametrize(
    ("defer_prices", "expected_callback"),
    [(False, "send_immediate_start_to_dishwasher"), (True, "send_delayed_start_to_dishwasher")],
)
def test_dishwasher_uses_deferred_start_only_under_appliance_price_policy(
    monkeypatch, defer_prices, expected_callback
):
    from lib import event_handler_appliances as appliances

    callbacks = []
    monkeypatch.setattr(appliances, "price_deferral_enabled", lambda: defer_prices, raising=False)
    monkeypatch.setattr(appliances, "abort_dishwasher", lambda: None)
    monkeypatch.setattr(appliances, "_remote_start_available", lambda device: True, raising=False)
    monkeypatch.setattr(
        appliances, "_wait_for_operation_state", lambda *args, **kwargs: True, raising=False
    )
    monkeypatch.setattr(appliances, "_set_schedule_status", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(appliances, "_persist_delayed_plan", lambda *args: None, raising=False)
    monkeypatch.setattr(appliances, "_remove_reservation", lambda *args: None, raising=False)
    monkeypatch.setattr(
        appliances,
        "_prepare_appliance_plan",
        lambda device: {
            "decision": "delayed" if defer_prices else "immediate",
            "start": "2026-01-15T12:00:00+01:00",
            "end": "2026-01-15T13:00:00+01:00",
            "load_kw": 1.2,
            "load_profile": [],
        },
        raising=False,
    )
    monkeypatch.setattr(
        appliances,
        "wait_for_ready_state",
        lambda device, callback, **kwargs: callbacks.append(callback.__name__) or True,
    )

    appliances._reschedule_worker("Dishwasher")

    assert callbacks == [expected_callback]


def test_first_manual_start_is_intercepted_but_second_start_is_override(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState()
    monkeypatch.setattr(appliances, "gs_client", state)

    transition = ({"OperationState": "Ready"}, {"OperationState": "Run"})
    assert appliances.handle_user_intervention("Dishwasher", *transition) is False
    assert state.values["Dishwasher_UserInterventionCount"] == 1

    assert appliances.handle_user_intervention("Dishwasher", *transition) is True
    assert state.values["Dishwasher_UserInterventionCount"] == 0


def test_coordinator_immediate_run_is_not_counted_as_second_user_start(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({"Dishwasher_UserInterventionCount": 1})
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(
        appliances,
        "_coordinator_commands",
        {"Dishwasher": {"expected_operation": "Run"}},
        raising=False,
    )

    owned = appliances.handle_user_intervention(
        "Dishwasher",
        {"OperationState": "Ready"},
        {"OperationState": "Run"},
    )

    assert owned is True
    assert state.values["Dishwasher_UserInterventionCount"] == 1


def test_user_cancel_then_second_manual_start_remains_an_override(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({"Dishwasher_UserInterventionCount": 1})
    coordinator_commands = {
        "Dishwasher": {"expected_operation": "Run"},
    }
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(
        appliances, "_coordinator_commands", coordinator_commands, raising=False)
    monkeypatch.setattr(appliances, "_remove_reservation", lambda device: None)

    assert appliances.handle_user_intervention(
        "Dishwasher",
        {"OperationState": "Ready"},
        {"OperationState": "Run"},
    ) is True
    assert coordinator_commands["Dishwasher"]["acknowledged"] is True

    # Once the coordinator command has been acknowledged, a cancellation and
    # another Ready -> Run transition really is the user's second intervention.
    appliances._release_reservation_for_transition(
        "Dishwasher", "Run", "Ready")
    assert appliances.handle_user_intervention(
        "Dishwasher",
        {"OperationState": "Ready"},
        {"OperationState": "Run"},
    ) is True
    assert state.values["Dishwasher_UserInterventionCount"] == 0


def test_operation_wait_remembers_brief_coordinator_acknowledgement(monkeypatch):
    from lib import event_handler_appliances as appliances

    monkeypatch.setattr(
        appliances,
        "_coordinator_commands",
        {
            "Dishwasher": {
                "expected_operation": "Run",
                "acknowledged": True,
            },
        },
        raising=False,
    )
    monkeypatch.setattr(
        appliances,
        "retrieve_appliance_state",
        lambda device: {"OperationState": "Ready"},
    )

    assert appliances._wait_for_operation_state(
        "Dishwasher", "Run", timeout_seconds=0, poll_interval=0) is True


def test_completed_run_resets_manual_override_counter(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({"Dishwasher_UserInterventionCount": 1})
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(appliances, "_remove_reservation", lambda device: None)

    appliances._release_reservation_for_transition(
        "Dishwasher", "Run", "Finished")

    assert state.values["Dishwasher_UserInterventionCount"] == 0


def test_scheduled_delayed_start_is_not_intercepted(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({"Dryer_UserInterventionCount": 1})
    monkeypatch.setattr(appliances, "gs_client", state)

    allow_run = appliances.handle_user_intervention(
        "Dryer",
        {"OperationState": "DelayedStart"},
        {"OperationState": "Run"},
    )

    assert allow_run is False
    assert state.values["Dryer_UserInterventionCount"] == 0


def test_early_delayed_start_is_manual_override_and_clears_reservation(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({
        "Dryer_UserInterventionCount": 1,
        "Dryer_SchedulerDetail": (
            datetime.now().astimezone() + timedelta(hours=2)
        ).isoformat(),
    })
    removed = []
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(appliances, "_remove_reservation", removed.append)

    allow_run = appliances.handle_user_intervention(
        "Dryer",
        {"OperationState": "DelayedStart"},
        {"OperationState": "Run"},
    )

    assert allow_run is True
    assert removed == ["Dryer"]
    assert state.values["Dryer_UserInterventionCount"] == 0
    assert state.values["Dryer_SchedulerStatus"] == "ManualOverride"


@pytest.mark.parametrize(
    ("current_operation", "new_operation"),
    [("DelayedStart", "Ready"), ("Run", "Finished"), ("Run", "Inactive")],
)
def test_cancelled_or_completed_work_releases_optimizer_reservation(
    monkeypatch, current_operation, new_operation
):
    from lib import event_handler_appliances as appliances

    removed = []
    monkeypatch.setattr(appliances, "_remove_reservation", removed.append)

    appliances._release_reservation_for_transition(
        "Dishwasher", current_operation, new_operation)

    assert removed == ["Dishwasher"]


def test_start_of_reserved_work_keeps_load_in_optimizer_forecast(monkeypatch):
    from lib import event_handler_appliances as appliances

    removed = []
    monkeypatch.setattr(appliances, "_remove_reservation", removed.append)

    appliances._release_reservation_for_transition(
        "Dishwasher", "DelayedStart", "Run")

    assert removed == []


def test_ready_waiter_ignores_stale_ready_until_abort_transition_is_observed(monkeypatch):
    from lib import event_handler_appliances as appliances

    states = iter(["Ready", "Aborting", "Ready"])
    callbacks = []
    monkeypatch.setattr(
        appliances,
        "retrieve_appliance_state",
        lambda device: {"OperationState": next(states)},
    )
    monkeypatch.setattr(appliances.time, "sleep", lambda seconds: None)

    assert appliances.wait_for_ready_state(
        "Dishwasher", lambda: callbacks.append("command"), timeout_seconds=5, poll_interval=0
    ) is True
    assert callbacks == ["command"]


def test_ready_waiter_accepts_first_ready_after_pre_abort_run_was_confirmed(monkeypatch):
    from lib import event_handler_appliances as appliances

    callbacks = []
    monkeypatch.setattr(
        appliances,
        "retrieve_appliance_state",
        lambda device: {"OperationState": "Ready"},
    )

    assert appliances.wait_for_ready_state(
        "Dishwasher",
        lambda: callbacks.append("command"),
        timeout_seconds=5,
        poll_interval=0,
        departure_observed=True,
    ) is True
    assert callbacks == ["command"]


def test_ready_waiter_times_out_without_sending_replacement_command(monkeypatch):
    from lib import event_handler_appliances as appliances

    callbacks = []
    clock = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(
        appliances,
        "retrieve_appliance_state",
        lambda device: {"OperationState": "Run"},
    )
    monkeypatch.setattr(appliances.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(appliances.time, "sleep", lambda seconds: None)

    assert appliances.wait_for_ready_state(
        "Dryer", lambda: callbacks.append("command"), timeout_seconds=1, poll_interval=0
    ) is False
    assert callbacks == []


def test_replacement_command_failure_returns_false_instead_of_looping(monkeypatch):
    from lib import event_handler_appliances as appliances

    states = iter(["Aborting", "Ready"])
    attempts = []
    monkeypatch.setattr(
        appliances,
        "retrieve_appliance_state",
        lambda device: {"OperationState": next(states)},
    )
    monkeypatch.setattr(appliances.time, "sleep", lambda seconds: None)

    def failed_command():
        attempts.append("command")
        raise RuntimeError("Home Connect bridge offline")

    assert appliances.wait_for_ready_state(
        "Dryer", failed_command, timeout_seconds=5, poll_interval=0
    ) is False
    assert attempts == ["command"]


def test_operation_ack_accepts_run_when_selected_program_telemetry_is_stale(monkeypatch):
    from lib import event_handler_appliances as appliances

    monkeypatch.setattr(
        appliances,
        "retrieve_appliance_state",
        lambda device: {"OperationState": "Run", "SelectedProgram": 8196},
    )

    assert appliances._wait_for_operation_state(
        "Dishwasher",
        "Run",
        expected_program=appliances.PREFERRED_DISHWASHER_PROGRAM,
        timeout_seconds=5,
        poll_interval=0,
    ) is True


def test_ready_command_failure_attempts_one_immediate_fallback(monkeypatch):
    """Once abort succeeded, a failed replacement must not silently strand Ready."""
    from lib import event_handler_appliances as appliances

    immediate_attempts = []
    statuses = []
    plan = {
        "device": "Dishwasher",
        "decision": "delayed",
        "start": "2026-01-15T12:00:00+01:00",
        "end": "2026-01-15T13:00:00+01:00",
        "load_kw": 1.2,
        "load_profile": [],
    }
    monkeypatch.setattr(appliances, "_pending_plans", {})
    monkeypatch.setattr(appliances, "_prepare_appliance_plan", lambda device: plan)
    monkeypatch.setattr(appliances, "_remote_start_available", lambda device: True)
    monkeypatch.setattr(appliances, "abort_dishwasher", lambda: None)
    # False with an observed Ready state models a callback/publish failure after
    # the abort completed, rather than an appliance still stuck in Aborting.
    monkeypatch.setattr(appliances, "wait_for_ready_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        appliances, "retrieve_appliance_state", lambda device: {"OperationState": "Ready"}
    )
    monkeypatch.setattr(
        appliances,
        "send_immediate_start_to_dishwasher",
        lambda: immediate_attempts.append("immediate"),
    )
    monkeypatch.setattr(appliances, "_remove_reservation", lambda device: None)
    monkeypatch.setattr(
        appliances, "_wait_for_operation_state", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        appliances,
        "_set_schedule_status",
        lambda device, status, detail=None: statuses.append(status),
    )

    appliances._reschedule_worker("Dishwasher")

    assert immediate_attempts == ["immediate"]
    assert appliances._pending_plans == {}
    assert statuses[-1] == "ImmediateFallback"


def test_immediate_fallback_without_run_ack_is_reported_failed(monkeypatch):
    from lib import event_handler_appliances as appliances

    statuses = []
    monkeypatch.setattr(
        appliances, "retrieve_appliance_state", lambda device: {"OperationState": "Ready"})
    monkeypatch.setattr(appliances, "send_immediate_start_to_dishwasher", lambda: None)
    monkeypatch.setattr(appliances, "_remove_reservation", lambda device: None)
    monkeypatch.setattr(
        appliances, "_wait_for_operation_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        appliances,
        "_set_schedule_status",
        lambda device, status, detail=None: statuses.append(status),
    )

    assert appliances._attempt_immediate_fallback("Dishwasher", {}) is False
    assert statuses[-1] == "FallbackFailed"


@pytest.mark.parametrize("failure", ["plan", "remote"])
def test_worker_never_aborts_without_plan_and_remote_start_validation(monkeypatch, failure):
    from lib import event_handler_appliances as appliances

    aborts = []
    plan = {
        "device": "Dishwasher",
        "decision": "delayed",
        "start": "2026-01-15T12:00:00+01:00",
        "end": "2026-01-15T13:00:00+01:00",
        "load_kw": 1.2,
        "load_profile": [],
    }
    monkeypatch.setattr(
        appliances, "_prepare_appliance_plan", lambda device: None if failure == "plan" else plan
    )
    monkeypatch.setattr(
        appliances, "_remote_start_available", lambda device: failure != "remote"
    )
    monkeypatch.setattr(appliances, "abort_dishwasher", lambda: aborts.append("abort"))
    monkeypatch.setattr(appliances, "_set_schedule_status", lambda *args, **kwargs: None)

    appliances._reschedule_worker("Dishwasher")

    assert aborts == []


def test_malformed_selected_program_is_fail_safe(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({
        "Dishwasher_SelectedProgram": "unknown",
        "Dryer_SelectedProgram": "unknown",
    })
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(appliances, "price_deferral_enabled", lambda: False)

    dishwasher = appliances._prepare_appliance_plan("Dishwasher")
    dryer = appliances._prepare_appliance_plan("Dryer")

    assert dishwasher["decision"] == "immediate"
    assert dishwasher["program"] == appliances.PREFERRED_DISHWASHER_PROGRAM
    assert dryer is None


def test_correct_dishwasher_program_with_immediate_plan_is_left_running(monkeypatch):
    from lib import event_handler_appliances as appliances

    aborts = []
    monkeypatch.setattr(
        appliances,
        "_prepare_appliance_plan",
        lambda device: {
            "device": device,
            "decision": "immediate",
            "program": appliances.PREFERRED_DISHWASHER_PROGRAM,
        },
    )
    monkeypatch.setattr(appliances, "_program_id", lambda device: 8203)
    monkeypatch.setattr(appliances, "abort_dishwasher", lambda: aborts.append("abort"))
    monkeypatch.setattr(appliances, "_remove_reservation", lambda device: None)
    monkeypatch.setattr(appliances, "_set_schedule_status", lambda *args, **kwargs: None)

    appliances._reschedule_worker("Dishwasher")

    assert aborts == []


def test_event_snapshot_is_committed_before_worker_reads_new_metadata(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({"Dishwasher_OperationState": "Ready"})
    observed = []
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(appliances, "_remove_reservation", lambda device: None)
    monkeypatch.setattr(
        appliances,
        "_start_reschedule_worker",
        lambda device: observed.append(appliances.retrieve_appliance_state(device)),
    )

    appliances.handle_dishwasher_event({
        "OperationState": "Run",
        "SelectedProgram": 8203,
        "RemainingProgramTime": 3600,
        "RemoteControlStartAllowed": True,
        "RemoteControlActive": True,
        "DoorState": "Closed",
    })

    assert observed[0]["OperationState"] == "Run"
    assert observed[0]["SelectedProgram"] == 8203
    assert observed[0]["RemainingProgramTime"] == 3600
    assert observed[0]["RemoteControlStartAllowed"] is True


def test_coordinator_generated_run_is_not_rescheduled(monkeypatch):
    from lib import event_handler_appliances as appliances

    state = FakeState({
        "Dishwasher_OperationState": "Ready",
        "Dishwasher_SelectedProgram": 8196,
        "Dishwasher_UserInterventionCount": 1,
    })
    workers = []
    monkeypatch.setattr(appliances, "gs_client", state)
    monkeypatch.setattr(
        appliances,
        "_coordinator_commands",
        {"Dishwasher": {"expected_operation": "Run"}},
        raising=False,
    )
    monkeypatch.setattr(appliances, "_remove_reservation", lambda device: None)
    monkeypatch.setattr(
        appliances, "_start_reschedule_worker", lambda device: workers.append(device))

    appliances.handle_dishwasher_event({
        "OperationState": "Run",
        # This is the exact stale field observed on the live appliance after the
        # activeProgram command requested programme 8203.
        "SelectedProgram": 8196,
    })

    assert workers == []
    assert state.values["Dishwasher_UserInterventionCount"] == 1


def test_repeated_dishwasher_event_does_not_abort_before_plan_or_spawn_duplicate_worker(monkeypatch):
    from lib import event_handler_appliances as appliances

    threads = []
    aborts = []

    class PendingThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs["target"]
            threads.append(self)

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(appliances.threading, "Thread", PendingThread)
    monkeypatch.setattr(appliances, "abort_dishwasher", lambda: aborts.append("abort"))
    monkeypatch.setattr(appliances, "price_deferral_enabled", lambda: True)
    monkeypatch.setattr(appliances, "_remote_start_available", lambda device: True, raising=False)

    appliances.handle_dishwasher_running_state()
    appliances.handle_dishwasher_running_state()

    # Planning and remote-start validation happen inside the worker.  Merely
    # queueing it must not stop an appliance before a replacement is known.
    assert aborts == []
    assert len(threads) == 1


def test_negative_price_slots_are_valid_flexible_load_candidates():
    from lib.flexible_load_planner import plan_flexible_load

    now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    negative_start = now + timedelta(hours=2)
    negative_slots = {
        negative_start + timedelta(minutes=15 * index): -0.10 for index in range(4)
    }
    plan = plan_flexible_load(
        device="dishwasher",
        earliest_start=now,
        runtime_minutes=60,
        power_w=1200,
        price_points=_quarter_hour_prices(now, 6, overrides=negative_slots),
        min_savings_eur=0,
    )

    assert plan["decision"] == "delayed"
    assert plan["start"] == negative_start.isoformat()
    assert plan["estimated_cost_eur"] < 0


def test_daytime_deferral_never_exceeds_five_hour_comfort_window():
    from lib.flexible_load_planner import plan_flexible_load

    now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    latest_allowed = now + timedelta(hours=5)
    too_late = now + timedelta(hours=6)
    overrides = {
        latest_allowed + timedelta(minutes=15 * index): 0.20 for index in range(4)
    }
    overrides.update(
        {too_late + timedelta(minutes=15 * index): -0.20 for index in range(4)}
    )

    plan = plan_flexible_load(
        device="dishwasher",
        earliest_start=now,
        runtime_minutes=60,
        power_w=1200,
        price_points=_quarter_hour_prices(now, 8, overrides=overrides),
        min_savings_eur=0,
    )

    assert plan["decision"] == "delayed"
    assert plan["start"] == latest_allowed.isoformat()


def test_evening_plan_must_finish_by_next_morning_0530():
    from lib.flexible_load_planner import plan_flexible_load

    now = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
    valid_start = datetime(2026, 1, 16, 3, 30, tzinfo=timezone.utc)
    invalid_cheaper_start = datetime(2026, 1, 16, 4, 0, tzinfo=timezone.utc)
    overrides = {
        valid_start + timedelta(minutes=15 * index): 0.10 for index in range(8)
    }
    overrides.update(
        {invalid_cheaper_start + timedelta(minutes=15 * index): -0.20 for index in range(6)}
    )

    plan = plan_flexible_load(
        device="dryer",
        earliest_start=now,
        runtime_minutes=120,
        power_w=2500,
        price_points=_quarter_hour_prices(now, 11, overrides=overrides),
        min_savings_eur=0,
    )

    assert plan["decision"] == "delayed"
    assert plan["start"] == valid_start.isoformat()
    assert datetime.fromisoformat(plan["end"]) <= datetime(
        2026, 1, 16, 5, 30, tzinfo=timezone.utc
    )
