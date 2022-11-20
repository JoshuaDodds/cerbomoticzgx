import urllib3

from .constants import DzEndpoints, logging, systemId0

http = urllib3.PoolManager(num_pools=10, maxsize=25)


def domoticz_update(topic, value, logmsg):
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
