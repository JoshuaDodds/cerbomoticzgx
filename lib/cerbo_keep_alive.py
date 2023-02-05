import threading

import paho.mqtt.publish as publish
from lib.constants import logging, systemId0, cerboGxEndpoint


def keep_cerbo_alive():
    try:
        t = threading.Timer(30.0, keep_cerbo_alive)
        t.daemon = True
        t.start()

        publish.single(topic=f"R/{systemId0}/system/0/Serial", payload=None, qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
        logging.debug("cerbo_keep_alive: Published CerboGX mosquitto broker keep-alive message.")

    except Exception as E:
        logging.info(E)
