const assert = require("assert");
const { loadDashboard } = require("./cold_load_smoke");

const { document, window } = loadDashboard();

function navigate(target) {
  document.dispatchEvent({ type: "powerflow:navigate", detail: { target } });
}

navigate("battery");
assert.strictEqual(document.body.dataset.appView, "battery");
assert.strictEqual(window.location.hash, "#battery");

navigate("victron");
assert.strictEqual(document.body.dataset.appView, "live");
assert.strictEqual(window.location.hash, "#live");

navigate("vehicle");
assert.strictEqual(document.body.dataset.appView, "ess");
assert.strictEqual(window.location.hash, "#ess/vehicle");
assert.strictEqual(
  document.querySelector("#tab-vehicle").classList.contains("active"),
  true,
);

const callsBeforeUnknown = window.history.calls.length;
navigate("not-allow-listed");
assert.strictEqual(window.history.calls.length, callsBeforeUnknown);
assert.strictEqual(document.body.dataset.appView, "ess");
assert.strictEqual(window.location.hash, "#ess/vehicle");

window.location.hash = "#ess/vehicle";
window.dispatchEvent({ type: "popstate" });
assert.strictEqual(document.body.dataset.appView, "ess");
assert.strictEqual(
  document.querySelector("#tab-vehicle").classList.contains("active"),
  true,
);

console.log("app Power Flow routing behavior: PASS");
