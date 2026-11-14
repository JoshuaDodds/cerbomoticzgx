const assert = require("assert");
const vm = require("vm");
const { loadDashboard } = require("./cold_load_smoke");

function element(action) {
  const classes = new Set();
  return {
    action,
    textContent: "",
    className: "",
    disabled: false,
    classList: {
      add: (name) => classes.add(name),
      toggle: (name, value) => value ? classes.add(name) : classes.delete(name),
      contains: (name) => classes.has(name),
    },
    getAttribute: (name) => name === "data-ev-charge" ? action : null,
    addEventListener() {},
  };
}

const start = element("start");
const stop = element("stop");
const status = element();
const observed = element();
const { context } = loadDashboard(({ document }) => {
  const originalGet = document.getElementById;
  const originalQueryAll = document.querySelectorAll;
  document.getElementById = (id) => ({
    "ev-charge-status": status,
    "ev-charge-observed": observed,
  })[id] || originalGet(id);
  document.querySelectorAll = (selector) => selector === "[data-ev-charge]"
    ? [start, stop] : originalQueryAll(selector);
});

async function run() {
  let resolveStart;
  const calls = [];
  vm.runInContext("lastLive = {connected: true, ev_w: 0, grid_charging_enabled: true};", context);
  context.fetch = (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return new Promise((resolve) => { resolveStart = resolve; });
  };

  const first = context.requestEvCharge("start");
  assert.match(status.textContent, /Sending start request/);
  assert.strictEqual(start.disabled, true);
  assert.strictEqual(stop.disabled, true);
  await context.requestEvCharge("start");
  assert.strictEqual(calls.length, 1, "duplicate Start must not send another POST");

  resolveStart({ ok: true, json: async () => ({ ok: true, enabled: true }) });
  await first;
  assert.match(status.textContent, /Start request accepted/);
  assert.strictEqual(start.disabled, true);
  assert.strictEqual(stop.disabled, false, "Stop must remain available after Start acceptance");
  assert.deepStrictEqual(calls[0], ["/api/control/ev-charge", { enabled: true }]);
  assert.match(status.textContent, /Grid assist is on/);

  vm.runInContext("lastLive = {connected: true, ev_w: 0, grid_charging_enabled: false}; renderEvChargeControl();", context);
  assert.match(status.textContent, /Grid assist is now off/);
  assert.match(status.textContent, /Stop clears the pending manual request/);

  vm.runInContext(`lastLive = {
    connected: true, ev_w: 0, grid_charging_enabled: true,
    ev_charge_requested: true, ev_charge_requested_at: 122,
    ev_charge_request_age_s: 10,
    ev_charge_decision: {
      intent: true, code: "grid_confirmation_pending", age_s: 0, updated_at: 123
    }
  }; renderEvChargeControl();`, context);
  assert.match(status.textContent, /waiting for vehicle telemetry/);

  vm.runInContext(`evChargeRequest.finishedAt = Date.now() - 60001;
    lastLive.ev_charge_request_age_s = 2 * 86400;
    renderEvChargeControl();`, context);
  assert.match(status.textContent, /Manual grid charging is enabled/);
  assert.strictEqual(start.disabled, false);

  vm.runInContext("lastLive.ev_charge_request_age_s = 10; lastLive.ev_charge_requested = false; renderEvChargeControl();", context);
  assert.match(status.textContent, /No manual charge request pending/);
  vm.runInContext("lastLive.ev_charge_requested = true; lastLive.ev_charge_decision.age_s = 181; renderEvChargeControl();", context);
  assert.match(status.textContent, /waiting for a current controller decision/);
  vm.runInContext("lastLive.ev_charge_decision.age_s = 0; lastLive.ev_charge_decision.updated_at = 121; renderEvChargeControl();", context);
  assert.match(status.textContent, /waiting for a current controller decision/);

  vm.runInContext("lastLive.ev_w = 3300; renderEvChargeControl();", context);
  assert.match(observed.textContent, /3.30 kW/);

  context.fetch = async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return { ok: true, json: async () => ({ ok: true, enabled: false }) };
  };
  await context.requestEvCharge("stop");
  assert.deepStrictEqual(calls[1], ["/api/control/ev-charge", { enabled: false }]);
  assert.match(status.textContent, /Stop request accepted/);
  assert.strictEqual(start.disabled, false);
  assert.strictEqual(stop.disabled, true);

  context.fetch = async () => ({
    ok: false,
    status: 409,
    json: async () => ({
      ok: false, code: "grid_assist_required",
      error: "Enable Grid assist before using manual Start.",
    }),
  });
  await context.requestEvCharge("start");
  assert.match(status.textContent, /Start blocked: Enable Grid assist/);
  assert.match(status.textContent, /No charge request was queued/);
  assert.strictEqual(start.disabled, false);
  assert.strictEqual(stop.disabled, false);

  context.fetch = async () => ({
    ok: false,
    json: async () => ({ ok: false, error: "service unavailable" }),
  });
  await context.requestEvCharge("start");
  assert.match(status.textContent, /Start request failed: service unavailable/);
  assert.strictEqual(start.disabled, false);
  assert.strictEqual(stop.disabled, false);

  let timeoutCallback;
  context.setTimeout = (callback) => { timeoutCallback = callback; return 1; };
  context.clearTimeout = () => {};
  context.fetch = () => new Promise(() => {});
  const timedOut = context.requestEvCharge("stop");
  assert.strictEqual(start.disabled, true);
  assert.strictEqual(stop.disabled, true);
  timeoutCallback();
  await timedOut;
  assert.match(status.textContent, /outcome is unknown/);
  assert.strictEqual(start.disabled, false);
  assert.strictEqual(stop.disabled, false);
  console.log("EV charge controls feedback and duplicate protection: PASS");
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
