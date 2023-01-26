from .base7_math import *
from ..constants import Topics

def topic_friendly_name(topic, system_id="system0"):
    """Convert a MQQT subscribed literal topic to its equivalent friendly string label"""
    subscribed_topics = Topics[system_id]
    return next((k for k in subscribed_topics if subscribed_topics.get(k) == topic), None)
