import json
import time
import paho.mqtt.client as mqtt

from datetime import datetime
from math import floor, ceil

from lib.helpers.base7_math import *
from lib.constants import Topics, logging, mosquittoEndpoint


def publish_message(topic, message=None, qos=0, retain=False, payload=None) -> None:
    """
    publishes a single message to a given topic on the MQtt broker
    """
    try:
        from lib.clients.mqtt_client_factory import VictronClient
        client = VictronClient().get_client()

        if payload is None:
            client.publish(topic=topic, payload=f"{{\"value\": \"{message}\"}}", qos=qos, retain=retain)
        else:
            client.publish(topic=topic, payload=payload, qos=qos, retain=retain)

    except Exception as e:
        logging.info(f"{e}", exc_info=True)


def get_current_value_from_mqtt(topic: str, timeout: float = 1.0, raw: bool = False) -> any:
    """
    Retrieves a single message from a given topic on the MQTT broker using a separate connection.
    If raw is True, it retrieves the raw message.
    """
    messages = []
    completed = False

    def on_connect(client, _userdata, _flags, _rc):
        """Subscribe to the topic upon connecting."""
        client.subscribe(topic)

    def on_message(client, _userdata, msg):
        """Handle incoming messages."""
        if raw:
            messages.append(msg.payload)
        else:
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                messages.append(payload.get('value'))
            except json.JSONDecodeError:
                pass
        client.disconnect()  # Disconnect after receiving the first message
        nonlocal completed
        completed = True

    # Initialize a new temporary MQTT client
    temp_client = mqtt.Client(client_id="helper-message-retrieval-client")
    temp_client.on_connect = on_connect
    temp_client.on_message = on_message

    # Connect to the broker
    temp_client.connect(mosquittoEndpoint, 1883, 60)
    temp_client.loop_start()

    # Wait for the message to arrive or for the timeout
    start_time = time.time()
    while time.time() - start_time < timeout and not completed:
        time.sleep(0.1)  # Short sleep to avoid busy waiting

    temp_client.loop_stop()
    temp_client.disconnect()

    return messages[0] if messages else None


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


def get_seasonally_adjusted_max_charge_slots(batt_soc: float, pv_production_remaining: float = 0.0) -> int:
    """
    Returns: (int): max number of 1 hour charge slots needed to top up the battery from the grid
    based on the current month. If optional pv_forecasted amount (in kWh) is passed, it adds this
    to the batt_soc.
    """

    if pv_production_remaining:
        # convert ov_forecasted from kWh  to a percentage assuming battery capacity of 40kWh
        pv_production_remaining = round((pv_production_remaining / 40) * 100, 2)

    current_month = datetime.now().month

    if current_month in [9, 10, 11, 12, 1, 2, 3]:
        return max(0, calculate_max_charge_slots_needed(batt_soc + pv_production_remaining))

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
