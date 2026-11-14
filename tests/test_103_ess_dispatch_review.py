"""Economic and physical regressions from the October dispatch review."""
from datetime import datetime, timedelta
import time

import pytest
from lib import ai_powered_ess as summer
from lib import ai_powered_ess_winter as winter
from lib import ev_charge_controller as ev


def prices(values):
    start = datetime.now().astimezone() + timedelta(hours=1)
    return [{'start': start + timedelta(minutes=15*i), 'total': p}
            for i, p in enumerate(values)]


@pytest.mark.parametrize('load', [0.15, 0.03, 0.001])
def test_small_house_load_is_supplied_without_export(load):
    engine = summer.OptimizationEngine()
    engine.battery_capacity = 42
    engine.discharge_efficiency = .92
    engine.min_soc = 40
    engine.soc_step = 1
    engine.terminal_value_factor = 0
    engine.cycle_cost = engine.arbitrage_margin = 0
    result = engine.optimize(90, prices([.50]*4), [load]*4, [0]*4,
                             policy_name='pv_first_self_sufficiency')
    assert all(s['control_action'] == 'IDLE' for s in result['schedule'])
    assert all(abs(s['grid_energy']) <= .0001 for s in result['schedule'])
    used_dc = (result['schedule'][0]['soc_start'] - result['schedule'][-1]['soc_end']) / 100 * 42
    assert used_dc == pytest.approx(4 * load / .92, abs=.0001)


def test_green_buys_cheap_and_supplies_peak_without_export(monkeypatch):
    monkeypatch.setattr(summer, 'retrieve_setting', lambda k: {
        'MIN_SOC_RESERVE_WINTER': '40'}.get(k))
    engine = summer.OptimizationEngine()
    engine.battery_capacity = 10
    engine.min_soc = 5
    engine.soc_step = 1
    engine.terminal_value_factor = 0
    engine.cycle_cost = engine.arbitrage_margin = 0
    result = engine.optimize(40, prices([.05]*4+[.50]*8), [.25]*12, [0]*12,
                             policy_name='green_conservation')
    assert any(s['control_action'] == 'BUY' for s in result['schedule'][:4])
    assert any(s['control_action'] == 'IDLE' and s['soc_end'] < s['soc_start']
               for s in result['schedule'][4:])
    assert all(s['soc_end'] >= 40 - .0001 for s in result['schedule'])
    assert all(s['control_action'] != 'SELL' for s in result['schedule'])


def test_winter_replay_accounts_for_integer_target_and_early_charging(monkeypatch):
    monkeypatch.setattr(winter, 'retrieve_setting', lambda k: {
        'BATTERY_CAPACITY_KWH': '10', 'MIN_SOC_RESERVE_WINTER': '40',
        'ESS_MAX_GRID_CHARGE_SOC': '80', 'ESS_MAX_CHARGE_KW': '4',
        'ESS_MAX_GRID_IMPORT_KW': '10', 'AC_DC_CHARGE_EFFICIENCY': '1',
        'AC_DC_DISCHARGE_EFFICIENCY': '1', 'HISTORY_DIR': '/nonexistent',
    }.get(k))
    engine = winter.OptimizationEngine()
    engine.initial_protected_soc = 40
    schedule = []
    for point, a, b in zip(prices([.2, .1]), [40, 40.8], [40.8, 50.2]):
        schedule.append(dict(time=point['start'], price=point['total'], sell=.1,
                             load=0, pv=0, soc_start=a, soc_end=b,
                             grid_energy=(b-a)/10, action='buy'))
    candidate = dict(schedule=schedule, replenishment_windows=[(0, 1)])
    result = engine._replay_charge_windows(candidate, .25, [40, 40])
    assert result['victron_slots'][0]['target_soc'] == 51
    assert result['schedule'][0]['soc_end'] == 50
    assert result['schedule'][1]['soc_start'] == 50
    assert result['schedule'][1]['soc_end'] == 51
    assert sum(s['grid_energy'] for s in result['schedule']) == pytest.approx(1.1)


def test_stale_phase_current_does_not_count_as_charge_delivery():
    engine = ev.EvCharger.__new__(ev.EvCharger)
    engine.global_state = {'tesla_l1_current_updated_at': time.time()-3600}
    engine.l1_charging_amps = 8
    engine.charging_amps = 8
    assert engine._local_charger_draw() is None
    engine.global_state['tesla_l1_current_updated_at'] = time.time()
    assert engine._local_charger_draw() is True


