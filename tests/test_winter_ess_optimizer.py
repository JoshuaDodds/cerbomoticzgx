"""TDD coverage for the isolated Winter Mode ESS optimizer."""

import ast
import json
import math
from datetime import datetime, timedelta

import pytest

from lib import ai_powered_ess_winter as winter


DEFAULTS = {
    'BATTERY_CAPACITY_KWH': '20',
    'AC_DC_CHARGE_EFFICIENCY': '0.90',
    'AC_DC_DISCHARGE_EFFICIENCY': '0.90',
    'ESS_MAX_GRID_IMPORT_KW': '10',
    'ESS_MAX_GRID_EXPORT_KW': '10',
    'ESS_MAX_CHARGE_KW': '10',
    'ESS_MAX_DISCHARGE_KW': '10',
    'ESS_EXPORT_PRICE_FACTOR': '1',
    'ESS_EXPORT_FEE': '0',
    'ESS_MIN_SELL_PRICE': '0',
    'ESS_BATTERY_CYCLE_COST': '0.03',
    'ESS_ARBITRAGE_MARGIN': '0.03',
    'OPTIMIZER_SLOT_MINUTES': '60',
    'DAILY_HOME_ENERGY_CONSUMPTION': '12',
    'MIN_SOC_RESERVE_WINTER': '20',
    'MIN_SOC_RESERVE_SUMMER': '5',
    'ESS_MAX_GRID_CHARGE_SOC': '90',
    'OPTIMIZER_SOC_STEP_PCT': '5',
    'ESS_EXPORT_AC_SETPOINT': '-10000',
    'HISTORY_DIR': '/tmp/cerbomoticz-winter-tests-no-history',
}


@pytest.fixture
def settings(monkeypatch, tmp_path):
    values = dict(DEFAULTS)
    values['HISTORY_DIR'] = str(tmp_path / 'no-history')
    winter._load_uncertainty_model.cache_clear()
    monkeypatch.setattr(winter, 'retrieve_setting', lambda name: values.get(name))
    return values


def _prices(values, minutes=60):
    now = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    start = now + timedelta(hours=1)
    return [
        {'start': start + timedelta(minutes=minutes * index), 'total': value}
        for index, value in enumerate(values)
    ]


def _active_sells(result):
    return [step for step in result['schedule'] if step['control_action'] == 'SELL']


def _active_buys(result):
    return [step for step in result['schedule'] if step['control_action'] == 'BUY']


def test_module_is_structurally_isolated_from_summer_optimizer():
    tree = ast.parse(open(winter.__file__, encoding='utf-8').read())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or '')
    assert 'lib.ai_powered_ess' not in imports
    assert callable(winter.optimize_schedule)
    assert winter._coerce_datetime('2026-01-01T00:00:00+01:00').year == 2026


def test_result_contract_and_explicit_winter_reserve(settings):
    result = winter.OptimizationEngine().optimize(
        30, _prices([0.10, 0.30, 0.10]), [0.5] * 3, [0] * 3)

    assert result is not None
    assert {
        'schedule', 'victron_slots', 'optimizer_guardrails', 'setpoint', 'mode',
        'control_action', 'reason', 'reason_code', 'grid_assist', 'pv_surplus',
        'current_price', 'limit_feed_in', 'slot_duration_h', 'winter_policy',
    } <= result.keys()
    assert result['optimizer_mode'] == 'winter'
    assert min(step['soc_end'] for step in result['schedule']) >= 20
    assert result['winter_policy']['mode'] == 'winter'
    json.dumps(result, default=str)


def test_normal_plan_charges_only_for_house_coverage_not_user_max(settings):
    prices = _prices([0.10, 0.10, 0.34, 0.34, 0.34, 0.34, 0.10, 0.10])
    loads = [0, 0, 1, 1, 1, 1, 0, 0]
    result = winter.OptimizationEngine().optimize(20, prices, loads, [0] * 8)

    assert result['winter_policy']['selected_candidate'] == 'self_sufficiency'
    assert _active_buys(result)
    assert max(step['soc_end'] for step in result['schedule']) < 90
    assert not _active_sells(result)


