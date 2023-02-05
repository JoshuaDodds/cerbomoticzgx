import json
import paho.mqtt.publish as publish
import paho.mqtt.subscribe as subscribe

from lib.constants import logging, cerboGxEndpoint, Topics, TopicsWritable, dotenv_config


float_voltage = float(dotenv_config('BATTERY_FLOAT_VOLTAGE'))
max_voltage = float(dotenv_config('BATTERY_ABSORPTION_VOLTAGE'))
battery_full_voltage = float(dotenv_config('BATTERY_FULL_VOLTAGE'))

def ac_power_setpoint(watts=None):
    if watts:
        _msg = f"{{\"value\": {watts}}}"
        logging.info(f"Victron Integration: Setting AC Power Set Point to: {watts} watts")
        publish.single(TopicsWritable['system0']['ac_power_setpoint'], payload=_msg, qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)

def minimum_ess_soc(percent: int = 10):
    if percent:
        _msg = f"{{\"value\": {percent}}}"
        logging.info(f"Setting battery sustain percent to: {percent}%")
        publish.single(TopicsWritable['system0']['minimum_ess_soc'], payload=_msg, qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)

def restore_default_battery_max_voltage():
    logging.info(f"Victron Integration: Restoring max charge voltage to {float_voltage}V before shutdown...")
    publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{float_voltage}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

def regulate_battery_max_voltage(ess_soc):
    """
    This logic is triggered by updates to the ess battery Soc topic on the cerbo GX
    :param ess_soc:
    :return: boolean
    """
    max_charge_voltage_msg = subscribe.simple(Topics["system0"]["max_charge_voltage"], hostname=cerboGxEndpoint, port=1883)
    current_max_charge_voltage = float(json.loads(max_charge_voltage_msg.payload.decode("utf-8"))['value'])

    try:
        if int(ess_soc) == int(dotenv_config('MINIMUM_ESS_SOC')) and current_max_charge_voltage != float_voltage:
            publish.single(TopicsWritable["system0"]["max_charge_voltage"], payload=f"{{\"value\": {float_voltage}}}", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
            logging.info(f"Victron Integration: Adjusting max charge voltage to {float_voltage}V due to battery SOC at {ess_soc}%")

            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{float_voltage}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        elif int(ess_soc) < int(dotenv_config('MINIMUM_ESS_SOC')) and current_max_charge_voltage != max_voltage:
            publish.single(TopicsWritable["system0"]["max_charge_voltage"], payload=f"{{\"value\": {max_voltage}}}", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
            logging.info(f"Victron Integration: Adjusting max charge voltage to {max_voltage}V due to battery SOC {ess_soc}% of {dotenv_config('MINIMUM_ESS_SOC')}%")

            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{max_voltage}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        elif int(ess_soc) >= int(dotenv_config('MAXIMUM_ESS_SOC')):
            publish.single(TopicsWritable["system0"]["max_charge_voltage"], payload=f"{{\"value\": \"{battery_full_voltage}\"}}", qos=0, retain=False, hostname=cerboGxEndpoint, port=1883)
            logging.info(f"Victron Integration: Adjusting max charge voltage to {battery_full_voltage} due to battery SOC reaching {dotenv_config('MAXIMUM_ESS_SOC')}% or higher")

            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{battery_full_voltage}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        else:
            logging.info(f"Victron Integration: No Action. Battery max charge voltage is appropriately set at {current_max_charge_voltage}V with ESS SOC at {ess_soc}%")
            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{current_max_charge_voltage}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        return True

    except Exception as E:
        logging.info(f"Victron Integration (error): {E}")
        return False

def is_grid_import_enabled():
    setpoint_msg = subscribe.simple(Topics['system0']['ac_power_setpoint'], qos=0, msg_count=1, hostname=cerboGxEndpoint, port=1883)
    mode_msg = subscribe.simple(Topics['system0']['inverter_mode'], qos=0, msg_count=1, hostname=cerboGxEndpoint, port=1883)
    ac_in_setpoint = json.loads(setpoint_msg.payload.decode("utf-8"))['value']
    inverter_mode = json.loads(mode_msg.payload.decode("utf-8"))['value']

    if ac_in_setpoint > 0.0 or inverter_mode == 1:
        publish.single(Topics["system0"]["grid_charging_enabled"], payload=f"{{\"value\": \"True\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        return True
    else:
        publish.single(Topics["system0"]["grid_charging_enabled"], payload=f"{{\"value\": \"False\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        return False