def test_winter_learned_risk_changes_normal_procurement(monkeypatch):
    values = {'BATTERY_CAPACITY_KWH': '20', 'MIN_SOC_RESERVE_WINTER': '20',
              'OPTIMIZER_SLOT_MINUTES': '60', 'OPTIMIZER_SOC_STEP_PCT': '1',
              'ESS_MAX_GRID_CHARGE_SOC': '100', 'ESS_MAX_CHARGE_KW': '30',
              'ESS_MAX_GRID_IMPORT_KW': '30', 'ESS_MIN_SELL_PRICE': '1',
              'AC_DC_CHARGE_EFFICIENCY': '.92', 'AC_DC_DISCHARGE_EFFICIENCY': '.92',
              'HISTORY_DIR': '/nonexistent'}
    monkeypatch.setattr(winter, 'retrieve_setting', values.get)
    start = datetime.now().astimezone() + timedelta(hours=1)
    tariffs = [{'start': start + timedelta(hours=i), 'total': p}
               for i, p in enumerate([.05, .4, .4, .4, .4])]
    results = []
    for rate in (.12, .25):
        engine = winter.OptimizationEngine()
        engine.uncertainty_model = dict(engine.uncertainty_model, rate=rate)
        result = engine.optimize(20, tariffs, [0, 1, 1, 1, 1], [0]*5)
        assert result['winter_policy']['selected_candidate'] == 'self_sufficiency'
        assert all(s['grid_energy'] <= .0001 for s in result['schedule'][1:])
        results.append(result)
    assert results[1]['schedule'][0]['grid_energy'] > results[0]['schedule'][0]['grid_energy']
    assert results[1]['schedule'][0]['soc_end'] < 100


def test_old_start_with_grid_assist_off_is_cleared_despite_retained_high_current(monkeypatch):
    from types import SimpleNamespace
    class State(dict):
        def set(self, key, value):
            self[key] = value
    engine = ev.EvCharger.__new__(ev.EvCharger)
    engine.global_state = State(ev_charge_requested=True, grid_charging_enabled=False,
                               ev_charge_requested_at=time.time()-3600,
                               tesla_l1_current_updated_at=time.time()-3600)
    engine.charging_amps = engine.l1_charging_amps = 8
    engine.tesla = SimpleNamespace(is_charging=False)
    monkeypatch.setattr(ev, 'publish_message', lambda *a, **kw: None)
    engine._clear_unactionable_manual_start()
    assert engine.global_state['ev_charge_requested'] is False


def test_grey_day_selector_uses_green_until_trade_clears_hurdle(monkeypatch):
    engine = summer.OptimizationEngine()
    engine.terminal_value_factor = 0
    engine.cycle_cost = engine.arbitrage_margin = 0
    monkeypatch.setattr(summer, '_load_adaptive_policy', lambda: {})
    monkeypatch.setattr(summer, '_persist_adaptive_policy', lambda value: None)
    monkeypatch.setattr(summer, 'retrieve_setting', lambda key: {
        'ESS_ADAPTIVE_TRADE_MIN_BENEFIT_EUR': '1',
        'ESS_ADAPTIVE_UNKNOWN_HORIZON_HOURS': '0',
    }.get(key))
    def plan(name, energy):
        return dict(strategy=name, slot_duration_h=.25, schedule=[dict(
            time=prices([.5])[0]['start'], price=.5, sell=.5, load=1, pv=0,
            soc_start=50, soc_end=50, grid_energy=energy)])
    candidates = {name: plan(name, energy) for name, energy in (
        ('green_conservation', 1), ('pv_first_self_sufficiency', .9),
        ('market_arbitrage', .8))}
    selected, _ = summer._select_adaptive_policy(candidates, engine,
                                                opportunity_model={'forecast_risk_eur': 0})
    assert selected['strategy'] == 'green_conservation'
    candidates['market_arbitrage'] = plan('market_arbitrage', -3)
    selected, _ = summer._select_adaptive_policy(candidates, engine,
                                                opportunity_model={'forecast_risk_eur': 0})
    assert selected['strategy'] == 'market_arbitrage'


