"""Tests for the read-only weather/HVAC forecast validation report.

The validation tool must be deliberately stricter than a one-off spreadsheet:
it only learns from settled, measured, EV-excluded load records and it must not
present a pre-nowcast PV shadow as evidence for changing the live PV branch.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib import forecast_validation as validation


def _load_row(day, slot, *, actual=1.0, baseline=2.0, shadow=1.1, **extra):
    row = {
        "kind": "settlement",
        "slot_start": f"{day}T{slot}:00+02:00",
        "ts": f"{day}T{slot}:15+02:00",
        "incomplete": False,
        "load_meter_quality": "measured",
        "base_load_kwh": actual,
        "baseline_load_forecast_kwh": baseline,
        "weather_load_shadow_kwh": shadow,
    }
    row.update(extra)
    return row


def _matched_pv_row(day, slot, *, actual=1.0, baseline=2.0, shadow=1.1, version="v1"):
    return {
        "kind": "settlement",
        "slot_start": f"{day}T{slot}:00+02:00",
        "ts": f"{day}T{slot}:15+02:00",
        "incomplete": False,
        "actual_pv_kwh": actual,
        "final_baseline_pv_forecast_kwh": baseline,
        "final_weather_pv_shadow_kwh": shadow,
        "pv_forecast_stage": "post_nowcast",
        "pv_nowcast_pipeline_version": version,
    }


def _criteria(**overrides):
    values = {
        "min_complete_days": 2,
        "min_slots_per_day": 2,
        "min_relative_mae_improvement": 0.05,
        "bootstrap_samples": 200,
        "bootstrap_seed": 7,
        "max_bias_degradation_kwh": 0.01,
        "max_abs_shadow_adjustment_kwh": 2.0,
        # Coverage is measured against a real local calendar day even when
        # boundary checking is deliberately disabled for these tiny unit
        # fixtures.  Keep the synthetic fixtures focused on score math;
        # production retains the strict 80% full-day default.
        "min_slot_coverage_ratio": 0.01,
        # Unit fixtures use a few slots to isolate score math. The production
        # command retains the default full-calendar-day requirement.
        "require_calendar_day_boundaries": False,
    }
    values.update(overrides)
    return validation.ValidationCriteria(**values)


def test_load_validation_uses_only_measured_ev_excluded_settlements():
    records = [
        _load_row("2026-08-01", "00:00"),
        _load_row("2026-08-01", "00:15"),
        _load_row("2026-08-02", "00:00"),
        _load_row("2026-08-02", "00:15"),
        _load_row("2026-08-02", "00:30", incomplete=True),
        _load_row("2026-08-02", "00:45", load_meter_quality="implausible_delta"),
        _load_row("2026-08-02", "01:00", actual=None),
        {"kind": "cycle", "ts": "2026-08-02T01:15:00+02:00"},
    ]

    report = validation.analyze_load_shadow(records, criteria=_criteria())

    assert report["eligible_days"] == 2
    assert report["eligible_slots"] == 4
    assert report["baseline"]["mae_kwh"] == 1.0
    assert report["shadow"]["mae_kwh"] == 0.1
    assert report["gate"]["pass"] is True
    assert report["gate"]["recommendation"] == "MANUAL_REVIEW_ONLY"
    assert report["rejected_records"]["incomplete"] == 1
    assert report["rejected_records"]["load_meter_quality:implausible_delta"] == 1
    assert report["rejected_records"]["missing_or_invalid:base_load_kwh"] == 1


def test_load_gate_rejects_small_improvement_and_partial_days():
    records = [
        _load_row("2026-08-01", "00:00", baseline=2.0, shadow=1.96),
        _load_row("2026-08-01", "00:15", baseline=2.0, shadow=1.96),
        _load_row("2026-08-02", "00:00", baseline=2.0, shadow=1.96),
        # This date has only one valid slot, so it cannot be treated as complete.
        _load_row("2026-08-03", "00:00", baseline=2.0, shadow=1.96),
    ]

    report = validation.analyze_load_shadow(records, criteria=_criteria())

    assert report["eligible_days"] == 1
    assert report["gate"]["pass"] is False
    assert report["gate"]["recommendation"] == "KEEP_APPLY_OFF"
    reasons = " ".join(report["gate"]["reasons"])
    assert "complete days" in reasons
    assert "MAE improvement" in reasons


def test_validation_deduplicates_settlement_slots_and_keeps_latest_revision():
    rows = [
        _load_row("2026-08-01", "00:00", baseline=2.0, shadow=1.5,
                  ts="2026-08-01T00:15:00+02:00"),
        # Same settled interval, later correction. It must replace rather than
        # inflate the day sample count or be chosen by filesystem order.
        _load_row("2026-08-01", "00:00", baseline=2.0, shadow=1.0,
                  ts="2026-08-01T00:16:00+02:00"),
        _load_row("2026-08-01", "00:15", baseline=2.0, shadow=1.0),
        _load_row("2026-08-01", "00:30", baseline=2.0, shadow=1.0),
        _load_row("2026-08-01", "00:45", baseline=2.0, shadow=1.0),
    ]

    report = validation.analyze_load_shadow(
        rows,
        criteria=_criteria(min_complete_days=1, min_slots_per_day=4),
    )

    assert report["candidate_records"] == 5
    assert report["duplicate_slot_records"] == 1
    assert report["distinct_candidate_slots"] == 4
    assert report["eligible_days"] == 1
    # The corrected zero-error shadow replaces the earlier 0.5 kWh error.
    assert report["shadow"]["mae_kwh"] == 0.0


def test_validation_rejects_bunched_slots_even_when_raw_row_count_is_high():
    rows = [
        _load_row("2026-08-01", slot, baseline=2.0, shadow=1.0)
        for slot in ("00:00", "00:15", "10:00", "10:15")
    ]

    report = validation.analyze_load_shadow(
        rows,
        criteria=_criteria(
            min_complete_days=1,
            min_slots_per_day=4,
            min_slot_coverage_ratio=0.8,
        ),
    )

    assert report["eligible_days"] == 0
    assert report["dropped_partial_days"] == [{
        "date": "2026-08-01",
        "distinct_slots": 4,
        "span_slots": 42,
        "expected_calendar_slots": 96,
        "coverage_ratio": 0.041667,
        "reason": "insufficient_slot_coverage",
    }]
    assert report["gate"]["recommendation"] == "KEEP_APPLY_OFF"


def test_validation_collapses_bunched_nearby_timestamps_into_one_slot():
    rows = [
        _load_row("2026-08-01", f"00:{minute:02d}", baseline=2.0, shadow=1.0)
        for minute in (1, 2)
    ]

    report = validation.analyze_load_shadow(
        rows,
        criteria=_criteria(min_complete_days=1, min_slots_per_day=4),
    )

    assert report["candidate_records"] == 2
    assert report["duplicate_slot_records"] == 1
    assert report["distinct_candidate_slots"] == 1
    assert report["eligible_slots"] == 0
    assert report["dropped_partial_days"][0]["reason"] == "insufficient_distinct_slots"


def test_production_gate_rejects_contiguous_early_day_capture_without_calendar_boundaries():
    # 80 uninterrupted quarter-hours is 20 hours of evidence, not a completed
    # day. Counting coverage only from first observed slot to last observed slot
    # would incorrectly call this 100% complete.
    rows = [
        _load_row(
            "2026-08-01",
            f"{minute // 60:02d}:{minute % 60:02d}",
            baseline=2.0,
            shadow=1.0,
        )
        for minute in range(0, 20 * 60, 15)
    ]

    report = validation.analyze_load_shadow(
        rows,
        criteria=_criteria(
            min_complete_days=1,
            min_slots_per_day=80,
            min_slot_coverage_ratio=0.8,
            require_calendar_day_boundaries=True,
        ),
    )

    assert report["eligible_days"] == 0
    assert report["dropped_partial_days"] == [{
        "date": "2026-08-01",
        "distinct_slots": 80,
        "span_slots": 80,
        "expected_calendar_slots": 96,
        "coverage_ratio": 0.833333,
        "reason": "missing_calendar_day_boundary",
    }]


def test_gate_uses_unrounded_improvement_at_the_threshold():
    # The human report prints 5.0%, but the true value is just under the 5%
    # gate. Presentation rounding must never activate an evidence review.
    records = [
        _load_row(day, slot, actual=0.0, baseline=1.0, shadow=0.9500004)
        for day in ("2026-08-01", "2026-08-02")
        for slot in ("00:00", "00:15")
    ]
    report = validation.analyze_load_shadow(records, criteria=_criteria())

    assert report["relative_mae_improvement"] == 0.05
    assert report["decision_metrics"]["relative_mae_improvement"] < 0.05
    assert report["gate"]["pass"] is False
    assert any("relative MAE improvement" in reason for reason in report["gate"]["reasons"])


def test_gate_uses_unrounded_bias_degradation_at_the_threshold():
    criteria = _criteria(max_bias_degradation_kwh=0.005)
    presentation_metrics = {
        "eligible_days": 2,
        "relative_mae_improvement": 0.10,
        "improved_days": 2,
        "absolute_bias_degradation_kwh": 0.005,
        "max_abs_shadow_adjustment_kwh": 0.1,
        "day_block_bootstrap": {"ci95_high_kwh": -0.01},
    }
    decision_metrics = {
        **presentation_metrics,
        "absolute_bias_degradation_kwh": 0.0050004,
        "day_block_bootstrap": {"_raw_ci95_high_kwh": -0.01},
    }

    gate = validation._gate(
        presentation_metrics,
        criteria,
        decision_metrics=decision_metrics,
    )

    assert gate["pass"] is False
    assert any("bias degradation" in reason for reason in gate["reasons"])


def test_overall_report_keeps_hvac_and_pv_evidence_gates_independent():
    # A valid HVAC/load result must remain reviewable while the newly added
    # matched-PV instrumentation is still collecting its own sample.
    records = [
        _load_row(day, slot)
        for day in ("2026-08-01", "2026-08-02")
        for slot in ("00:00", "00:15")
    ]

    report = validation.analyze_records(records, criteria=_criteria())

    assert report["load"]["gate"]["pass"] is True
    assert report["pv"]["matched"]["gate"]["pass"] is False
    assert report["overall"]["pass"] is True
    assert report["overall"]["reviewable_branches"] == ["HVAC_LOAD_APPLY"]


def test_record_date_accepts_iso_z_suffix_for_python_310_compatibility():
    assert validation._record_date({"slot_start": "2026-08-01T00:00:00Z"}) == "2026-08-01"


def test_pv_report_marks_raw_shadow_as_diagnostic_until_final_nowcast_fields_exist():
    records = [
        {
            "kind": "settlement",
            "slot_start": f"2026-08-0{day}T00:{minute}:00+02:00",
            "ts": f"2026-08-0{day}T00:{minute}:15+02:00",
            "incomplete": False,
            "actual_pv_kwh": 1.0,
            "baseline_pv_forecast_kwh": 2.0,
            "weather_pv_shadow_kwh": 1.1,
        }
        for day in (1, 2)
        for minute in ("00", "15")
    ]

    report = validation.analyze_pv_shadow(records, criteria=_criteria())

    assert report["matched"]["status"] == "needs_instrumentation"
    assert report["matched"]["gate"]["recommendation"] == "KEEP_APPLY_OFF"
    assert set(report["matched"]["required_fields"]) == {
        "final_baseline_pv_forecast_kwh",
        "final_weather_pv_shadow_kwh",
        "pv_forecast_stage",
        "pv_nowcast_pipeline_version",
    }
    assert report["raw_pre_nowcast_diagnostic"]["status"] == "diagnostic_only"
    assert report["raw_pre_nowcast_diagnostic"]["eligible_slots"] == 4


def test_pv_report_accepts_only_one_explicit_post_nowcast_pipeline_version():
    records = [
        _matched_pv_row("2026-08-01", "00:00"),
        _matched_pv_row("2026-08-01", "00:15"),
        _matched_pv_row("2026-08-02", "00:00"),
        _matched_pv_row("2026-08-02", "00:15"),
    ]

    matched = validation.analyze_pv_shadow(records, criteria=_criteria())["matched"]

    assert matched["status"] == "ready"
    assert matched["pipeline_version"] == "v1"
    assert matched["gate"]["pass"] is True

    records[-1]["pv_nowcast_pipeline_version"] = "v2"
    mixed = validation.analyze_pv_shadow(records, criteria=_criteria())["matched"]
    assert mixed["status"] == "mixed_pipeline_versions"
    assert mixed["gate"]["recommendation"] == "KEEP_APPLY_OFF"


def test_cli_prints_pending_report_without_treating_insufficient_evidence_as_an_error(tmp_path, capsys):
    day = tmp_path / "ess-2026-08-01.ndjson"
    day.write_text(json.dumps(_load_row("2026-08-01", "00:00")) + "\n", encoding="utf-8")

    from scripts.validate_forecasts import main

    assert main(["--dir", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["load"]["gate"]["recommendation"] == "KEEP_APPLY_OFF"


def test_cli_human_output_explains_current_and_weather_trial_forecasts(tmp_path, capsys):
    day = tmp_path / "ess-2026-08-01.ndjson"
    day.write_text(json.dumps(_load_row("2026-08-01", "00:00")) + "\n", encoding="utf-8")

    from scripts.validate_forecasts import main

    assert main(["--dir", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "Current forecast = the forecast used by the optimizer." in output
    assert "Weather/HVAC trial = the comparison forecast; it is not applied." in output
    assert "current forecast MAE=" in output
    assert "weather/HVAC trial MAE=" in output
    assert "baseline MAE=" not in output
    assert "shadow MAE=" not in output


def test_history_reader_fails_closed_when_normal_parquet_scan_would_swallow_error(monkeypatch, tmp_path):
    """Validation must not inherit the live reader's partial-history fallback."""
    from lib import history_store

    # Keep one healthy hot day present: normal ``available_days`` would return
    # this alone after swallowing the damaged cold-month scan, which is exactly
    # the partial evidence set validation must reject.
    (tmp_path / "ess-2026-08-01.ndjson").write_text(
        json.dumps(_load_row("2026-08-01", "00:00")) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "ess-2026-07.parquet").write_bytes(b"not a parquet file")

    class BrokenConnection:
        def execute(self, _query):
            raise RuntimeError("corrupt parquet footer")

        def close(self):
            pass

    monkeypatch.setattr(history_store, "_HAVE_DUCKDB", True)
    monkeypatch.setattr(
        history_store,
        "duckdb",
        SimpleNamespace(connect=lambda: BrokenConnection()),
    )

    # This is intentional production behaviour: the normal app reader keeps
    # working from healthy sources after logging the cold-store failure.
    assert history_store.available_days(tmp_path) == ["2026-08-01"]

    with pytest.raises(validation.HistoryReadError, match="corrupt parquet footer"):
        validation.load_history_records(tmp_path)


def test_history_reader_fails_closed_when_enumerated_parquet_day_cannot_be_read(monkeypatch, tmp_path):
    """A successful day listing is insufficient if the individual read fails."""
    from lib import history_store

    (tmp_path / "ess-2026-07.parquet").write_bytes(b"unreadable payload")

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class ReadFailingConnection:
        def execute(self, query, _params=None):
            if "SELECT DISTINCT day" in query:
                return Result([("2026-07-01",)])
            raise RuntimeError("parquet row read failed")

        def close(self):
            pass

    monkeypatch.setattr(history_store, "_HAVE_DUCKDB", True)
    monkeypatch.setattr(
        history_store,
        "duckdb",
        SimpleNamespace(connect=lambda: ReadFailingConnection()),
    )

    with pytest.raises(validation.HistoryReadError, match="parquet row read failed"):
        validation.load_history_records(tmp_path)
