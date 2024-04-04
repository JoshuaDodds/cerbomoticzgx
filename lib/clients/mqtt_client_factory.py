import json
import threading
import time
import paho.mqtt.client as mqtt

from lib.constants import retrieve_mqtt_subcribed_topics, logging, DzEndpoints, cerboGxEndpoint, systemId0
from lib.domoticz_updater import domoticz_update

class VictronClient:
    """
    Usage:  victron_client = VictronClient().get_client()
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(VictronClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, client_id="victron_client", host=cerboGxEndpoint, keepalive=45, port=1883):
        # To prevent re-initialization if __init__ is called again
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True

        self.client_id = client_id
        self.host = host
        self.keepalive = keepalive
        self.port = port
        self.ka_thread = None
        self.client = self._configure_client()

    def get_client(self):
        """
        Returns the MQTT client instance.
        """
        return self.client

    def _configure_client(self):
        """
        Initializes and connects the MQTT client.
        """
        client = mqtt.Client(client_id=self.client_id, reconnect_on_failure=True)
        client.connect(host=self.host, keepalive=self.keepalive, port=self.port)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_log = None
        client.on_publish = None

        return client

    def _start_keepalive(self):
        def keepalive_loop():
            while not self._stop_event.is_set():
                try:
                    self.client.publish(topic=f"R/{systemId0}/keepalive")
                    self.client.publish(topic=f"R/{systemId0}/system/0/Serial",
                                        payload=json.dumps({"value": systemId0}))
                    logging.debug("Published Victron CerboGX keep-alive message to the victron mqtt broker.")
                except Exception as e:
                    logging.error(f"Failed to publish keep-alive message: {e}")
                time.sleep(30)  # Sleep for 30 seconds before next publish

        if self.ka_thread is None or not self.ka_thread.is_alive():
            self._stop_event = threading.Event()
            self.ka_thread = threading.Thread(target=keepalive_loop, daemon=True)
            self.ka_thread.start()
            logging.info(f"Victron MQTT Client Keep Alive thread started.")

    def _on_connect(self, _client, _userdata, _flags, _rc):
        logging.info(f"MQTT Client Re-Connect...")

        self._start_keepalive()

        for topic in retrieve_mqtt_subcribed_topics():
            if _client.subscribe(topic):
                logging.info(f"MQTT Client Subscribed to: {topic}")

    @staticmethod
    def _on_disconnect(_client, _userdata, _rc):
        if _rc == 0:
            logging.info("MQTT Client disconnected gracefully.")
        else:
            logging.info(f"MQTT Client disconnected unexpectedly. Return code: {_rc}, Reason: {mqtt.error_string(_rc)}")

    @staticmethod
    def _on_message(_client, _userdata, msg):
        from lib.event_handler import Event

        if msg and msg.payload:
            try:
                # grab topic and payload from message
                topic = msg.topic
                value = json.loads(msg.payload.decode("utf-8"))['value']
                # format a  logging message
                logmsg = f"{' '.join(topic.rsplit('/', 3)[1:3])}: {value}"
                logging.debug(logmsg)

                if topic and value:
                    # capture and dispatch events which should update Domoticz
                    if topic in DzEndpoints['system0']:
                        domoticz_update(topic, value, logmsg)

                    # capture and dispatch all events to the event handler
                    Event(topic, value, logmsg).dispatch()

            except Exception as E:
                logging.info(E)
