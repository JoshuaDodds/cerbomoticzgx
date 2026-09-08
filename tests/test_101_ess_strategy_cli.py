"""Tests for the strictly read-only ESS strategy comparison command."""
from __future__ import annotations

import json

import pytest

from scripts.evaluate_ess_strategies import main


def _plan(**overrides):
    plan = {
        "battery_soc": 60.0,
        "slot_duration_h": 1.0,
        "optimizer_guardrails": {
            "battery_cycle_cost": 0.03,
            "arbitrage_margin": 0.02,
        },
        "strategy_candidate_config": {
            "battery_capacity_kwh": 10.0,
            "min_soc_percent": 20.0,
            "protected_soc_percent": 60.0,
            "charge_efficiency": 0.90,
            "discharge_efficiency": 0.90,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "max_import_kw": 10.0,
            "max_export_kw": 10.0,
            "soc_step_percent": 10.0,
            "grid_charge_soc_cap_percent": 100.0,
            "export_price_factor": 1.0,
            "export_fee_eur_per_kwh": 0.0,
            "terminal_value_eur_per_dc_kwh": 0.0,
        },
        "schedule": [
            {
                "time": "2030-06-01T00:00:00+00:00",
                "price": 0.10,
                "sell": 0.10,
                "load": 0.0,
                "pv": 0.0,
            },
            {
                "time": "2030-06-01T01:00:00+00:00",
                "price": 0.50,
                "sell": 0.50,
                "load": 0.0,
                "pv": 0.0,
            },
        ],
    }
    plan.update(overrides)
    return plan


