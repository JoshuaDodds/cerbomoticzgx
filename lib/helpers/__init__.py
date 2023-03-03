import json
import paho.mqtt.subscribe as subscribe
import paho.mqtt.publish as publish
from datetime import datetime

from lib.helpers.base7_math import *
from lib.constants import Topics, cerboGxEndpoint


def publish_message(topic, message, retain=False) -> None:
    """
    publishes a single message to a given topic on the MQtt broker
    """
    publish.single(topic=topic, payload=f"{{\"value\": \"{message}\"}}", qos=0, retain=retain, hostname=cerboGxEndpoint,
                   port=1883)


def get_current_value_from_mqtt(topic: str) -> any:
    """
    Retrieves a single message froma given topic on the MQTT broker
    """
    t = subscribe.simple(Topics['system0'][topic], qos=0, msg_count=1, hostname=cerboGxEndpoint, port=1883)
    return json.loads(t.payload.decode("utf-8"))['value']


def get_topic_key(topic, system_id="system0") -> str:
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


def get_seasonal_max_items() -> int:
    """
    Returns: (int): max number of 1 hour charge slots needed to top up the battery from the grid
    based on the current month.
    """
    if datetime.now().month in [10, 11, 12, 1]:
        return 3
    if datetime.now().month in [8, 9, 2]:
        return 2
    if datetime.now().month in [3]:
        return 1

    return 0


# Function Aliases
retrieve_message = get_current_value_from_mqtt