def test_large_battery_can_self_supply_small_quarter_hour_load_without_export(settings):
    settings.update({
        'BATTERY_CAPACITY_KWH': '42',
        'OPTIMIZER_SOC_STEP_PCT': '1',
        'OPTIMIZER_SLOT_MINUTES': '15',
        'MIN_SOC_RESERVE_WINTER': '20',
    })
    prices = _prices([0.10] * 4 + [0.34] * 8 + [0.10] * 4, minutes=15)
    loads = [0.1] * 4 + [0.3] * 8 + [0.1] * 4
    result = winter.OptimizationEngine().optimize(50, prices, loads, [0] * 16)

    self_supply = [
        step for step in result['schedule'] if step['action'] == 'self_supply'
    ]
    assert self_supply, 'winter policy must cover peak household demand from battery'
    assert all(abs(step['grid_energy']) <= 1e-4 for step in self_supply)
    assert not _active_sells(result)
    assert result['winter_policy']['soc_step_percent'] < 1


def test_pv_forecast_reduces_winter_replenishment_target(settings):
    prices = _prices([0.10, 0.10, 0.34, 0.34, 0.34, 0.34, 0.10])
    loads = [0, 0, 1, 1, 1, 1, 0]
    no_pv = winter.OptimizationEngine().optimize(20, prices, loads, [0] * 7)
    with_pv = winter.OptimizationEngine().optimize(20, prices, loads, [0, 0, 1, 1, 0, 0, 0])

    assert with_pv['winter_policy']['protected_soc_percent'] \
        < no_pv['winter_policy']['protected_soc_percent']


def test_load_uncertainty_is_learned_from_measured_settlement_history(settings, tmp_path):
    history = tmp_path / 'ess-2026-01-01.ndjson'
    rows = [
        json.dumps({
            'kind': 'settlement',
            'incomplete': False,
            'predicted_load_kwh': 0.2,
            'base_load_kwh': 0.3,
            'load_meter_quality': 'measured',
            'ev_meter_quality': 'measured',
        })
        for _ in range(60)
    ]
    history.write_text('\n'.join(rows) + '\n', encoding='utf-8')
    settings['HISTORY_DIR'] = str(tmp_path)
    winter._load_uncertainty_model.cache_clear()

    engine = winter.OptimizationEngine()
    result = engine.optimize(
        20, _prices([0.10, 0.40, 0.10]), [0, 1, 0], [0] * 3)

    assert engine.uncertainty_model['source'] == 'settlement_history'
    assert engine.uncertainty_model['samples'] == 60
    assert engine.uncertainty_model['rate'] == 0.25
    assert result['winter_policy']['uncertainty_source'] == 'settlement_history'
    assert result['winter_policy']['uncertainty_samples'] == 60
    assert result['winter_policy']['uncertainty_quantile'] == 0.8
    assert result['winter_policy']['uncertainty_min_kwh'] == 0.5
    assert result['winter_policy']['uncertainty_max_kwh'] == 3.0


def test_unclassified_raw_actual_load_never_trains_uncertainty(settings, tmp_path):
    history = tmp_path / 'ess-2026-01-02.ndjson'
    rows = [
        json.dumps({
            'kind': 'settlement',
            'incomplete': False,
            'predicted_load_kwh': 0.2,
            'actual_load_kwh': 4.2,  # could be EV-contaminated legacy load
            'base_load_kwh': None,
            'load_meter_quality': 'measured',
            'ev_meter_quality': 'measured',
        })
        for _ in range(60)
    ]
    history.write_text('\n'.join(rows) + '\n', encoding='utf-8')
    settings['HISTORY_DIR'] = str(tmp_path)
    winter._load_uncertainty_model.cache_clear()

    model = winter.OptimizationEngine().uncertainty_model

    assert model['source'] == 'bounded_fallback'
    assert model['samples'] == 0
    assert model['rate'] == 0.12


