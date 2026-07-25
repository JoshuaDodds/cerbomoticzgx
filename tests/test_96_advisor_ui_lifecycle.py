import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BEHAVIOUR_TEST = ROOT / "tests" / "js" / "advisor_ui_lifecycle.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_advisor_ui_lifecycle_and_rendering_behaviour():
    result = subprocess.run(
        ["node", str(BEHAVIOUR_TEST)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        "Advisor UI lifecycle regression:\n\n"
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
