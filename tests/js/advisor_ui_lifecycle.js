// Behavioural Advisor/config UI regression tests executed against the real app.js.
const assert = require("assert");
const vm = require("vm");
const { loadDashboard } = require("./cold_load_smoke");

function freshDashboard() {
  const loaded = loadDashboard();
  const { context, document, window } = loaded;
  const streams = [];
  const fetches = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.closed = false;
      streams.push(this);
    }
    close() { this.closed = true; }
  }
  const fetchStub = async (url) => {
    fetches.push(String(url));
    return {
      ok: true,
      async json() {
        return { ok: true, schema: "advisor_chat_v1", messages: [] };
      },
    };
  };
  context.EventSource = FakeEventSource;
  window.EventSource = FakeEventSource;
  context.fetch = fetchStub;
  window.fetch = fetchStub;
  return { context, document, streams, fetches };
}

function submit(loaded, draft) {
  const input = loaded.document.querySelector("#advisor-q");
  const submitButton = loaded.document.querySelector("#advisor-submit");
  input.value = draft;
  let focusCount = 0;
  input.focus = () => { focusCount += 1; };
  const started = vm.runInContext("submitAdvisorQuestion()", loaded.context);
  return { input, submitButton, started, focusCount: () => focusCount };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function testDraftWaitsForAcceptanceAndSuccessfulFinishReloads() {
  const loaded = freshDashboard();
  const state = submit(loaded, "Why did the plan change?");

  assert.strictEqual(state.started, true);
  assert.strictEqual(state.input.value, "Why did the plan change?");
  assert.strictEqual(state.input.disabled, true);
  assert.strictEqual(state.submitButton.disabled, true);
  assert.strictEqual(loaded.streams.length, 1);
  assert.strictEqual(vm.runInContext("submitAdvisorQuestion()", loaded.context), false);
  assert.strictEqual(loaded.streams.length, 1, "busy guard must prevent duplicate streams");

  const stream = loaded.streams[0];
  if (stream.onopen) stream.onopen();
  assert.strictEqual(state.input.value, "Why did the plan change?",
    "transport open is not backend acceptance");

  stream.onmessage({ data: JSON.stringify({ type: "accepted" }) });
  assert.strictEqual(state.input.value, "");

  stream.onmessage({ data: JSON.stringify({
    type: "done",
    mode: "question",
    model: "claude-sonnet-5",
    auth: "cli",
    elapsed_s: 1,
  }) });
  await settle();

  assert.strictEqual(state.input.disabled, false);
  assert.strictEqual(state.submitButton.disabled, false);
  assert.ok(state.focusCount() >= 1);
  assert.ok(loaded.fetches.includes("/api/advisor/latest"));
}

function testSynchronousStreamStartFailureKeepsDraftAndUnlocksForm() {
  const loaded = freshDashboard();
  class BrokenEventSource {
    constructor() { throw new Error("EventSource unavailable"); }
  }
  loaded.context.EventSource = BrokenEventSource;
  const state = submit(loaded, "Do not lose this");

  assert.strictEqual(state.started, false);
  assert.strictEqual(state.input.value, "Do not lose this");
  assert.strictEqual(state.input.disabled, false);
  assert.strictEqual(state.submitButton.disabled, false);
  assert.ok(state.focusCount() >= 1);
}

async function testRejectedOrDisconnectedStartKeepsDraftAndReloadsLatest() {
  for (const terminal of ["error", "disconnect"]) {
    const loaded = freshDashboard();
    const state = submit(loaded, "Keep this draft");
    const stream = loaded.streams[0];
    if (stream.onopen) stream.onopen();

    if (terminal === "error") {
      stream.onmessage({ data: JSON.stringify({
        type: "error",
        error: "An advisor review is already running",
      }) });
    } else {
      stream.onerror();
    }
    await settle();

    assert.strictEqual(state.input.value, "Keep this draft");
    assert.strictEqual(state.input.disabled, false);
    assert.ok(state.focusCount() >= 1);
    assert.ok(loaded.fetches.includes("/api/advisor/latest"));
  }
}

function testSavedSourcesAndRunDetailsAreEscapedAndCollapsed() {
  const loaded = freshDashboard();
  const html = vm.runInContext(`renderAdvisorMessage({
    role: "assistant",
    text: "Safe answer",
    sources: [{locator: "<img src=x onerror=globalThis.pwned=1>"}],
    run_details: [
      {type: "stage", msg: "<script>globalThis.pwned=2</script>"},
      {type: "private_reasoning", msg: "must-not-render"}
    ]
  }, {})`, loaded.context);

  assert.ok(html.includes("Sources used"));
  assert.ok(html.includes("&lt;img src=x onerror=globalThis.pwned=1&gt;"));
  assert.ok(html.includes("&lt;script&gt;globalThis.pwned=2&lt;/script&gt;"));
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes("<script>globalThis.pwned"));
  assert.ok(!html.includes("must-not-render"));
  assert.ok(html.includes('<details class="advisor-run-details">'));
  assert.ok(!html.includes('<details class="advisor-run-details" open>'));
}

function testConfigValueEditorKeyboardActivation() {
  const loaded = freshDashboard();
  const listeners = {};
  const attrs = {};
  const valueControl = {
    setAttribute(name, value) { attrs[name] = String(value); },
    addEventListener(name, fn) { listeners[name] = fn; },
  };
  let activations = 0;
  vm.runInContext(
    "globalThis.__bindConfig = (control, activate) => makeConfigValueEditable(control, 'Advisor model', activate)",
    loaded.context,
  );
  loaded.context.__bindConfig(valueControl, () => { activations += 1; });

  assert.strictEqual(attrs.role, "button");
  assert.strictEqual(attrs.tabindex, "0");
  assert.strictEqual(attrs["aria-label"], "Edit Advisor model");

  listeners.click({});
  assert.strictEqual(activations, 1);
  let prevented = false;
  listeners.keydown({ key: "Enter", preventDefault() { prevented = true; } });
  assert.strictEqual(activations, 2);
  assert.strictEqual(prevented, true);
  listeners.keydown({ key: " ", preventDefault() {} });
  assert.strictEqual(activations, 3);
  listeners.keydown({ key: "Escape", preventDefault() {} });
  assert.strictEqual(activations, 3);
}

async function run() {
  await testDraftWaitsForAcceptanceAndSuccessfulFinishReloads();
  await testRejectedOrDisconnectedStartKeepsDraftAndReloadsLatest();
  testSynchronousStreamStartFailureKeepsDraftAndUnlocksForm();
  testSavedSourcesAndRunDetailsAreEscapedAndCollapsed();
  testConfigValueEditorKeyboardActivation();
  console.log("advisor UI lifecycle test: all assertions passed.");
}

run().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
