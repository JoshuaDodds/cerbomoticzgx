from paho.mqtt import publish
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message
from lib.constants import logging, Topics, TopicsWritable, cerboGxEndpoint
from lib.config_retrieval import retrieve_setting

STATE = GlobalStateClient()
float_voltage = float(retrieve_setting('BATTERY_FLOAT_VOLTAGE'))
max_voltage = float(retrieve_setting('BATTERY_ABSORPTION_VOLTAGE'))
battery_full_voltage = float(retrieve_setting('BATTERY_FULL_VOLTAGE'))
battery_sustain_percent = float(retrieve_setting('BATTERY_SUSTAIN_PERCENT')) or 20

def ac_power_setpoint(watts: str = None, override_ess_net_mettering=True, silent: bool = False):
    # disable net metering overide whenever power setpoint returns to zero
    if watts == "0.0":
        publish_message(Topics['system0']['ess_net_metering_overridden'], message="False", retain=True)

    if watts:
        _msg = f"{{\"value\": {watts}}}"

        if override_ess_net_mettering:
            publish_message(Topics['system0']['ess_net_metering_overridden'], message="True", retain=True)

        STATE.set(key='ac_power_setpoint', value=f"{watts}")
        publish.single(TopicsWritable['system0']['ac_power_setpoint'], payload=_msg, qos=1, retain=True, hostname=cerboGxEndpoint, port=1883)

        if not silent:
            logging.info(f"Victron Integration: Set AC Power Set Point to: {watts} watts")

def set_minimum_ess_soc(percent: int = battery_sustain_percent):
    if percent:
        _msg = f"{{\"value\": {percent}}}"
        logging.info(f"Setting battery sustain percent to: {percent}%")
        publish.single(TopicsWritable['system0']['minimum_ess_soc'], payload=_msg, qos=1, retain=True, hostname=cerboGxEndpoint, port=1883)

def restore_default_battery_max_voltage():
    logging.info(f"Victron Integration: Restoring max charge voltage to {float_voltage}V before shutdown...")
    publish_message("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{float_voltage}\"}}", retain=True)

def regulate_battery_max_voltage(ess_soc):
    """
    This logic is triggered by updates to the ess battery Soc topic on the cerbo GX
    :param ess_soc:
    :return: boolean
    """
    current_max_charge_voltage = STATE.get("max_charge_voltage")

    try:
        if int(ess_soc) == float(retrieve_setting('MINIMUM_ESS_SOC')) and current_max_charge_voltage != float_voltage:
            publish.single(TopicsWritable["system0"]["max_charge_voltage"], payload=f"{{\"value\": {float_voltage}}}", qos=1, retain=False, hostname=cerboGxEndpoint, port=1883)
            logging.info(f"Victron Integration: Adjusting max charge voltage to {float_voltage}V due to battery SOC at {ess_soc}%")
            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{float_voltage}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        elif int(ess_soc) < float(retrieve_setting('MINIMUM_ESS_SOC')) and current_max_charge_voltage != max_voltage:
            publish.single(TopicsWritable["system0"]["max_charge_voltage"], payload=f"{{\"value\": {max_voltage}}}", qos=1, retain=False, hostname=cerboGxEndpoint, port=1883)
            logging.info(f"Victron Integration: Adjusting max charge voltage to {max_voltage}V due to battery SOC {ess_soc}% of {retrieve_setting('MINIMUM_ESS_SOC')}%")
            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{max_voltage}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        elif int(ess_soc) >= float(retrieve_setting('MAXIMUM_ESS_SOC')) and current_max_charge_voltage != float(retrieve_setting('BATTERY_FULL_VOLTAGE')):
            publish.single(TopicsWritable["system0"]["max_charge_voltage"], payload=f"{{\"value\": \"{battery_full_voltage}\"}}", qos=1, retain=False, hostname=cerboGxEndpoint, port=1883)
            logging.info(f"Victron Integration: Adjusting max charge voltage to {battery_full_voltage} due to battery SOC reaching {retrieve_setting('MAXIMUM_ESS_SOC')}% or higher")
            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{battery_full_voltage}\"}}", qos=1, retain=True, hostname=cerboGxEndpoint, port=1883)
            # when battery is full, return Minumum batt SOC (unless grid fails) to 20%
            set_minimum_ess_soc(20)

        else:
            logging.debug(f"Victron Integration: No Action. Battery max charge voltage is appropriately set at {current_max_charge_voltage}V with ESS SOC at {ess_soc}%")
            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{current_max_charge_voltage}\"}}", qos=1, retain=True, hostname=cerboGxEndpoint, port=1883)

        return True

    except Exception as E:
        logging.info(f"Victron Integration (error): {E}")
        return False
