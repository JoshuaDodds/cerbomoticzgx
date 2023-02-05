from .base7_math import *
from ..constants import Topics

def topic_friendly_name(topic, system_id="system0"):
    """
    Convert a MQQT subscribed literal topic to its equivalent friendly string label
    """
    subscribed_topics = Topics[system_id]
    return next((k for k in subscribed_topics if subscribed_topics.get(k) == topic), None)

def convert_to_fractional_hour(minutes) -> str:
    """
    Converts a given number of minutes to fractional hours or minutes, in the format "x hr y min"

    Parameters:
    minutes (int): The number of minutes to convert.

    Returns:
    str: The converted number of minutes in the format 'x hr y min' or 'x min', depending on the value.
    """

    if minutes > 60:
        hours = int(minutes / 60)
        minutes = int(minutes % 60)
        return f"{hours} hr {minutes} min" if minutes != 0 else f"{hours} hr"
    else:
        return f"{minutes} min"
