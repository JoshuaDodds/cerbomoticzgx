"""Earliest preflight: runtime configuration must cover the public template."""

import os
from pathlib import Path

from dotenv import dotenv_values


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ENV_PATH = REPOSITORY_ROOT / ".env.example"


def _runtime_env_path() -> Path:
    configured = os.environ.get("APP_ENV_PATH", ".env")
    path = Path(configured)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _env_keys(path: Path) -> set[str]:
    return set(dotenv_values(path))


def test_00_runtime_env_contains_every_example_key():
    """Fail early with the complete missing-key list, never config values."""
    runtime_path = _runtime_env_path()
    assert runtime_path.is_file(), (
        f"Runtime environment file does not exist: {runtime_path}"
    )

    missing = sorted(
        _env_keys(EXAMPLE_ENV_PATH) - _env_keys(runtime_path)
    )
    assert not missing, (
        f"{runtime_path} is missing {len(missing)} key(s) declared in "
        f"{EXAMPLE_ENV_PATH}:\n"
        + "\n".join(f"  - {key}" for key in missing)
    )
