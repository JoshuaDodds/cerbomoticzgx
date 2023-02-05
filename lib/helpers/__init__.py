import json
import paho.mqtt.subscribe as subscribe

from lib.helpers.base7_math import *
from lib.constants import Topics, cerboGxEndpoint, systemId0


def get_topic_key(topic, system_id="system0"):
    """
    Retrieves the key name for a MQQT literal topic from the Topics dict() if one exists
    """
    try:
        subscribed_topics = Topics[system_id]
    except KeyError:
        return None

    return next((k for k in subscribed_topics if subscribed_topics.get(k) == topic), None)


def convert_to_fractional_hour(minutes: int) -> str:
    """
    Parameters:
    minutes (int):

    Returns:
    str: The converted number of minutes in the format 'x hr y min' or 'x min', depending on the value.
    """
    if type(minutes) is not int:    # we are not charging and minutes == "N/A"
        return minutes

    if minutes > 60:
        hours = int(minutes / 60)
        minutes = int(minutes % 60)
        return f"{hours} hr {minutes} min" if minutes != 0 else f"{hours} hr"
    else:
        return f"{minutes} min"


def get_current_value_from_mqtt(topic: str) -> any:
    t = subscribe.simple(Topics['system0'][topic], qos=0, msg_count=1, hostname=cerboGxEndpoint, port=1883)
    return json.loads(t.payload.decode("utf-8"))['value']