def test_fractional_supply_can_hold_when_next_slot_blocks_discharge():
    engine = summer.OptimizationEngine()
    engine.battery_capacity = 42
    engine.discharge_efficiency = .92
    engine.min_soc = 40
    engine.soc_step = 1
    engine.terminal_value_factor = 0
    engine.cycle_cost = engine.arbitrage_margin = 0
    tariffs = prices([.50, .10])
    result = engine.optimize(90, tariffs, [.15, .15], [0, 0],
                             policy_name='pv_first_self_sufficiency',
                             discharge_blocked_slots=[tariffs[1]['start']])
    first, second = result['schedule']
    assert first['grid_energy'] == pytest.approx(0, abs=.0001)
    assert second['soc_start'] == pytest.approx(first['soc_end'])
    assert second['soc_end'] == second['soc_start']
    assert second['grid_energy'] == pytest.approx(.15)


def test_winter_small_peak_load_is_exact_and_replay_does_not_drain_extra(monkeypatch):
    monkeypatch.setattr(winter, 'retrieve_setting', lambda k: {
        'BATTERY_CAPACITY_KWH': '42', 'MIN_SOC_RESERVE_WINTER': '40',
        'OPTIMIZER_SOC_STEP_PCT': '1', 'ESS_MIN_SELL_PRICE': '2',
        'AC_DC_DISCHARGE_EFFICIENCY': '.92', 'HISTORY_DIR': '/nonexistent',
    }.get(k))
    engine = winter.OptimizationEngine()
    tariffs = prices([.4693, .4525, .42, .40])
    slots, hours = engine._normalise(tariffs, [.224, .4981, .3, .3], [0]*4)
    engine._adapt_soc_lattice(slots, 90)
    engine.initial_protected_soc = 90
    windows = engine._replenishment_windows(slots)
    _, envelope, _ = engine._coverage(slots, hours, windows)
    raw = engine._run_candidate(slots, hours, windows, {}, envelope, False)
    assert raw['schedule'][0]['action'] == 'self_supply'
    assert abs(raw['schedule'][0]['grid_energy']) < .0001
    end_before = raw['schedule'][-1]['soc_end']
    replay = engine._replay_charge_windows(raw, hours, envelope)
    assert not replay['victron_slots']
    assert replay['schedule'][-1]['soc_end'] == pytest.approx(end_before, abs=.00001)
    result = engine.optimize(90, tariffs, [.224, .4981, .3, .3], [0]*4)
    assert result['winter_policy']['next_replenishment_time'] is None
    assert result['winter_policy']['active_charge_windows'] == 0
    assert result['winter_policy']['next_eligible_charge_time'] is not None


def test_winter_delays_grouped_purchase_to_cheaper_slots(monkeypatch):
    monkeypatch.setattr(winter, 'retrieve_setting', lambda key: {
        'BATTERY_CAPACITY_KWH': '42', 'MIN_SOC_RESERVE_WINTER': '0',
        'ESS_MAX_GRID_IMPORT_KW': '17', 'ESS_MAX_CHARGE_KW': '16',
        'AC_DC_CHARGE_EFFICIENCY': '.92', 'HISTORY_DIR': '/nonexistent',
    }.get(key))
    engine = winter.OptimizationEngine()
    engine.initial_protected_soc = 19.5
    schedule = []
    starts = [19.5, 19.6, 19.6, 28]
    ends = [19.6, 19.6, 28, 35]
    for point, a, b in zip(prices([.4002, .4016, .382, .3822]), starts, ends):
        schedule.append(dict(time=point['start'], price=point['total'], sell=.35,
                             load=.3, pv=0, soc_start=a, soc_end=b,
                             grid_energy=.3 + (b-a)*.42/.92,
                             action='buy' if b>a else 'hold'))
    candidate = dict(schedule=schedule, replenishment_windows=[(0, 3)])
    old = engine._replay_charge_windows(
        dict(candidate, schedule=[dict(s) for s in schedule]), .25, [0]*4)
    new = engine._choose_charge_window_starts(candidate, .25, [0]*4)
    assert new['victron_slots'][0]['start'] == schedule[2]['time']
    assert new['schedule'][0]['soc_end'] == 19.5
    assert new['schedule'][-1]['soc_end'] == pytest.approx(35)
    assert new['objective_cost'] < old['objective_cost']
    assert len(new['victron_slots']) <= 5
