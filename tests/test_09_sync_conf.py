"""Behavioural coverage for the cluster configuration sync helper."""

import os
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


def test_push_summary_groups_added_updated_and_metadata_only_paths(tmp_path):
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
        ["bash", str(SYNC_SCRIPT), "--push", "--dry-run"],
        cwd=ROOT,
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
    fake_bin = _fake_rsync(tmp_path, "")
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SYNC_SCRIPT), "--pull", "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Pulling .env, .secrets, and data/" in result.stdout
    assert "No files need copying; both locations are already in sync." in result.stdout
