"""Pure shadow-candidate coverage for the ESS strategy research layer.

The evaluator deliberately has no settings, broker, MQTT, or Victron imports.
These tests therefore remain deterministic in GitHub Actions and make sure the
research output cannot accidentally turn into an execution path.
"""
from datetime import datetime, timedelta, timezone

import pytest

from lib.ess_strategy_candidates import (
    CandidateConfig,
    DeterministicSlot,
    evaluate_shadow_candidates,
)


def _config(**overrides):
    values = {
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
        "terminal_value_eur_per_dc_kwh": 0.0,
    }
    values.update(overrides)
    return CandidateConfig(**values)


def _slots(*prices, load_kwh=0.0, pv_kwh=0.0):
    start = datetime(2030, 6, 1, tzinfo=timezone.utc)
    return tuple(
        DeterministicSlot(
            start=start + timedelta(hours=index),
            duration_h=1.0,
            buy_price=price,
            load_kwh=load_kwh,
            pv_kwh=pv_kwh,
        )
        for index, price in enumerate(prices)
    )


def test_shadow_evaluator_returns_three_candidates_with_explainable_metrics():
    candidates = evaluate_shadow_candidates(
        _slots(0.10, 0.50),
        initial_soc_percent=60.0,
        config=_config(),
    )

    assert list(candidates) == [
        "market_arbitrage",
        "pv_first_self_sufficiency",
        "protected_hybrid",
    ]
    for candidate in candidates.values():
        assert candidate.feasible is True
        assert candidate.rejection_reason is None
        assert candidate.terminal_soc_percent >= 20.0
        assert candidate.dc_throughput_kwh >= 0.0
        assert candidate.full_equivalent_cycles >= 0.0
        assert candidate.cash_net_eur == pytest.approx(
            candidate.export_reward_eur - candidate.import_cost_eur
        )
        assert candidate.economic_net_eur == pytest.approx(
            candidate.cash_net_eur - candidate.lifecycle_cost_eur
        )
    assert candidates["market_arbitrage"].protected_soc_percent == 20.0
    assert candidates["pv_first_self_sufficiency"].protected_soc_percent == 60.0
    assert candidates["protected_hybrid"].protected_soc_percent == 60.0


def test_pv_first_never_uses_active_grid_charge_or_active_battery_export():
    candidates = evaluate_shadow_candidates(
        _slots(0.10, 0.50),
        initial_soc_percent=50.0,
        config=_config(protected_soc_percent=20.0),
    )

    market = candidates["market_arbitrage"]
    pv_first = candidates["pv_first_self_sufficiency"]

    assert market.grid_import_kwh > 0.0
    assert market.grid_export_kwh > 0.0
    assert pv_first.grid_import_kwh == pytest.approx(0.0)
    assert pv_first.grid_export_kwh == pytest.approx(0.0)
    assert all(not step.active_grid_charge for step in pv_first.schedule)
    assert all(not step.active_battery_export for step in pv_first.schedule)


def test_protected_hybrid_can_sell_only_energy_above_protected_reserve():
    candidates = evaluate_shadow_candidates(
        _slots(0.50),
        initial_soc_percent=100.0,
        config=_config(),
    )

    market = candidates["market_arbitrage"]
    hybrid = candidates["protected_hybrid"]

    assert market.terminal_soc_percent == pytest.approx(20.0)
    assert market.dc_discharge_kwh == pytest.approx(8.0)
    assert market.dc_throughput_kwh == pytest.approx(8.0)
    assert market.full_equivalent_cycles == pytest.approx(0.4)
    assert market.export_reward_eur == pytest.approx(3.6)
    assert hybrid.terminal_soc_percent == pytest.approx(60.0)
    assert hybrid.minimum_soc_percent >= 60.0
    assert hybrid.grid_export_kwh > 0.0
    assert all(
        not step.active_battery_export
        or step.soc_end_percent >= hybrid.protected_soc_percent
        for step in hybrid.schedule
    )


def test_hybrid_can_recover_only_to_protected_reserve_with_grid_energy():
    candidates = evaluate_shadow_candidates(
        _slots(0.10, 0.50),
        initial_soc_percent=20.0,
        config=_config(),
    )

    hybrid = candidates["protected_hybrid"]
    assert hybrid.feasible is True
    assert hybrid.terminal_soc_percent >= 60.0
    assert all(
        not step.active_grid_charge
        or step.soc_end_percent <= hybrid.protected_soc_percent
        for step in hybrid.schedule
    )


def test_protected_hybrid_recovers_a_missing_reserve_gradually_and_reports_shortfall():
    # A 2 kW charger can restore only 20 percentage points of this 10 kWh
    # battery per one-hour slot.  The safe candidate must recover 20 -> 40 ->
    # 60 rather than falsely declaring the whole horizon infeasible because it
    # cannot teleport to the protected reserve in the first slot.
    candidates = evaluate_shadow_candidates(
        _slots(0.10, 0.10),
        initial_soc_percent=20.0,
        config=_config(max_charge_kw=2.0, max_import_kw=3.0),
    )

    hybrid = candidates["protected_hybrid"]
    assert hybrid.feasible is True
    assert [step.soc_end_percent for step in hybrid.schedule] == [40.0, 60.0]
    assert hybrid.minimum_soc_percent == pytest.approx(20.0)
    assert hybrid.protection_shortfall_kwh == pytest.approx(4.0)
    assert all(step.dc_change_kwh >= 0.0 for step in hybrid.schedule)


def test_terminal_value_is_disabled_for_a_same_day_only_horizon():
    # Mirrors the live optimizer: retained energy is not assigned a terminal
    # value when tomorrow has not been published.  A profitable same-day sale
    # must therefore not be hidden by an artificial retained-energy bonus.
    candidates = evaluate_shadow_candidates(
        _slots(0.50),
        initial_soc_percent=100.0,
        config=_config(
            protected_soc_percent=20.0,
            terminal_value_eur_per_dc_kwh=1.0,
        ),
    )

    market = candidates["market_arbitrage"]
    assert market.terminal_soc_percent == pytest.approx(20.0)
    assert market.model_score_eur == pytest.approx(market.economic_net_eur)


def test_terminal_value_is_applied_only_when_timestamps_cross_a_day_boundary():
    start = datetime(2030, 6, 1, 23, 0, tzinfo=timezone.utc)
    slots = (
        DeterministicSlot(start=start, duration_h=1.0, buy_price=0.10, load_kwh=0.0, pv_kwh=0.0),
        DeterministicSlot(start=start + timedelta(hours=1), duration_h=1.0, buy_price=0.10, load_kwh=0.0, pv_kwh=0.0),
    )
    candidates = evaluate_shadow_candidates(
        slots,
        initial_soc_percent=100.0,
        config=_config(
            protected_soc_percent=20.0,
            terminal_value_eur_per_dc_kwh=1.0,
        ),
    )

    assert candidates["market_arbitrage"].terminal_soc_percent == pytest.approx(100.0)


def test_reports_infeasibility_without_falling_back_to_a_real_control_path():
    impossible = _config(
        max_charge_kw=0.0,
        max_discharge_kw=0.0,
        max_import_kw=0.0,
        max_export_kw=0.0,
    )
    candidates = evaluate_shadow_candidates(
        _slots(0.20, load_kwh=1.0),
        initial_soc_percent=20.0,
        config=impossible,
    )

    for candidate in candidates.values():
        assert candidate.feasible is False
        assert candidate.rejection_reason == "no_physical_feasible_schedule"
        assert candidate.schedule == ()
