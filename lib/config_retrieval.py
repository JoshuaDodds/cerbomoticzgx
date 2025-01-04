from dotenv import dotenv_values

from lib.global_state import GlobalStateClient
from lib.helpers import publish_message

STATE = GlobalStateClient()


def retrieve_setting(env_variable):
    # Load secret values once and cache them
    if not hasattr(retrieve_setting, "_secrets"):
        retrieve_setting._secrets = dotenv_values('.secrets')

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
    current_env_values = dotenv_values('.env')
    requested_value = current_env_values.get(env_variable)
    if requested_value is not None:
        publish_message(topic=f"Cerbomoticzgx/config/{env_variable}", message=requested_value, retain=True)
    return requested_value
