import json
import re
import urllib3

from lib.helpers import get_topic_key
from lib.constants import DzEndpoints, logging, mqtt_msg_value_conversion, systemId0
from lib.config_retrieval import retrieve_setting

http = urllib3.PoolManager(num_pools=10, maxsize=25)


def domoticz_read_device(idx):
    """Read a single Domoticz device by IDX (best-effort, read-only).

    Returns the device dict (result[0]) or None. The base URL is derived from
    DZ_URL_PREFIX (the same host used for updates).
    """
    try:
        prefix = retrieve_setting('DZ_URL_PREFIX') or ''
        base = prefix.split('/json.htm')[0]
        if not base:
            return None
        url = f"{base}/json.htm?type=command&param=getdevices&rid={idx}"
        resp = http.request('GET', url, timeout=urllib3.Timeout(total=5.0))
        if resp.status != 200:
            return None
        payload = json.loads(resp.data.decode('utf-8'))
        results = payload.get('result') or []
        return results[0] if results else None
    except Exception as e:
        logging.debug(f"dz_read (idx {idx}) failed: {e}")
        return None


def domoticz_sun_times():
    """Return (sunrise, sunset) 'HH:MM' strings from Domoticz, or (None, None).

    Domoticz includes these at the top level of any getdevices response.
    """
    try:
        prefix = retrieve_setting('DZ_URL_PREFIX') or ''
        base = prefix.split('/json.htm')[0]
        if not base:
            return (None, None)
        resp = http.request('GET', f"{base}/json.htm?type=command&param=getSunRiseSet",
                            timeout=urllib3.Timeout(total=5.0))
        if resp.status != 200:
            return (None, None)
        payload = json.loads(resp.data.decode('utf-8'))
        return (payload.get('Sunrise'), payload.get('Sunset'))
    except Exception as e:
        logging.debug(f"dz_read sun times failed: {e}")
        return (None, None)


def domoticz_device_number(idx, fields=("Usage", "Data", "CounterToday")):
    """Return the leading numeric value from a Domoticz device's value fields.

    Domoticz formats values like "974 W" / "0.017 m3"; we parse the first number
    from the first present field. Returns None when unavailable.
    """
    dev = domoticz_read_device(idx)
    if not dev:
        return None
    for f in fields:
        v = dev.get(f)
        if v is None:
            continue
        m = re.search(r'-?\d+(?:\.\d+)?', str(v))
        if m:
            return float(m.group())
    return None

def handle_response(response, logmsg, topic):
    code = response.status or "Unknown"

    if code == 200:
        logging.debug(f"dz_updater: {logmsg} (HTTP: {code})")
    else:
        logging.info(f"Timeout while attempting to update Domoticz with data from {topic}. (HTTP: {code})")


def domoticz_update(topic, value, logmsg):
    # apply value conversions for domoticz if needed
    if mqtt_msg_value_conversion.get(get_topic_key(topic)):
        value = mqtt_msg_value_conversion.get(get_topic_key(topic))(value=value)

    # It's an update from the victron integration
    if systemId0 in topic:
        try:
            _response = http.request('GET', f"{DzEndpoints['system0'][topic]}{value}")
            handle_response(_response, logmsg, topic)

        except Exception as E:
            logging.info(f"dz_updater (ERROR): {E}")

    # It's an update from the Tesla integration
    else:
        try:
            _response = http.request('GET', f"{DzEndpoints['vehicle0'][topic]}{value}")
            handle_response(_response, logmsg, topic)

        except Exception as E:
            logging.info(f"dz_updater (ERROR): {E}")
