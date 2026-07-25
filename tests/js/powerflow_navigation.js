// Behaviour test for the route-neutral navigation contract exposed by powerflow.js.
// The production app decides what each target means; this test only verifies that
// desktop/mobile cards are complete accessible hit targets and emit one event.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "frontend", "static", "js", "powerflow.js"),
  "utf8",
);

class TestCustomEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.bubbles = Boolean(options.bubbles);
    this.detail = options.detail;
  }
}

function makeBox(width) {
  const listeners = {};
  const emitted = [];
  let html = "";
  const stubNode = { setAttribute() {}, textContent: "" };
  return {
    id: "powerflow",
    dataset: {},
    clientWidth: width,
    clientHeight: 700,
    listeners,
    emitted,
    addEventListener(type, handler) {
      (listeners[type] = listeners[type] || []).push(handler);
    },
    dispatchEvent(event) {
      emitted.push(event);
      return true;
    },
    querySelector(selector) {
      if (selector === "svg") return html.includes("<svg") ? stubNode : null;
      return stubNode;
    },
    get innerHTML() { return html; },
    set innerHTML(value) { html = String(value); },
  };
}

function eventFor(target, extra = {}) {
  let prevented = false;
  return {
    target: {
      closest(selector) {
        assert.strictEqual(selector, "[data-pf-navigation]");
        return target ? { dataset: { pfNavigation: target } } : null;
      },
    },
    repeat: false,
    preventDefault() { prevented = true; },
    get prevented() { return prevented; },
    ...extra,
  };
}

function liveData() {
  return {
    connected: true,
    grid_w: 0,
    batt_w: 0,
    pv_w: 0,
    load_w: 400,
    ev_w: 4,
    soc: 60,
  };
}

const boxes = {};
const context = {
  console,
  CustomEvent: TestCustomEvent,
  document: {
    getElementById(id) { return boxes[id] || null; },
  },
  window: {},
  ResizeObserver: class { observe() {} },
  requestAnimationFrame(fn) { fn(); return 1; },
  cancelAnimationFrame() {},
};
context.window.window = context.window;
context.window.CustomEvent = TestCustomEvent;
vm.createContext(context);
vm.runInContext(source, context, { filename: "powerflow.js" });

const box = makeBox(1000);
boxes.powerflow = box;
context.window.renderPowerFlow("powerflow", liveData(), {});

for (const [card, target, label] of [
  ["batt", "battery", "Open Battery details"],
  ["inv", "victron", "Open Victron details"],
  ["ev", "vehicle", "Open Vehicle details"],
]) {
  const open = `<g class="pf-navigable-card" data-pf-navigation="${target}" role="button" tabindex="0" focusable="true" aria-label="${label}">`;
  assert(
    box.innerHTML.includes(open) && box.innerHTML.indexOf(open) < box.innerHTML.indexOf(`id="pf-card-${card}"`),
    `${card} desktop card must be wrapped by an accessible navigation group`,
  );
}
assert(!box.innerHTML.includes('data-pf-navigation="grid"'), "Grid must remain observational");
assert.strictEqual(box.listeners.click.length, 1, "click delegation must bind once");
assert.strictEqual(box.listeners.keydown.length, 1, "keyboard delegation must bind once");

box.listeners.click[0](eventFor("battery"));
assert.strictEqual(box.emitted.length, 1);
assert.strictEqual(box.emitted[0].type, "powerflow:navigate");
assert.strictEqual(box.emitted[0].bubbles, true);
assert.strictEqual(box.emitted[0].detail.target, "battery");

const enter = eventFor("victron", { key: "Enter" });
box.listeners.keydown[0](enter);
assert.strictEqual(enter.prevented, true);
assert.strictEqual(box.emitted.at(-1).detail.target, "victron");

const space = eventFor("vehicle", { key: " " });
box.listeners.keydown[0](space);
assert.strictEqual(space.prevented, true);
assert.strictEqual(box.emitted.at(-1).detail.target, "vehicle");

const beforeIgnoredKeys = box.emitted.length;
box.listeners.keydown[0](eventFor("battery", { key: "ArrowDown" }));
box.listeners.keydown[0](eventFor("battery", { key: "Enter", repeat: true }));
box.listeners.click[0](eventFor(null));
const invalidCard = eventFor("not-a-route", { key: " " });
box.listeners.keydown[0](invalidCard);
assert.strictEqual(box.emitted.length, beforeIgnoredKeys, "irrelevant/repeated events must not navigate");
assert.strictEqual(invalidCard.prevented, false, "an unknown data attribute must retain normal keyboard behaviour");

// Rendering again must not multiply delegated handlers.
context.window.renderPowerFlow("powerflow", liveData(), {});
assert.strictEqual(box.listeners.click.length, 1);
assert.strictEqual(box.listeners.keydown.length, 1);

// A narrow rebuild uses the mobile card builder but retains the same semantic groups.
box.clientWidth = 500;
context.window.renderPowerFlow("powerflow", liveData(), {});
for (const target of ["battery", "victron", "vehicle"]) {
  assert(
    box.innerHTML.includes(`class="pf-navigable-card" data-pf-navigation="${target}"`),
    `${target} mobile card must remain navigable`,
  );
}

console.log("powerflow navigation test: desktop/mobile event contract passed.");
