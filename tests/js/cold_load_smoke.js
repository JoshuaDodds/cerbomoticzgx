// Minimal-stub cold-load smoke test for the dashboard's client-side scripts.
//
// Executes powerflow.js, charts.js, and app.js in that order (matching
// frontend/templates/index.html's <script> tag order) inside one shared global
// scope via Node's `vm` module -- exactly like a real browser loading the page.
// If any of them throw during synchronous top-level execution (the exact phase
// that runs before any user interaction, on every page load), this script exits
// non-zero and prints the error. See tests/test_frontend_js_smoke.py for why
// this exists.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC_JS = path.join(__dirname, "..", "..", "frontend", "static", "js");
const SCRIPTS = ["powerflow.js", "charts.js", "app.js"];

function makeElementStub(idHint) {
  const el = {
    id: idHint && idHint.startsWith("#") ? idHint.slice(1) : "",
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: {},
    children: [],
    childNodes: [],
    attributes: {},
    addEventListener() {},
    removeEventListener() {},
    appendChild(child) { el.childNodes.push(child); return child; },
    removeChild() {},
    setAttribute(name, value) { el.attributes[name] = value; },
    getAttribute(name) { return el.attributes[name] ?? null; },
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    focus() {},
    click() {},
    scrollTo() {},
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }),
  };
  Object.defineProperty(el, "innerHTML", { get() { return ""; }, set() {} });
  Object.defineProperty(el, "textContent", { get() { return ""; }, set() {} });
  return el;
}

function makeDocumentStub() {
  const body = makeElementStub();
  // IMPORTANT: selectors must resolve to a real (non-null) stub element, not null. The bug this
  // test exists to catch (see tests/test_frontend_js_smoke.py) only manifests once a function
  // like activateTab() gets PAST its "if (!panel) return" existence guard and reaches the line
  // that touches a not-yet-declared variable -- a null-returning stub would make every such
  // guard bail out early and silently hide the exact class of bug we're testing for. This
  // intentionally does NOT verify that index.html actually contains a matching element for
  // every selector app.js queries (tests/test_90_mobile_ux_static.py's static string checks are
  // the closer fit for that); it exists purely to run app.js's real control flow far enough to
  // catch use-before-declare / ordering bugs during synchronous startup.
  const bySelector = new Map();
  const stubFor = (key) => {
    if (!bySelector.has(key)) bySelector.set(key, makeElementStub(key));
    return bySelector.get(key);
  };
  return {
    body,
    documentElement: makeElementStub(),
    querySelector: (sel) => stubFor(sel),
    querySelectorAll: (sel) => [stubFor(sel + "#1"), stubFor(sel + "#2")],
    getElementById: (id) => stubFor("#" + id),
    createElement: () => makeElementStub(),
    createDocumentFragment: () => makeElementStub(),
    addEventListener() {},
    removeEventListener() {},
  };
}

function makeWindowStub() {
  const listeners = {};
  const win = {
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    removeEventListener() {},
    location: { hash: "", href: "http://localhost/", search: "" },
    history: { replaceState() {}, pushState() {} },
    EventSource: class { constructor() {} close() {} },
    ResizeObserver: class { observe() {} disconnect() {} unobserve() {} },
    fetch: () => Promise.reject(new Error("fetch stubbed out in cold-load smoke test")),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: () => 0,
    clearInterval() {},
    setTimeout: () => 0,
    clearTimeout() {},
    console,
  };
  return win;
}

function run() {
  const document = makeDocumentStub();
  const window = makeWindowStub();
  const context = {
    document,
    window,
    console,
    fetch: window.fetch,
    EventSource: window.EventSource,
    ResizeObserver: window.ResizeObserver,
    setInterval: window.setInterval,
    clearInterval: window.clearInterval,
    setTimeout: window.setTimeout,
    clearTimeout: window.clearTimeout,
    navigator: { userAgent: "node-cold-load-smoke-test" },
  };
  window.window = context;   // self-reference, some browser code does window.window
  context.self = context;
  vm.createContext(context);

  for (const name of SCRIPTS) {
    const filePath = path.join(STATIC_JS, name);
    const source = fs.readFileSync(filePath, "utf8");
    try {
      vm.runInContext(source, context, { filename: filePath });
    } catch (e) {
      console.error(`COLD-LOAD SMOKE TEST FAILED while executing ${name}:`);
      console.error(e && e.stack ? e.stack : e);
      process.exit(1);
    }
  }
  console.log("cold-load smoke test: all scripts executed without throwing.");
  process.exit(0);
}

run();
