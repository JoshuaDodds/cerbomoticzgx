"""Tests for the strictly read-only ESS strategy comparison command."""
from __future__ import annotations

import json

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
