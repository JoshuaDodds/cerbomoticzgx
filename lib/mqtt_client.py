import urllib3
import json

import paho.mqtt.client as mqtt

from .cerbo_keep_alive import keep_cerbo_alive
from .constants import retrieve_mqtt_subcribed_topics, logging, cerboGxEndpoint, SystemState, DzEndpoints, dotenv_config
from .domoticz_updater import domoticz_update
from .victron_integration import regulate_battery_max_voltage
from .tibber_api import publish_pricing_data
from .energy_broker import set_48h_charging_schedule

client = mqtt.Client()
http = urllib3.PoolManager()
global EvChargeControl


def on_connect(_client, _userdata, _flags, _rc):
    logging.info(f"MQTT Client Connected.")

    for topic in retrieve_mqtt_subcribed_topics():
        if client.subscribe(topic):
            logging.info(f"MQTT Client Subscribed to: {topic}")

    if keep_cerbo_alive():
        logging.info(f"MQTT Client Keep Alive thread started.")


def on_message(_client, _userdata, msg):
    if msg and msg.payload:
        try:
            topic = msg.topic
            value = json.loads(msg.payload.decode("utf-8"))['value']
            dummy_value = "1"
            logmsg = f"{' '.join(topic.rsplit('/', 3)[1:3])}: {value}"

            if topic and value:
                if topic in DzEndpoints['system0']:
                    #
                    # Handle messages which will update Domoticz
                    #
                    if "SystemState" in topic:
                        value = SystemState[value]
                    elif "Dc/0/Current" in topic:
                        value = round(value, 2)
                    elif "Pv/Power" in topic:
                        EvChargeControl.set_pv_watts(value)
                        value = f"{round(value)};{dummy_value}"
                    elif "Pv/Current" in topic:
                        EvChargeControl.set_pv_amps(value)
                        value = round(value)
                    elif "battery/277/Soc" in topic:
                        value = round(value, 2)
                        EvChargeControl.set_ess_soc(value)
                        if dotenv_config('VICTRON_OPTIMIZED_CHARGING') == '1':
                            regulate_battery_max_voltage(value)
                        if dotenv_config('TIBBER_UPDATES_ENABLED') == '1':
                            publish_pricing_data(__name__)
                    elif "battery/277/Dc/0/Voltage" in topic:
                        EvChargeControl.set_ess_volts(round(value, 2))
                    elif "acload/40/Ac/Power" in topic:
                        EvChargeControl.set_charging_watts(value)
                        value = f"{round(value)};{dummy_value}"
                    elif "battery/277/Dc/0/Power" in topic:
                        EvChargeControl.set_ess_watts(round(value))
                        value = f"{round(value)};{dummy_value}"

                    domoticz_update(topic, value, logmsg)

                #
                # Handle other specific messages
                #
                elif "acload/40/Ac/L1/Current" in topic:
                    EvChargeControl.set_l1_charging_amps(value)
                elif "acload/40/Ac/L2/Current" in topic:
                    EvChargeControl.set_l2_charging_amps(value)
                elif "acload/40/Ac/L3/Current" in topic:
                    EvChargeControl.set_l3_charging_amps(value)
                elif "Ac/Out/P" in topic:
                    EvChargeControl.set_acload_watts(value)
                elif "Ac/ActiveIn/P" in topic:
                    EvChargeControl.set_acin_watts(value)
                # elif "battery/277/Dc/0/Power" in topic:
                #     value = round(value)
                #     EvChargeControl.set_ess_watts(value)
                elif "SystemSetup/MaxChargeVoltage" in topic:
                    value = float(value)
                    EvChargeControl.set_ess_max_charge_voltage(value)
                elif "settings/grid_charging_enabled" in topic:
                    _val = value == "True"
                    EvChargeControl.set_grid_charging_enabled(_val)
                elif "EnergyBroker/RunTrigger" in topic:
                    set_48h_charging_schedule(__name__)
                #
                # default handling of messages not matching any of the conditions above
                #
                else:
                    logging.info(f"MQTT Client (topic update): {topic} {logmsg}")

        except Exception as E:
            logging.info(E)


def on_publish(_client, _obj, _msg):
    # not yet implemented
    pass
    # logging.info(f"{_msg}")


def on_log(_client, _obj, _level, _msg):
    # not yet implemented
    pass
    # logging.debug(f"{_msg}")


def mqtt_start(evcharge_control):
    global EvChargeControl
    EvChargeControl = evcharge_control

    try:
        logging.info(f"Starting mqtt_client")
        client.on_connect = on_connect
        client.on_publish = on_publish
        client.on_message = on_message
        client.on_log = on_log

        client.connect(cerboGxEndpoint, 1883)
        client.loop_forever()

    except Exception as E:
        logging.info(f"Mqtt Client (mqtt_start): Error - {E}")


def mqtt_stop():
    logging.info("Mqtt Client: Stopping...")
    client.loop_stop(force=True)
    client.disconnect()
