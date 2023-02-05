import urllib3

from lib.helpers import get_topic_key
from lib.constants import DzEndpoints, logging, systemId0, mqtt_msg_value_conversion

http = urllib3.PoolManager(num_pools=10, maxsize=25)


def domoticz_update(topic, value, logmsg):
    # apply value conversions for domoticz if needed
    if mqtt_msg_value_conversion.get(get_topic_key(topic)):
        value = mqtt_msg_value_conversion.get(get_topic_key(topic))(value=value)

    if systemId0 in topic:
        try:
            _response = http.request('GET', f"{DzEndpoints['system0'][topic]}{value}")

            if _response.status == 200:
                logging.debug(f"dz_updater: {logmsg}")
            else:
                logging.info(f"Timeout while attempting to update Domoticz with data from {topic}.")

        except Exception as E:
            logging.info(f"dz_updater (ERROR): {E}")

    else:
        try:
            _response = http.request('GET', f"{DzEndpoints['vehicle0'][topic]}{value}")

            if _response.status == 200:
                logging.debug(f"dz_updater: {logmsg}")
            else:
                logging.info(f"Timeout while attempting to update Domoticz with data from {topic}.")

        except Exception as E:
            logging.info(f"dz_updater (ERROR): {E}")
