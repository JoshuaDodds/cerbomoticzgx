import math

from paho.mqtt import publish
from lib.global_state import GlobalStateClient
from lib.helpers import publish_message
from lib.constants import logging, Topics, TopicsWritable, cerboGxEndpoint
from lib.config_retrieval import retrieve_setting

STATE = GlobalStateClient()
float_voltage = float(retrieve_setting('BATTERY_FLOAT_VOLTAGE'))
max_voltage = float(retrieve_setting('BATTERY_ABSORPTION_VOLTAGE'))
battery_full_voltage = float(retrieve_setting('BATTERY_FULL_VOLTAGE'))

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

def limit_grid_feed_in(enabled: bool, watts: int = 0):
    """Toggle the Victron "Limit system feed-in" ESS setting (MaxFeedInPower).

    :param enabled: True limits feed-in to ``watts`` (default 0W); False restores
                    unlimited feed-in by writing -1.
    :param watts: feed-in limit in Watts to apply when ``enabled`` is True.

    The write is idempotent: it only publishes to the broker when the desired
    state differs from the last applied state recorded in global state. This
    avoids hammering the dbus/MQTT bus on a critical system when the optimizer
    runs frequently.
    """
    # Venus OS stores MaxFeedInPower as a float (W); -1.0 disables the limit.
    # Emit a float to exactly match the value type the dbus/MQTT bus expects.
    desired_value = float(watts) if enabled else -1.0
    desired_state = f"limited:{int(watts)}" if enabled else "unlimited"

    last_state = STATE.get('feed_in_limit_state')
    if last_state == desired_state:
        return

    try:
        _msg = f"{{\"value\": {desired_value}}}"
        publish.single(
            TopicsWritable['system0']['max_feed_in_power'],
            payload=_msg, qos=1, retain=False, hostname=cerboGxEndpoint, port=1883,
        )
        STATE.set('feed_in_limit_state', desired_state)
        STATE.set('max_feed_in_power', desired_value)

        if enabled:
            logging.info(f"Victron Integration: Limiting system grid feed-in to {watts}W (negative price protection).")
        else:
            logging.info("Victron Integration: Restored unlimited system grid feed-in.")
    except Exception as e:
        logging.error(f"Victron Integration: Failed to set grid feed-in limit ({desired_state}): {e}")


def configured_victron_min_soc_limit():
    """Return the independent Victron hard minimum SoC, or ``None`` if invalid.

    This is deliberately separate from the optimizer's seasonal reserve. Victron
    enters Recharge when actual SoC is below ``MinimumSocLimit``; mirroring a 40%
    winter planning reserve here would therefore cause an immediate unscheduled
    grid charge instead of letting the optimizer choose the cheapest window.

    Missing settings default to zero so existing installations clear any stale
    seasonal value after upgrade. Explicit malformed/out-of-range settings are
    rejected rather than silently issuing a surprising safety-critical command.
    """
    raw = retrieve_setting('VICTRON_HARDWARE_MIN_SOC')
    if raw in (None, '', 'None'):
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logging.error(
            "Victron Integration: Invalid VICTRON_HARDWARE_MIN_SOC %r; "
            "leaving MinimumSocLimit unchanged.",
            raw,
        )
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        logging.error(
            "Victron Integration: VICTRON_HARDWARE_MIN_SOC must be finite and "
            "between 0 and 100 (received %r); leaving MinimumSocLimit unchanged.",
            raw,
        )
        return None
    return int(round(value))


def set_minimum_ess_soc(percent=None, *, force=False) -> bool:
    """Apply Victron's independent hard ``MinimumSocLimit``.

    Idempotence is based on the actual subscribed Victron topic
    (``minimum_ess_soc``), never only on our last-command shadow. GlobalState
    returns numeric zero for a missing key, so an explicit ``has`` check is
    essential when the desired value is zero after a restart.

    Returns ``True`` when a write was published and ``False`` when the observed
    Victron value already matched or the requested value was invalid.
    """
    if percent is None:
        percent = configured_victron_min_soc_limit()
        if percent is None:
            return False

    try:
        numeric = float(percent)
    except (TypeError, ValueError):
        logging.error(
            "Victron Integration: Refusing invalid minimum SoC value %r.", percent)
        return False
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 100.0:
        logging.error(
            "Victron Integration: Refusing out-of-range minimum SoC value %r.",
            percent,
        )
        return False
    percent = int(round(numeric))

    observed_available = False
    try:
        observed_available = bool(STATE.has('minimum_ess_soc'))
    except (AttributeError, TypeError):
        # Without presence semantics, zero cannot be distinguished from missing;
        # publishing the idempotent setting is safer than leaving a stale floor.
        observed_available = False
    if not force and observed_available:
        try:
            observed_numeric = float(STATE.get('minimum_ess_soc'))
            observed = (
                int(round(observed_numeric))
                if math.isfinite(observed_numeric)
                else None
            )
        except (TypeError, ValueError, OverflowError):
            observed = None
        if observed == percent:
            STATE.set('min_ess_soc_applied', percent)
            return False

    _msg = f"{{\"value\": {percent}}}"
    logging.info(
        "Victron Integration: Setting independent hardware minimum SoC limit to: %s%%",
        percent,
    )
    publish.single(TopicsWritable['system0']['minimum_ess_soc'], payload=_msg, qos=1, retain=True, hostname=cerboGxEndpoint, port=1883)
    STATE.set('min_ess_soc_applied', percent)
    return True

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
            # On full charge, re-assert the independently configured Victron
            # safety floor. Seasonal planning reserves remain optimizer-only.
            set_minimum_ess_soc()

        else:
            logging.debug(f"Victron Integration: No Action. Battery max charge voltage is appropriately set at {current_max_charge_voltage}V with ESS SOC at {ess_soc}%")
            publish.single("Tesla/vehicle0/solar/ess_max_charge_voltage", payload=f"{{\"value\": \"{current_max_charge_voltage}\"}}", qos=1, retain=True, hostname=cerboGxEndpoint, port=1883)

        return True

    except Exception as E:
        logging.info(f"Victron Integration (error): {E}")
        return False