def _write_plan(tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def test_cli_evaluates_metadata_backed_plan_as_read_only_json(tmp_path, capsys):
    path = _write_plan(tmp_path, _plan())

    assert main(["--plan", str(path), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True
    assert report["plan_path"] == str(path)
    assert report["slot_count"] == 2
    assert report["initial_soc_percent"] == 60.0
    assert report["assumptions"]["defaulted_fields"] == []
    assert report["candidates"]["market_arbitrage"]["feasible"] is True
    assert len(report["candidates"]["market_arbitrage"]["schedule"]) == 2
    assert report["candidates"]["pv_first_self_sufficiency"]["grid_import_kwh"] == 0.0


def test_cli_human_output_uses_dashboard_energy_terms(tmp_path, capsys):
    path = _write_plan(tmp_path, _plan())

    assert main(["--plan", str(path)]) == 0

    output = capsys.readouterr().out
    assert "Net grid result = export reward − import cost." in output
    assert "Grid result" in output
    assert "After battery wear" in output
    assert "cash " not in output
    assert "economic " not in output


def test_cli_requires_explicit_assumptions_or_research_defaults(tmp_path, capsys):
    plan = _plan()
    plan.pop("strategy_candidate_config")
    path = _write_plan(tmp_path, plan)

    assert main(["--plan", str(path), "--json"]) == 2
    error = capsys.readouterr().err
    assert "Missing physical/economic assumptions" in error
    assert "--use-research-defaults" in error

    assert main(["--plan", str(path), "--use-research-defaults", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "battery_capacity_kwh" in report["assumptions"]["defaulted_fields"]
    assert report["assumptions"]["uses_research_defaults"] is True


def test_cli_flags_override_plan_metadata_without_reading_live_settings(tmp_path, capsys):
    path = _write_plan(tmp_path, _plan())

    assert main([
        "--plan", str(path), "--protected-soc-percent", "80", "--json",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["assumptions"]["values"]["protected_soc_percent"] == 80.0
    assert report["candidates"]["protected_hybrid"]["protected_soc_percent"] == 80.0


def test_cli_accepts_a_self_contained_assumptions_file(tmp_path, capsys):
    plan = _plan()
    config = plan.pop("strategy_candidate_config")
    config["cycle_cost_eur_per_dc_kwh"] = 0.04
    path = _write_plan(tmp_path, plan)
    config_path = tmp_path / "assumptions.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert main(["--plan", str(path), "--config", str(config_path), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["assumptions"]["sources"]["battery_capacity_kwh"] == "--config"
    assert report["assumptions"]["sources"]["cycle_cost_eur_per_dc_kwh"] == "--config"


def test_cli_rejects_plan_rows_without_required_energy_fields(tmp_path, capsys):
    plan = _plan()
    plan["schedule"][0].pop("load")
    path = _write_plan(tmp_path, plan)

    assert main(["--plan", str(path), "--json"]) == 2
    assert "schedule[0] is missing load energy" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Whole-day comparison basis
#
# The dashboard's today tile spans 00:00-23:59, so a candidate reported over the
# full 32h plan horizon was never comparable with it.  These cover the corrected
# basis: the already-settled part of today plus each policy's planned remainder.
# --------------------------------------------------------------------------- #

def _crossing_plan(**overrides):
    """A plan whose horizon crosses local midnight, with live-plan flows.

    Slot geometry (10 kWh battery, 0.90 efficiencies): a 20-point discharge is
    2.0 kWh DC -> 1.8 kWh exported; a 20-point charge is 2.0 kWh DC -> 2.222 kWh
    imported.
    """
    plan = {
        "battery_soc": 60.0,
        "slot_duration_h": 1.0,
        "today_actuals": {
            "imp_kwh": 4.0, "imp_cost": 1.00,
            "exp_kwh": 10.0, "exp_rev": 2.50,
        },
        "strategy_candidate_config": {
            "battery_capacity_kwh": 10.0,
            "min_soc_percent": 20.0,
            "protected_soc_percent": 60.0,
            "charge_efficiency": 0.90,
            "discharge_efficiency": 0.90,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "max_import_kw": 10.0,
            "max_export_kw": 10.0,
            "soc_step_percent": 10.0,
            "grid_charge_soc_cap_percent": 100.0,
            "cycle_cost_eur_per_dc_kwh": 0.03,
            "arbitrage_margin_eur_per_dc_kwh": 0.0,
            "export_price_factor": 1.0,
            "export_fee_eur_per_kwh": 0.0,
            "terminal_value_eur_per_dc_kwh": 0.20,
        },
        "schedule": [
            {"time": "2030-06-01T22:00:00+02:00", "price": 0.50, "sell": 0.50,
             "load": 0.0, "pv": 0.0, "grid_energy": -1.8,
             "soc_start": 60.0, "soc_end": 40.0},
            {"time": "2030-06-01T23:00:00+02:00", "price": 0.40, "sell": 0.40,
             "load": 0.0, "pv": 0.0, "grid_energy": 0.0,
             "soc_start": 40.0, "soc_end": 40.0},
            {"time": "2030-06-02T00:00:00+02:00", "price": 0.10, "sell": 0.10,
             "load": 0.0, "pv": 0.0, "grid_energy": 2.2222,
             "soc_start": 40.0, "soc_end": 60.0},
            {"time": "2030-06-02T01:00:00+02:00", "price": 0.10, "sell": 0.10,
             "load": 0.0, "pv": 0.0, "grid_energy": 0.0,
             "soc_start": 60.0, "soc_end": 60.0},
        ],
    }
    plan.update(overrides)
    return plan


def _report(tmp_path, capsys, plan, *args):
    path = _write_plan(tmp_path, plan)
    assert main(["--plan", str(path), "--json", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_today_window_stops_at_local_midnight_not_at_the_end_of_the_horizon(tmp_path, capsys):
    report = _report(tmp_path, capsys, _crossing_plan())

    assert report["schema_version"] == 2
    assert report["slot_count"] == 4
    for candidate in report["candidates"].values():
        today = candidate["today"]
        assert today["slot_count"] == 2
        assert today["window_start"] == "2030-06-01T22:00:00+02:00"
        assert today["window_end"] == "2030-06-02T00:00:00+02:00"


def test_candidates_still_plan_over_the_whole_horizon_while_reporting_today(tmp_path, capsys):
    # Truncating the horizon would drop the terminal value of retained energy and
    # let every candidate empty the battery by midnight for free. The full plan
    # must stay intact behind the today window.
    report = _report(tmp_path, capsys, _crossing_plan())
    market = report["candidates"]["market_arbitrage"]

    # The evaluated plan still spans both days...
    assert len(market["schedule"]) == 4
    assert market["schedule"][-1]["start"].startswith("2030-06-02")
    # ...while only today is reported.
    assert market["today"]["slot_count"] == 2
    # Today's window is a genuine slice, not a relabelled horizon total: it sells
    # into tonight's 0.50 peak (+1.80) while the horizon nets less because
    # tomorrow's cheap slot buys the energy back.
    assert market["today"]["cash_net_eur"] == pytest.approx(1.80)
    assert market["cash_net_eur"] == pytest.approx(0.9111, abs=1e-4)
    assert market["today"]["cash_net_eur"] != pytest.approx(market["cash_net_eur"])


def test_whole_day_total_is_the_settled_part_of_today_plus_the_planned_remainder(tmp_path, capsys):
    report = _report(tmp_path, capsys, _crossing_plan())

    settled = report["settled_today"]
    assert settled["cash_net_eur"] == pytest.approx(1.50)  # 2.50 reward - 1.00 cost

    for candidate in report["candidates"].values():
        today = candidate["today"]
        assert today["whole_day_cash_net_eur"] == pytest.approx(
            settled["cash_net_eur"] + today["cash_net_eur"]
        )
        assert today["whole_day_economic_net_eur"] == pytest.approx(
            settled["cash_net_eur"] + today["economic_net_eur"]
        )


def test_live_plan_baseline_is_totalled_with_the_same_arithmetic_as_the_candidates(tmp_path, capsys):
    report = _report(tmp_path, capsys, _crossing_plan())

    baseline = report["plan_baseline"]
    assert baseline["available"] is True
    assert baseline["unavailable_reason"] is None

    today = baseline["today"]
    assert today["slot_count"] == 2
    # Only the 22:00 slot moves energy: 1.8 kWh exported at 0.50.
    assert today["export_reward_eur"] == pytest.approx(0.90)
    assert today["import_cost_eur"] == pytest.approx(0.0)
    assert today["cash_net_eur"] == pytest.approx(0.90)
    # 2.0 kWh DC discharged at the plan's own 0.03/kWh cycle cost.
    assert today["lifecycle_cost_eur"] == pytest.approx(0.06)
    assert today["economic_net_eur"] == pytest.approx(0.84)
    assert today["whole_day_cash_net_eur"] == pytest.approx(2.40)
    assert today["whole_day_economic_net_eur"] == pytest.approx(2.34)
    assert today["closing_soc_percent"] == pytest.approx(40.0)


def test_carried_energy_discloses_what_a_today_total_borrows_from_tomorrow(tmp_path, capsys):
    # A today-only total credits a policy for selling stored energy without
    # debiting the emptier battery it hands over at midnight.
    report = _report(tmp_path, capsys, _crossing_plan())

    baseline_today = report["plan_baseline"]["today"]
    # Closing 40% against the live optimizer's own 20% floor on a 10 kWh pack.
    assert baseline_today["carried_energy_kwh"] == pytest.approx(2.0)
    assert baseline_today["carried_energy_value_eur"] == pytest.approx(0.40)

    for candidate in report["candidates"].values():
        today = candidate["today"]
        assert today["carried_energy_kwh"] is not None
        assert today["carried_energy_value_eur"] == pytest.approx(
            today["carried_energy_kwh"] * 0.20
        )


def test_carried_energy_is_absent_when_the_horizon_never_reaches_tomorrow(tmp_path, capsys):
    plan = _crossing_plan()
    plan["schedule"] = plan["schedule"][:2]          # today only
    report = _report(tmp_path, capsys, plan)

    for candidate in report["candidates"].values():
        assert candidate["today"]["slot_count"] == 2
        assert candidate["today"]["carried_energy_kwh"] is None
        assert candidate["today"]["carried_energy_value_eur"] is None


def test_whole_day_total_is_withheld_when_the_plan_has_no_settled_totals(tmp_path, capsys):
    plan = _crossing_plan()
    plan.pop("today_actuals")
    report = _report(tmp_path, capsys, plan)

    assert report["settled_today"] is None
    for candidate in report["candidates"].values():
        assert candidate["today"]["whole_day_cash_net_eur"] is None
        assert candidate["today"]["whole_day_economic_net_eur"] is None
        # The planned remainder is still reported rather than suppressed.
        assert candidate["today"]["slot_count"] == 2


def test_baseline_is_withheld_rather_than_invented_when_plan_rows_lack_flows(tmp_path, capsys):
    plan = _crossing_plan()
    for row in plan["schedule"]:
        row.pop("grid_energy")
    report = _report(tmp_path, capsys, plan)

    baseline = report["plan_baseline"]
    assert baseline["available"] is False
    assert baseline["today"] is None
    assert "grid_energy" in baseline["unavailable_reason"]
    # Candidates remain fully evaluated; only the comparison row is missing.
    assert report["candidates"]["market_arbitrage"]["feasible"] is True


def test_live_row_ties_the_dashboard_today_tile_on_a_pv_surplus_morning(
        tmp_path, capsys, monkeypatch):
    """The live row must equal what the dashboard will actually settle.

    Regression for a real divergence: the evaluator credited PV surplus as
    exported during IDLE slots at 4% SoC, where the neutral setpoint stores it
    in the battery instead. The panel read EUR 1.52 above the Today tile while
    claiming to share its basis. Comparing against the dashboard's own
    calculation is the only check that catches a drift between the two.
    """
    from datetime import datetime as _datetime

    from frontend import data

    class _PlanDay(_datetime):
        """Make the dashboard treat the fixture's date as today."""

        @classmethod
        def now(cls, tz=None):
            return cls(2030, 6, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(data, "datetime", _PlanDay)

    plan = _crossing_plan()
    plan["schedule"] = [
        # Neutral-setpoint slots with PV surplus and an almost empty battery:
        # the hardware stores this, so neither surface may book it as revenue.
        {"time": "2030-06-01T10:00:00+02:00", "price": 0.30, "sell": 0.30,
         "load": 0.0, "pv": 1.0, "grid_energy": -1.0, "control_action": "IDLE",
         "soc_start": 4.0, "soc_end": 4.0},
        {"time": "2030-06-01T11:00:00+02:00", "price": 0.30, "sell": 0.30,
         "load": 0.0, "pv": 1.0, "grid_energy": -1.0, "control_action": "IDLE",
         "soc_start": 4.0, "soc_end": 4.0},
        # A commanded discharge to grid, which both surfaces must credit.
        {"time": "2030-06-01T19:00:00+02:00", "price": 0.60, "sell": 0.60,
         "load": 0.0, "pv": 0.0, "grid_energy": -1.8, "control_action": "SELL",
         "soc_start": 40.0, "soc_end": 20.0},
    ]
    report = _report(tmp_path, capsys, plan)

    summary = data.day_summary(
        plan["schedule"], plan["today_actuals"], slot_duration_h=1.0)
    tile_grid_result = -next(
        row["net"] for row in summary["days"] if row["is_today"])

    assert report["plan_baseline"]["today"]["whole_day_cash_net_eur"] == pytest.approx(
        tile_grid_result, abs=0.01)
    # Only the evening discharge is credited: 1.8 kWh at 0.60.
    assert report["plan_baseline"]["today"]["export_reward_eur"] == pytest.approx(1.08)
    assert report["plan_baseline"]["today"]["stored_surplus_kwh"] == pytest.approx(2.0)


def test_winter_row_is_absent_for_a_plan_published_without_a_winter_reserve(tmp_path, capsys):
    report = _report(tmp_path, capsys, _crossing_plan())

    assert report["winter_candidate"] is None
    assert "winter_self_sufficiency" not in report["candidates"]


def test_winter_row_uses_the_plans_winter_reserve_and_ships_its_caveats(tmp_path, capsys):
    # The reserve is a sibling of strategy_candidate_config, never a member:
    # that mapping rejects unknown fields, so adding a key inside it would make
    # every plan this build publishes unreadable.
    plan = _crossing_plan(winter_reserve_soc_percent=80.0)
    report = _report(tmp_path, capsys, plan)

    winter = report["winter_candidate"]
    assert winter["reserve_soc_percent"] == pytest.approx(80.0)
    assert winter["is_approximation"] is True
    assert winter["engine_module"] == "lib.ai_powered_ess_winter"
    assert len(winter["caveats"]) >= 3

    candidate = report["candidates"]["winter_self_sufficiency"]
    assert candidate["protected_soc_percent"] == pytest.approx(80.0)
    assert candidate["grid_export_kwh"] == pytest.approx(0.0)
    assert candidate["today"]["slot_count"] == 2


def test_winter_row_reports_the_floor_it_actually_held_not_the_one_requested(tmp_path, capsys):
    # The evaluator raises a reserve that sits below the configured physical
    # minimum. Reporting the requested value would put a number on screen the
    # row was never held to — precisely the mislabelling this panel exists to
    # avoid. Config minimum here is 20%.
    plan = _crossing_plan(winter_reserve_soc_percent=10.0)
    report = _report(tmp_path, capsys, plan)

    winter = report["winter_candidate"]
    assert winter["reserve_soc_percent"] == pytest.approx(20.0)
    assert winter["requested_reserve_soc_percent"] == pytest.approx(10.0)
    assert winter["reserve_was_raised"] is True
    assert report["candidates"]["winter_self_sufficiency"][
        "protected_soc_percent"] == pytest.approx(20.0)
    # The carried figure is measured against the same raised floor.
    assert report["candidates"]["winter_self_sufficiency"]["today"][
        "floor_soc_percent"] == pytest.approx(20.0)


def test_winter_caveats_name_the_unbounded_grid_charging_difference(tmp_path, capsys):
    # Without demand-sized replenishment the row behaves closer to market
    # arbitrage minus export, which is its largest divergence from the real
    # engine and must not be omitted from what the reader is shown.
    report = _report(tmp_path, capsys, _crossing_plan(winter_reserve_soc_percent=80.0))

    caveats = " ".join(report["winter_candidate"]["caveats"]).lower()
    assert "grid-charge cap" in caveats
    assert "market arbitrage" in caveats


def test_every_row_publishes_the_floor_its_carried_energy_is_measured_above(tmp_path, capsys):
    report = _report(tmp_path, capsys, _crossing_plan(winter_reserve_soc_percent=80.0))

    assert report["plan_baseline"]["today"]["floor_soc_percent"] == pytest.approx(20.0)
    for candidate_id, candidate in report["candidates"].items():
        assert candidate["today"]["floor_soc_percent"] == pytest.approx(
            candidate["protected_soc_percent"]), candidate_id


def test_a_malformed_plan_reserve_costs_only_the_winter_row(tmp_path, capsys):
    # Graceful degradation matches the rest of the report: an absent key drops
    # the row, so a corrupt one must not blank the whole Advisor panel.
    report = _report(tmp_path, capsys, _crossing_plan(winter_reserve_soc_percent="not-a-number"))

    assert report["winter_candidate"] is None
    assert "winter_self_sufficiency" not in report["candidates"]
    assert report["candidates"]["market_arbitrage"]["feasible"] is True
    assert report["plan_baseline"]["available"] is True


def test_an_operator_typed_reserve_still_fails_loudly(tmp_path, capsys):
    path = _write_plan(tmp_path, _crossing_plan())

    assert main([
        "--plan", str(path), "--winter-reserve-soc-percent", "150", "--json",
    ]) == 2
    assert "must be between 0 and 100" in capsys.readouterr().err


def test_winter_row_can_be_suppressed_or_overridden_from_the_command_line(tmp_path, capsys):
    plan = _crossing_plan(winter_reserve_soc_percent=80.0)

    suppressed = _report(tmp_path, capsys, plan, "--no-winter-candidate")
    assert suppressed["winter_candidate"] is None
    assert "winter_self_sufficiency" not in suppressed["candidates"]

    overridden = _report(
        tmp_path, capsys, plan, "--winter-reserve-soc-percent", "50")
    assert overridden["winter_candidate"]["reserve_soc_percent"] == pytest.approx(50.0)


def test_an_out_of_range_plan_reserve_drops_only_the_winter_row(tmp_path, capsys):
    # Same graceful-degradation contract as a malformed value: the optional row
    # is forfeited, the rest of the comparison still renders.
    report = _report(tmp_path, capsys, _crossing_plan(winter_reserve_soc_percent=150.0))

    assert report["winter_candidate"] is None
    assert "winter_self_sufficiency" not in report["candidates"]
    assert report["candidates"]["protected_hybrid"]["feasible"] is True


def test_human_output_never_prints_a_winter_figure_without_its_caveats(tmp_path, capsys):
    path = _write_plan(tmp_path, _crossing_plan(winter_reserve_soc_percent=80.0))

    assert main(["--plan", str(path)]) == 0

    output = capsys.readouterr().out
    assert "Winter-style self-sufficiency (approx.)" in output
    assert "is an approximation held above a 80% reserve" in output
    assert "lib.ai_powered_ess_winter" in output
    assert "exceptional-spread export" in output


def test_human_output_leads_with_the_live_plan_on_the_whole_day_basis(tmp_path, capsys):
    path = _write_plan(tmp_path, _crossing_plan())

    assert main(["--plan", str(path)]) == 0

    output = capsys.readouterr().out
    assert "Live plan (active)" in output
    assert "Settled so far today (identical in every row below): +1.50 €." in output
    assert "Whole-day result per policy" in output
    assert "carries" in output and "into tomorrow" in output
    assert "cash " not in output
    assert "economic " not in output


def test_live_plan_label_names_summer_or_winter_mode_when_the_plan_states_it(tmp_path, capsys):
    summer = _report(tmp_path, capsys, _crossing_plan(optimizer_mode="summer"))
    assert summer["optimizer_mode"] == "summer"

    path = _write_plan(tmp_path, _crossing_plan(optimizer_mode="summer"))
    assert main(["--plan", str(path)]) == 0
    assert "Live plan (Summer mode, active)" in capsys.readouterr().out

    path = _write_plan(tmp_path, _crossing_plan(optimizer_mode="winter"))
    assert main(["--plan", str(path)]) == 0
    assert "Live plan (Winter mode, active)" in capsys.readouterr().out

    # A plan predating this field, or an unrecognised value, must not invent a
    # mode — fall back to the plain label rather than guess.
    path = _write_plan(tmp_path, _crossing_plan())
    assert main(["--plan", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Live plan (active)" in out
    assert "Summer mode" not in out and "Winter mode" not in out


def test_human_output_explains_what_drives_each_strategy_in_dashboard_terms(tmp_path, capsys):
    path = _write_plan(tmp_path, _crossing_plan(winter_reserve_soc_percent=80.0))

    assert main(["--plan", str(path)]) == 0

    output = capsys.readouterr().out
    assert "What drives each strategy:" in output
    # In the vocabulary already used elsewhere on the dashboard (the literal
    # control_action values, and the .env/Settings wording for "reserve"),
    # not the euro-denominated column headers, which describe outcome only.
    assert "BUY" in output and "SELL" in output and "RETAIN" in output
    assert "protected reserve" in output
    # One legend line per row that is actually in the table above it.
    assert "Market arbitrage:" in output
    assert "Protected hybrid:" in output
    assert "PV-first self-sufficiency:" in output
    assert "Winter-style self-sufficiency (approx.):" in output
