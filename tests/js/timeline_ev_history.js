const assert = require("assert");
const vm = require("vm");
const { loadDashboard } = require("./cold_load_smoke");

const { context } = loadDashboard();

const actual = vm.runInContext(
  `hourRowInner({
    is_current: false,
    label: "Sat 15:00",
    planned_ev_kwh: 0,
    actual_ev_kwh: 6.98,
    ev_target_kw: 0,
    ev_supply: null,
    ev_tentative: false,
    avg_price: 0.13,
    grid_kwh: 1,
    production_kwh: 2,
    consumption_kwh: 7,
    soc_start: 50,
    soc_end: 60,
    net_cost: 0.2
  })`,
  context,
);
assert.match(actual, /ev-actual/);
assert.match(actual, /EV 6\.98 kWh actual/);

const standbyNoise = vm.runInContext(
  `hourRowInner({
    is_current: false,
    label: "Sat 18:00",
    planned_ev_kwh: 0,
    actual_ev_kwh: 0.01,
    ev_target_kw: 0,
    ev_supply: null,
    ev_tentative: false,
    avg_price: 0.25,
    grid_kwh: 0,
    production_kwh: 0,
    consumption_kwh: 1,
    soc_start: 90,
    soc_end: 90,
    net_cost: 0
  })`,
  context,
);
assert.doesNotMatch(standbyNoise, /ev-actual/);

const exactTiming = vm.runInContext(
  `evObservedTiming({
    actual_ev_observed_from: "2026-07-25T11:53:00+02:00",
    actual_ev_observed_until: "2026-07-25T12:34:00+02:00",
    actual_ev_timing_quality: "meter_transition"
  })`,
  context,
);
assert.match(exactTiming, /11:53–12:34 · ABB power transitions/);

const intervalTiming = vm.runInContext(
  `evObservedTiming({
    actual_ev_observed_from: "2026-07-25T11:45:00+02:00",
    actual_ev_observed_until: "2026-07-25T12:00:00+02:00",
    actual_ev_timing_quality: "settlement_interval"
  })`,
  context,
);
assert.match(intervalTiming, /Energy measured during 11:45–12:00; exact start\/stop was not recorded/);

console.log("timeline EV history rendering: PASS");
