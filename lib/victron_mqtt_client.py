from lib.constants import logging
from lib.clients.mqtt_client_factory import VictronClient

client = VictronClient().get_client()

def mqtt_start():
    try:
        logging.info(f"Starting Victron MQTT client")
        client.loop_forever()

    except Exception as E:
        logging.info(f"MQTT Client (mqtt_start): Error - {E}")


def mqtt_stop():
    logging.info("Mqtt Client: Stopping...")
    client.loop_stop(force=True)
    client.disconnect()
