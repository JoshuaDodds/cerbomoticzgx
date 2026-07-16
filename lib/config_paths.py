"""Filesystem locations for runtime configuration files.

The Kubernetes deployment may mount the editable ``.env`` file somewhere other
than the application working directory. Keeping the lookup in one place ensures
the dashboard writer and the controller reader agree on the durable source.
"""
import os


def env_path() -> str:
    """Path to the writable runtime .env file."""
    return os.environ.get("APP_ENV_PATH") or ".env"


def secrets_path() -> str:
    """Path to the runtime secrets file."""
    return os.environ.get("APP_SECRETS_PATH") or ".secrets"