def test_uncertainty_cache_refreshes_when_history_file_changes(settings, tmp_path):
    history = tmp_path / 'ess-2026-01-03.ndjson'
    settings['HISTORY_DIR'] = str(tmp_path)
    winter._load_uncertainty_model.cache_clear()
    history.write_text('', encoding='utf-8')
    first = winter.OptimizationEngine().uncertainty_model
    row = json.dumps({
        'kind': 'settlement', 'incomplete': False,
        'predicted_load_kwh': 0.2, 'base_load_kwh': 0.3,
        'load_meter_quality': 'measured', 'ev_meter_quality': 'measured',
    })
    history.write_text((row + '\n') * 60, encoding='utf-8')

    second = winter.OptimizationEngine().uncertainty_model

    assert first['source'] == 'bounded_fallback'
    assert second['source'] == 'settlement_history'
    assert second['samples'] == 60


def test_ordinary_profitable_spread_never_actively_exports(settings):
    result = winter.OptimizationEngine().optimize(
        20, _prices([0.10, 0.10, 0.22, 0.22, 0.10]), [0] * 5, [0] * 5)

    assert result['winter_policy']['selected_candidate'] == 'self_sufficiency'
    assert result['winter_policy']['reason_code'] == 'WINTER_EXCEPTIONAL_SPREAD_REJECTED'
    assert not _active_sells(result)


def test_exceptional_candidate_charges_and_exports_above_protection(settings):
    result = winter.OptimizationEngine().optimize(
        20, _prices([0.10, 0.10, 0.55, 0.55, 0.10]), [0] * 5, [0] * 5)

    assert result['winter_policy']['selected_candidate'] == 'exceptional_arbitrage'
    assert result['winter_policy']['expected_incremental_benefit_eur'] >= 1
    assert _active_buys(result)
    assert _active_sells(result)
    for step in _active_sells(result):
        assert step['soc_end'] + 1e-6 >= result['winter_policy']['protected_soc_percent']


def test_exceptional_candidate_respects_exact_user_max_soc(settings):
    settings['ESS_MAX_GRID_CHARGE_SOC'] = '65'
    result = winter.OptimizationEngine().optimize(
        20, _prices([0.08, 0.08, 0.60, 0.60, 0.08]), [0] * 5, [0] * 5)

    assert max(step['soc_end'] for step in result['schedule']) <= 65
    assert all(slot['target_soc'] <= 65 for slot in result['victron_slots'])


def test_house_load_spike_protects_energy_and_can_reject_export(settings):
    settings['ESS_MAX_GRID_CHARGE_SOC'] = '60'
    prices = _prices([0.08, 0.08, 0.65, 0.40, 0.40, 0.08])
    loads = [0, 0, 0, 6, 6, 0]
    result = winter.OptimizationEngine().optimize(20, prices, loads, [0] * 6)

    assert result['winter_policy']['selected_candidate'] == 'self_sufficiency'
    assert not _active_sells(result)
    assert result['winter_policy']['warning'] == 'coverage_limited_by_configured_max_soc'


def test_exceptional_stress_envelope_does_not_rely_on_forecast_pv(settings):
    settings['ESS_MAX_GRID_CHARGE_SOC'] = '60'
    prices = _prices([0.05, 0.05, 0.70, 0.40, 0.40, 0.05])
    loads = [0, 0, 0, 4, 4, 0]
    # Optimistic PV cancels the normal load forecast, but exceptional export must
    # still stress the post-sale trajectory with near-zero PV.
    pv = [0, 0, 0, 6, 6, 0]
    result = winter.OptimizationEngine().optimize(20, prices, loads, pv)

    assert result['winter_policy']['selected_candidate'] == 'self_sufficiency'
    assert not _active_sells(result)


def test_cost_basis_protects_opening_tranche_but_allows_new_cheap_energy(settings):
    engine = winter.OptimizationEngine()
    engine.set_cost_basis_floor(0.60)  # AC recovery floor 0.667, above sale price.
    result = engine.optimize(
        60, _prices([0.08, 0.08, 0.55, 0.55, 0.08]), [0] * 5, [0] * 5)

    assert result['winter_policy']['selected_candidate'] == 'exceptional_arbitrage'
    assert _active_sells(result)
    assert min(step['soc_end'] for step in _active_sells(result)) >= 60


