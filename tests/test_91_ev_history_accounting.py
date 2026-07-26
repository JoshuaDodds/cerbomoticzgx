"""EV history accounting contracts.

Measured charger energy is authoritative.  Grid energy/cost is an explicitly
labelled proportional attribution because the site meter cannot identify which
simultaneous load consumed each imported electron.
"""

from datetime import datetime, timedelta
import json

import pytest

from lib import history_store
from lib.ev_history import (
    attribute_ev_grid_cost,
    measured_ev_sessions,
    record_ev_power_observation,
    summarize_ev_day,
)


def test_ev_grid_cost_is_proportional_to_ev_share_of_site_load():
    result = attribute_ev_grid_cost(
        ev_charge_kwh=2.0,
        site_load_kwh=4.0,
        site_import_kwh=3.0,
        site_import_cost_eur=0.60,
    )

    assert result == {
        "ev_grid_import_kwh": 1.5,
        "ev_non_grid_kwh": 0.5,
        "ev_grid_cost_eur": 0.3,
        "ev_cost_quality": "proportional_site_load",
    }


def test_ev_grid_cost_records_zero_cost_when_site_did_not_import():
    result = attribute_ev_grid_cost(
        ev_charge_kwh=2.0,
        site_load_kwh=2.5,
        site_import_kwh=0.0,
        site_import_cost_eur=0.0,
    )

    assert result["ev_grid_import_kwh"] == 0.0
    assert result["ev_non_grid_kwh"] == 2.0
    assert result["ev_grid_cost_eur"] == 0.0
    assert result["ev_cost_quality"] == "no_grid_import"


def test_ev_day_summary_prefers_durable_daily_meter_and_reports_partial_cost():
    records = [
        {"kind": "cycle", "ev_actual_today_kwh": 3.0},
        {
            "kind": "settlement",
            "slot_start": "2026-07-25T10:00:00+02:00",
            "slot_end": "2026-07-25T10:15:00+02:00",
            "ev_charge_kwh": 1.5,
            "ev_meter_quality": "measured",
            "ev_grid_import_kwh": 1.0,
            "ev_non_grid_kwh": 0.5,
            "ev_grid_cost_eur": 0.2,
            "ev_cost_quality": "proportional_site_load",
        },
        {
            "kind": "settlement",
            "slot_start": "2026-07-25T10:15:00+02:00",
            "slot_end": "2026-07-25T10:30:00+02:00",
            "ev_charge_kwh": 1.5,
            "ev_meter_quality": "measured",
            "ev_grid_import_kwh": 0.5,
            "ev_non_grid_kwh": 1.0,
            "ev_grid_cost_eur": 0.1,
            "ev_cost_quality": "proportional_site_load",
        },
        {"kind": "cycle", "ev_actual_today_kwh": 7.5},
        {
            "kind": "settlement",
            "slot_start": "2026-07-25T12:00:00+02:00",
            "slot_end": "2026-07-25T15:00:00+02:00",
            "ev_charge_kwh": None,
            "ev_meter_quality": "incomplete_interval",
        },
    ]

    summary = summarize_ev_day(records)

    assert summary["ev_charge_kwh"] == 7.5
    assert summary["ev_energy_source"] == "daily_meter"
    assert summary["ev_grid_import_kwh_attributed"] == 1.5
    assert summary["ev_non_grid_kwh_attributed"] == 1.5
    assert summary["ev_grid_cost_eur_attributed"] == 0.3
    assert summary["ev_sessions"] == 1
    assert summary["ev_history_quality"] == "partial"


def test_ev_power_observations_persist_only_measured_start_and_stop(tmp_path):
    start = datetime.fromisoformat("2026-07-25T11:53:00+02:00")

    # Establish an idle baseline, then cross the start/stop hysteresis. Repeated
    # active readings must not create duplicate session boundaries.
    assert record_ev_power_observation(
        4.0, now=start - timedelta(minutes=1), history_dir=str(tmp_path)
    ) is None
    started = record_ev_power_observation(
        3540.0, now=start, history_dir=str(tmp_path)
    )
    assert record_ev_power_observation(
        3490.0, now=start + timedelta(minutes=10), history_dir=str(tmp_path)
    ) is None
    stopped = record_ev_power_observation(
        4.0, now=start + timedelta(minutes=41), history_dir=str(tmp_path)
    )

    assert started["event"] == "started"
    assert stopped["event"] == "stopped"
    records = history_store.read_day(start.date(), str(tmp_path))
    transitions = [
        record for record in records
        if record.get("kind") == "ev_charge_transition"
    ]
    assert [record["event"] for record in transitions] == ["started", "stopped"]
    assert all(record["source"] == "abb_meter" for record in transitions)

    sessions = measured_ev_sessions(records)
    assert sessions == [{
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=41)).isoformat(),
        "timing_quality": "meter_transition",
    }]


def test_first_active_observation_is_labelled_as_boundary_not_exact_start(tmp_path):
    observed = datetime.fromisoformat("2026-07-25T12:00:00+02:00")

    transition = record_ev_power_observation(
        3500.0, now=observed, history_dir=str(tmp_path)
    )

    assert transition["event"] == "started"
    assert transition["timing_quality"] == "first_active_observation"


def test_first_idle_observation_after_restart_is_not_claimed_as_exact_stop(
        tmp_path):
    started_at = datetime.fromisoformat("2026-07-25T12:00:00+02:00")
    record_ev_power_observation(
        3500.0, now=started_at, history_dir=str(tmp_path)
    )
    state_path = tmp_path / ".ev-power-state.json"
    state = json.loads(state_path.read_text())
    state["writer_token"] = "prior-process"
    state_path.write_text(json.dumps(state))

    stopped = record_ev_power_observation(
        4.0, now=started_at + timedelta(minutes=30), history_dir=str(tmp_path)
    )

    assert stopped["event"] == "stopped"
    assert stopped["timing_quality"] == "first_idle_observation"
    sessions = measured_ev_sessions(
        history_store.read_day(started_at.date(), str(tmp_path))
    )
    assert sessions[0]["timing_quality"] == "first_idle_observation"


def test_failed_transition_append_does_not_advance_durable_state(
        monkeypatch, tmp_path):
    observed = datetime.fromisoformat("2026-07-25T12:00:00+02:00")

    def fail_append(*args, **kwargs):
        raise OSError("history volume unavailable")

    monkeypatch.setattr(history_store, "append", fail_append)
    with pytest.raises(OSError, match="history volume unavailable"):
        record_ev_power_observation(
            3500.0, now=observed, history_dir=str(tmp_path)
        )

    assert not (tmp_path / ".ev-power-state.json").exists()
