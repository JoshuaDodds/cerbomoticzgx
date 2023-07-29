import json
from math import floor, ceil
import paho.mqtt.subscribe as subscribe
import paho.mqtt.publish as publish
from datetime import datetime

from lib.helpers.base7_math import *
from lib.constants import Topics, cerboGxEndpoint, logging


def publish_message(topic, message, retain=False) -> None:
    """
    publishes a single message to a given topic on the MQtt broker
    """
    publish.single(topic=topic, payload=f"{{\"value\": \"{message}\"}}", qos=0, retain=retain, hostname=cerboGxEndpoint,
                   port=1883)

    # todo: remove below debug logging
    if topic == "True" or topic == "False":
        logging.info(f"{__name__} sent topic: {topic} with message payload of: {message}")


def get_current_value_from_mqtt(topic: str) -> any:
    """
    Retrieves a single message froma given topic on the MQTT broker
    """
    try:
        t = subscribe.simple(Topics['system0'][topic], qos=0, msg_count=1, hostname=cerboGxEndpoint, port=1883)
        return json.loads(t.payload.decode("utf-8"))['value']

    except KeyError as e: # noqa
        return None


def get_raw_message_from_mqtt(topic: str) -> any:
    """
    Retrieves a single message froma given topic on the MQTT broker
    """
    try:
        t = subscribe.simple(topic, qos=0, msg_count=1, hostname=cerboGxEndpoint, port=1883)
        # return json.loads(t.payload.decode("utf-8"))['value']
        return t

    except KeyError as e: # noqa
        return None


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


def calculate_max_charge_slots_needed(batt_soc: float) -> int:
    """
    This assumes for the installed system that it can charge 25% of ESS storage capacity in an hour based on
    the installed systems specifications.  With that in mind, this function take the current cbattery SOC
    and determines how many hours are needed to fill it to 100% based each hour representing a 25% increase in
    battery SOC.
    """
    return round((100 - (round(floor(batt_soc / 25) * 25))) / 25)


def calculate_max_discharge_slots_needed(capacity_for_sale: float) -> int:
    """
    This function calculates the number of maximum discharge slots needed (hours) to discharge the available batt
    capacity we have available for sale (derived from the limit we set on ess net metering)
    It assumes that each slot represents a 25% decrease in battery SOC.
    """
    return ceil(capacity_for_sale / 25)


def get_seasonally_adjusted_max_charge_slots(batt_soc: float) -> int:
    """
    Returns: (int): max number of 1 hour charge slots needed to top up the battery from the grid
    based on the current month.
    """
    current_month = datetime.now().month

    if current_month in [10, 11, 12, 1, 8, 9, 2, 3]:
        return calculate_max_charge_slots_needed(batt_soc)

    return 0


def reduce_decimal(value):
    value = str(value)

    if '.' in value:
        try:
            float_value = float(value)
            rounded_value = round(float_value, 4)
            return str(rounded_value)
        except ValueError:
            return value
    else:
        return value


# Function Aliases
retrieve_message = get_current_value_from_mqtt
retrieve_raw_message = get_raw_message_from_mqtt
