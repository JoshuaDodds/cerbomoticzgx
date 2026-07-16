from dotenv import dotenv_values

from lib.config_paths import env_path, secrets_path
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message

STATE = GlobalStateClient()


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
    current_env_values = dotenv_values(env_path())
    requested_value = current_env_values.get(env_variable)
    if requested_value is not None:
        publish_message(topic=f"Cerbomoticzgx/config/{env_variable}", message=requested_value, retain=True)
    return requested_value
