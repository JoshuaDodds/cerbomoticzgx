import urllib3
import json

import paho.mqtt.client as mqtt

from lib.event_handler import Event
from lib.cerbo_keep_alive import keep_cerbo_alive
from lib.constants import retrieve_mqtt_subcribed_topics, logging, cerboGxEndpoint, DzEndpoints
from lib.domoticz_updater import domoticz_update

client = mqtt.Client()
http = urllib3.PoolManager()


def on_connect(_client, _userdata, _flags, _rc):
    logging.info(f"MQTT Client Re-Connect...")

    for topic in retrieve_mqtt_subcribed_topics():
        if client.subscribe(topic):
            logging.debug(f"MQTT Client Subscribed to: {topic}")

    if keep_cerbo_alive():
        logging.info(f"MQTT Client Keep Alive thread started.")


def on_disconnect(_client, _userdata, _rc):
    if _rc == 0:
        logging.debug("MQTT Client disconnected gracefully.")
    else:
        logging.debug(f"MQTT Client disconnected unexpectedly. Return code: {_rc}, Reason: {mqtt.connack_string(_rc)}")


def on_message(_client, _userdata, msg):
    if msg and msg.payload:
        try:
            # grab topic and payload from message
            topic = msg.topic
            value = json.loads(msg.payload.decode("utf-8"))['value']
            # format a  logging message
            logmsg = f"{' '.join(topic.rsplit('/', 3)[1:3])}: {value}"

            if topic and value:
                # capture and dispatch events which should update Domoticz
                if topic in DzEndpoints['system0']:
                    domoticz_update(topic, value, logmsg)

                # capture and dispatch all events to the event handler
                Event(topic, value, logmsg).dispatch()

        except Exception as E:
            logging.info(E)


def mqtt_start():
    try:
        logging.info(f"Starting mqtt_client")
        client.on_connect = on_connect
        client.on_message = on_message
        client.on_disconnect = on_disconnect
        client.on_log = None
        client.on_publish = None

        client.connect(cerboGxEndpoint, keepalive=30, port=1883)
        client.loop_forever()

    except Exception as E:
        logging.info(f"Mqtt Client (mqtt_start): Error - {E}")


def mqtt_stop():
    logging.info("Mqtt Client: Stopping...")
    client.loop_stop(force=True)
    client.disconnect()
