"""Behavioural coverage for the cluster configuration sync helper."""

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "sync_conf.sh"


def _fake_rsync(tmp_path: Path, itemized_changes: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rsync = bin_dir / "rsync"
    rsync.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "cat <<'EOF'\n"
        f"{itemized_changes}"
        "EOF\n",
        encoding="utf-8",
    )
    rsync.chmod(0o755)
    return bin_dir


def _sync_project(tmp_path: Path) -> tuple[Path, Path]:
    """Build a self-contained sync source tree, matching GitHub Actions.

    CI deliberately has no real ``.env`` or ``.secrets`` checkout.  Push mode
    correctly refuses to run without those files, so this behavioural test must
    provide harmless stand-ins rather than accidentally relying on a developer's
    local credentials.
    """
    project = tmp_path / "project"
    project.mkdir()
    script = project / "sync_conf.sh"
    shutil.copy2(SYNC_SCRIPT, script)
    (project / ".env").write_text("TEST_ONLY=true\n", encoding="utf-8")
    (project / ".secrets").write_text("TEST_ONLY_SECRET=true\n", encoding="utf-8")
    (project / "data").mkdir()
    return script, project


def test_push_summary_groups_added_updated_and_metadata_only_paths(tmp_path):
    sync_script, project = _sync_project(tmp_path)
    fake_bin = _fake_rsync(
        tmp_path,
        "\n".join(
            (
                "<f+++++++++|data/history/new-day.ndjson",
                "<f.st......|data/weather/latest.json",
                ".f....og...|.secrets",
                ".d..t......|data/",
            )
        ),
    )
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(sync_script), "--push", "--dry-run"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Dry run — no files will be copied." in result.stdout
    assert "Added to the cluster (remote n1) (1):" in result.stdout
    assert "+ data/history/new-day.ndjson" in result.stdout
    assert "Updated in the cluster (remote n1) (1):" in result.stdout
    assert "~ data/weather/latest.json" in result.stdout
    assert "Metadata updated in the cluster (remote n1) (2):" in result.stdout
    assert "· .secrets" in result.stdout
    assert "· data/" in result.stdout


def test_pull_summary_identifies_local_destination_and_no_change_case(tmp_path):
    sync_script, project = _sync_project(tmp_path)
    fake_bin = _fake_rsync(tmp_path, "")
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(sync_script), "--pull", "--dry-run"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Pulling .env, .secrets, and data/" in result.stdout
    assert "No files need copying; both locations are already in sync." in result.stdout
