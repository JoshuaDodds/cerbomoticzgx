// The AC Loads card is the non-EV household view. The raw Victron AC-out total
// still contains the separately metered EV, so rendering must subtract it.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "frontend", "static", "js", "powerflow.js"),
  "utf8",
);

const nodes = new Map();
const node = (selector) => {
  if (!nodes.has(selector)) {
    nodes.set(selector, { textContent: "", setAttribute() {} });
  }
  return nodes.get(selector);
};
const box = {
  id: "powerflow",
  dataset: {},
  clientWidth: 1000,
  clientHeight: 700,
  addEventListener() {},
  dispatchEvent() { return true; },
  querySelector(selector) { return node(selector); },
  set innerHTML(value) { this.html = String(value); },
  get innerHTML() { return this.html || ""; },
};
const context = {
  console,
  CustomEvent: class {},
  document: { getElementById() { return box; } },
  window: {},
  ResizeObserver: class { observe() {} },
  requestAnimationFrame(fn) { fn(); return 1; },
  cancelAnimationFrame() {},
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(source, context, { filename: "powerflow.js" });

context.window.renderPowerFlow("powerflow", {
  connected: true,
  grid_w: 16000,
  batt_w: 0,
  pv_w: 0,
  load_w: 17000,
  load_l1: 5700,
  load_l2: 5650,
  load_l3: 5650,
  ev_w: 16000,
  ev_l1_a: 23,
  ev_l2_a: 23,
  ev_l3_a: 23,
  soc: 50,
}, {});

assert.strictEqual(node("#pf-house-big").textContent, "1.00 kW");
assert.strictEqual(node("#pf-house-l1").textContent, "367 W");
assert.strictEqual(node("#pf-house-l2").textContent, "317 W");
assert.strictEqual(node("#pf-house-l3").textContent, "317 W");

console.log("powerflow AC Loads card excludes separately metered EV power: PASS");
