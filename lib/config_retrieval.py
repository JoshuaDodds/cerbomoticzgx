import os

from dotenv import dotenv_values

from lib.config_paths import env_path, secrets_path
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message

STATE = GlobalStateClient()


def _env_values():
    """Read the active env once per file revision.

    Optimizer construction requests many settings together. Re-parsing the full
    file for every key made an exact adaptive solve spend material scheduler time
    in dotenv rather than planning. Atomic dashboard writes change inode and/or
    mtime, so this cache retains hot-reload behavior without a polling delay.
    """
    path = env_path()
    try:
        stat = os.stat(path)
        revision = (path, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except OSError:
        revision = (path, None, None, None)
    if getattr(_env_values, "_revision", None) != revision:
        _env_values._values = dotenv_values(path)
        _env_values._revision = revision
    return getattr(_env_values, "_values", {})


def retrieve_setting(env_variable):
    # Load secret values once and cache them
    current_secrets_path = secrets_path()
    if (not hasattr(retrieve_setting, "_secrets")
            or getattr(retrieve_setting, "_secrets_path", None) != current_secrets_path):
        retrieve_setting._secrets = dotenv_values(current_secrets_path)
        retrieve_setting._secrets_path = current_secrets_path

    # Check .secrets first
    if env_variable in retrieve_setting._secrets: # noqa
        return retrieve_setting._secrets[env_variable] # noqa

    # then Check STATE
    try:
        state_value = STATE.get(env_variable)
        if state_value not in [None, 0, ""]:
            return state_value
    except Exception:
        pass

    # Dynamically fetch the latest value from .env and update config topic
    current_env_values = _env_values()
    requested_value = current_env_values.get(env_variable)
    if requested_value is not None:
        publish_message(topic=f"Cerbomoticzgx/config/{env_variable}", message=requested_value, retain=True)
    return requested_value