def test_absolute_sell_floor_blocks_exceptional_export(settings):
    settings['ESS_MIN_SELL_PRICE'] = '0.70'
    result = winter.OptimizationEngine().optimize(
        20, _prices([0.08, 0.08, 0.60, 0.60, 0.08]), [0] * 5, [0] * 5)

    assert result['winter_policy']['selected_candidate'] == 'self_sufficiency'
    assert not _active_sells(result)


def test_export_factor_and_fee_are_applied_before_exceptional_decision(settings):
    settings['ESS_EXPORT_PRICE_FACTOR'] = '0.5'
    settings['ESS_EXPORT_FEE'] = '0.10'
    result = winter.OptimizationEngine().optimize(
        20, _prices([0.05, 0.05, 0.60, 0.60, 0.05]), [0] * 5, [0] * 5)

    assert result['winter_policy']['selected_candidate'] == 'self_sufficiency'
    assert not _active_sells(result)


def test_single_day_horizon_protects_unknown_tail_after_last_price(settings):
    base = (datetime.now().astimezone() + timedelta(days=1)).replace(
        hour=20, minute=0, second=0, microsecond=0)
    prices = [
        {'start': base + timedelta(hours=index), 'total': price}
        for index, price in enumerate([0.10, 0.40, 0.40])
    ]
    result = winter.OptimizationEngine().optimize(
        20, prices, [0, 1, 1], [0, 0, 0])

    # The known post-trough load is 2 kWh. A same-day horizon must add the
    # conservative unknown-price tail instead of assuming a cheap midnight slot.
    assert result['winter_policy']['forecast_house_energy_required_kwh'] > 2.0
    assert result['winter_policy']['protected_soc_percent'] > 20.0


def test_quarter_hour_resolution_and_physical_power_limits(settings):
    settings['OPTIMIZER_SLOT_MINUTES'] = '15'
    settings['ESS_MAX_GRID_IMPORT_KW'] = '2'
    settings['ESS_MAX_GRID_EXPORT_KW'] = '3'
    settings['ESS_MAX_CHARGE_KW'] = '1.5'
    settings['ESS_MAX_DISCHARGE_KW'] = '2.5'
    prices = _prices([0.05] * 4 + [0.70] * 4 + [0.05] * 2, minutes=15)
    result = winter.OptimizationEngine().optimize(40, prices, [0] * 10, [0] * 10)

    assert result['slot_duration_h'] == pytest.approx(0.25)
    for step in result['schedule']:
        assert step['grid_energy'] <= 2 * 0.25 + 1e-6
        assert -step['grid_energy'] <= 3 * 0.25 + 1e-6
        dc = abs(step['soc_end'] - step['soc_start']) / 100 * 20
        limit = 1.5 if step['soc_end'] >= step['soc_start'] else 2.5
        assert dc <= limit * 0.25 + 1e-6


def test_infeasible_checkpoint_degrades_with_warning_not_invented_energy(settings):
    settings['ESS_MAX_CHARGE_KW'] = '0.1'
    settings['ESS_MAX_GRID_IMPORT_KW'] = '1'
    prices = _prices([0.05, 0.60, 0.60, 0.05])
    loads = [0, 1, 1, 0]
    result = winter.OptimizationEngine().optimize(20, prices, loads, [0] * 4)

    assert result is not None
    assert result['winter_policy']['warning'] == 'coverage_infeasible_safest_feasible_plan'
    for step in result['schedule']:
        dc = max(0, step['soc_end'] - step['soc_start']) / 100 * 20
        assert dc <= 0.1 + 1e-6


def test_negative_and_flat_prices_are_stable(settings):
    negative = winter.OptimizationEngine().optimize(
        20, _prices([-0.05, -0.05, 0.20]), [0] * 3, [0] * 3)
    flat = winter.OptimizationEngine().optimize(
        40, _prices([0.20] * 6), [0.25] * 6, [0] * 6)

    assert _active_buys(negative)
    assert negative['limit_feed_in'] is True
    assert not _active_sells(flat)
    assert len(flat['victron_slots']) <= 1


