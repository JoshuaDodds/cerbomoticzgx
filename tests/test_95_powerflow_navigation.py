import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERFLOW_JS = ROOT / "frontend" / "static" / "js" / "powerflow.js"
APP_CSS = ROOT / "frontend" / "static" / "css" / "app.css"
BEHAVIOUR_TEST = ROOT / "tests" / "js" / "powerflow_navigation.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_powerflow_navigation_event_contract():
    result = subprocess.run(
        ["node", str(BEHAVIOUR_TEST)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_powerflow_navigation_has_visible_pointer_and_keyboard_focus():
    powerflow = POWERFLOW_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert 'role="group" aria-label="Live power flow"' in powerflow
    assert ".powerflow .pf-navigable-card" in css
    assert "cursor: pointer" in css
    assert ".pf-navigable-card:focus-visible > rect" in css
