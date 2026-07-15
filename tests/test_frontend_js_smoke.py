"""Regression test for a real production outage (2026-07-15): a cold page load of the
dashboard threw `ReferenceError: Cannot access '_logsES' before initialization` and the
ENTIRE UI stayed stuck on "loading" forever, because activateTab() -- called synchronously
during page load via setAppView(appViewFromHash()) -- reached disconnectLogsStream(), which
referenced a `let` variable declared much further down the file. `let`/`const` bindings are
hoisted but stay in the "temporal dead zone" until their own declaration line executes, so
referencing one from code that runs earlier throws and aborts the whole script -- silently,
with no server-side symptom at all (the backend logs looked completely healthy).

This was missed in manual/browser testing during development because every test load used a
URL with a `#ess` hash fragment (for convenience, to land directly on a specific tab), which
happens to skip the exact branch (`view === "overview"` -> `activateTab("live")`) that a real,
bare-URL first visit hits on every single load.

tests/js/cold_load_smoke.js actually EXECUTES frontend/static/js/app.js's (and the two other
dashboard scripts') real top-level startup code inside a minimal stubbed DOM/window -- not just
a static read-the-source check -- so it exercises the real declaration-order semantics of the
language and catches this whole class of bug (any future `let`/`const` declared "too late" for
a function that can run during the synchronous startup call chain), not just a recurrence of
this one specific variable. Verified against both the original broken commit and the fix: fails
with the exact production stack trace on the former, passes cleanly on the latter.

Deliberately scoped: the stub DOM makes every `querySelector`/`getElementById`/`querySelectorAll`
call resolve to a generic non-null stub element (memoized per selector, with `.id` derived from
`#id` selectors) rather than a null/empty result OR a faithful parse of the real index.html.
That's intentional -- a null-returning stub would make every `if (!el) return` existence guard
bail out before reaching the code we're trying to exercise (this was tried first and silently
missed the bug entirely). This test does not verify that index.html actually contains a
matching element for everything app.js queries; tests/test_90_mobile_ux_static.py's static
string-order assertions are the tool for that. This test's only job is: does the startup code
run to completion without throwing.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "tests" / "js" / "cold_load_smoke.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_dashboard_scripts_execute_cleanly_on_cold_load():
    result = subprocess.run(
        ["node", str(SMOKE_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        "frontend/static/js/{powerflow,charts,app}.js threw during a simulated cold page "
        "load (see tests/js/cold_load_smoke.js and this file's module docstring for why this "
        f"test exists).\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