def test_normal_pv_surplus_is_idle_not_active_battery_export(settings):
    result = winter.OptimizationEngine().optimize(
        100, _prices([0.20, 0.20, 0.20]), [0] * 3, [1] * 3)

    assert result['control_action'] == 'IDLE'
    assert result['setpoint'] == 0.0
    assert result['pv_surplus'] is True
    assert not _active_sells(result)


def test_non_finite_price_and_forecasts_are_sanitized(settings):
    prices = _prices([0.10, 0.40, 0.10])
    prices.insert(1, {'start': prices[0]['start'] + timedelta(minutes=30), 'total': float('nan')})
    result = winter.OptimizationEngine().optimize(
        30, prices, [float('nan'), float('inf'), 0.5, 0.5],
        [float('nan'), float('-inf'), 0, 0],
    )

    assert result is not None
    for step in result['schedule']:
        assert math.isfinite(step['price'])
        assert math.isfinite(step['load'])
        assert math.isfinite(step['pv'])
        assert math.isfinite(step['grid_energy'])


def test_six_fragmented_troughs_never_publish_a_truncated_charge_plan(settings):
    # Six isolated troughs exceed the Victron five-slot limit. The planner must
    # retain the final fallback trough as eligible and forbid charge outside its
    # selected five windows, rather than optimizing six and silently dropping an
    # active BUY. The final eligible trough need not be used when no later load
    # requires replenishment (in particular when the horizon crosses midnight).
    values = [0.08, 0.40] * 6 + [0.08]
    loads = [0, 1.0] * 6 + [0]
    result = winter.OptimizationEngine().optimize(
        20, _prices(values), loads, [0] * len(values))

    assert result['winter_policy']['active_charge_windows'] == 5
    assert len(result['victron_slots']) <= 5
    assert result['victron_slots']
    for step in _active_buys(result):
        assert any(
            slot['start'] <= step['time']
            < slot['start'] + timedelta(seconds=slot['duration'])
            for slot in result['victron_slots']
        )
    for previous, current in zip(result['schedule'], result['schedule'][1:]):
        assert current['soc_start'] == pytest.approx(previous['soc_end'])


def test_opening_soc_is_exact_and_never_snapped_into_free_energy(settings):
    result = winter.OptimizationEngine().optimize(
        22.4, _prices([0.10, 0.40, 0.10]), [0, 0.3, 0], [0] * 3)

    assert result['schedule'][0]['soc_start'] == pytest.approx(22.4)
    for previous, current in zip(result['schedule'], result['schedule'][1:]):
        assert current['soc_start'] == pytest.approx(previous['soc_end'])


def test_cap_below_reserve_is_not_raised_and_recovers_safely(settings):
    settings['MIN_SOC_RESERVE_WINTER'] = '20'
    settings['ESS_MAX_GRID_CHARGE_SOC'] = '15'
    result = winter.OptimizationEngine().optimize(
        10, _prices([0.05, 0.40, 0.05]), [0] * 3, [0] * 3)

    assert result is not None
    assert result['optimizer_guardrails']['max_grid_charge_soc'] == 15
    assert result['winter_policy']['warning'] == 'max_grid_charge_soc_below_winter_reserve'
    assert result['schedule'][0]['soc_start'] == 10
    assert max(step['soc_end'] for step in result['schedule']) <= 15
    assert min(step['soc_end'] for step in result['schedule']) >= 10


def test_decimal_user_cap_uses_non_exceeding_victron_integer_target(settings):
    settings['ESS_MAX_GRID_CHARGE_SOC'] = '64.5'
    result = winter.OptimizationEngine().optimize(
        20, _prices([0.05, 0.05, 0.70, 0.70, 0.05]), [0] * 5, [0] * 5)

    assert max(step['soc_end'] for step in result['schedule']) <= 64.5
    assert all(slot['target_soc'] <= 64 for slot in result['victron_slots'])


def test_formatter_is_self_contained(settings):
    result = winter.OptimizationEngine().optimize(
        20, _prices([0.10, 0.30, 0.10]), [0.5] * 3, [0] * 3)
    summary = winter.format_plan_summary(result)
    assert summary.startswith('WINTER_ESS plan:')
    assert 'protected' in summary
