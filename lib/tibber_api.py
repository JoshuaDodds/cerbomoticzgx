import tibber
import time

from paho.mqtt import publish
from datetime import datetime, timezone
from dateutil import parser, tz

from .constants import logging, cerboGxEndpoint, dotenv_config, systemId0
from .domoticz_updater import domoticz_update


logging.getLogger("gql.transport").setLevel(logging.ERROR)

tzinfos = {"UTC": tz.gettz(dotenv_config('TIMEZONE'))}
account = tibber.Account(dotenv_config('TIBBER_ACCESS_TOKEN'))
_home = account.homes[0]


def live_measurements(home=_home or None):
    @home.event("live_measurement")
    def log_accumulated(data):
        ts = datetime.now().replace(microsecond=0)
        logging.debug(f"Tibber: Imported: {data.accumulated_consumption or 0.000} kWh / {data.accumulated_cost or 0.00} {data.currency} :: "
                      f"Exported: {data.accumulated_production or 0.000} kWh / {data.accumulated_reward or 0.00} {data.currency} :: "
                      f"Pwr Factor: {data.power_factor or 0.000} :: Avg Pwr: {data.average_power} Watts")

        # update mqtt topics
        publish.single("Tibber/home/energy/day/imported", payload=f"{{\"value\": \"{data.accumulated_consumption}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/energy/day/cost", payload=f"{{\"value\": \"{data.accumulated_cost or 0.00}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/energy/day/exported", payload=f"{{\"value\": \"{data.accumulated_production}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/energy/day/reward", payload=f"{{\"value\": \"{data.accumulated_reward or 0.00}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/energy/day/import_peak", payload=f"{{\"value\": \"{data.max_power}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/energy/day/export_peak", payload=f"{{\"value\": \"{data.max_power_production}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/energy/day/average_power", payload=f"{{\"value\": \"{data.average_power}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/energy/day/last_update", payload=f"{{\"value\": \"{ts}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        # Update domoticz
        day_total = None
        if data.accumulated_cost and data.accumulated_reward:
            day_total = round(data.accumulated_reward - data.accumulated_cost, 2)
        if day_total:
            if day_total > 0.00:
                counter_for_dz = str(day_total).replace('.', '')
            else:
                counter_for_dz = str(0.00)
        else:
            counter_for_dz = str(0.00)

        domoticz_update(f"N/{systemId0}/Tibber/home/energy/day/euro_day_total", counter_for_dz, f"Tibber Total: {day_total}")

    # Start the live feed. This runs forever.
    logging.info(f"Tibber: Live measurements starting...")
    home.start_live_feed(user_agent="cerbomoticzgx/0.0.1")

def dip_peak_data(caller=None, level="CHEAP", day=0, price_cap=0.22):
    """
    :param: str: level = "CHEAP", "EXPENSIVE", "NORMAL"
    :param: int: 0 = "today" or 1 = "tomorrow"
    """
    data = []

    _account = tibber.Account(dotenv_config('TIBBER_ACCESS_TOKEN'))
    home = _account.homes[0]

    for i in range(1, 25):
        if day == 0:
            hour = today_price_points(home, i)
            if level in hour[2] and time.localtime()[3] <= today_price_points(home, i)[0].hour and hour[3] <= price_cap:
                logging.info(f"{caller}: Today: {hour[2]} at {hour[0]} for {hour[3]}")
                data.append(str(hour[0]).replace(":00:00", ""))

        if day == 1:
            hour = tomorrow_price_points(home, i)
            if level in hour[2] and hour[3] <= price_cap:
                logging.info(f"{caller}: Tomorrow: {hour[2]} at {hour[0]} for {hour[3]}")
                data.append(str(hour[0]).replace(":00:00", ""))

    return data

def publish_pricing_data(caller):
    try:
        _account = tibber.Account(dotenv_config('TIBBER_ACCESS_TOKEN'))
        home = _account.homes[0]

        mqtt_publish_lowest_price_points(home)
        mqtt_publish_highest_price_points(home)
        mqtt_publish_current_price(home)

        # c = _account.websession.close()
        # c.close()
        # del home, _account

        logging.info(f"Tibber: (called from {caller}): retrieved and published Tibber pricing data to mqtt bus.")

    except Exception as e:
        logging.error(f"Tibber: (publish_pricing_Data) (Error): {e}")

def mqtt_publish_current_price(home):
    value = home.current_subscription.price_info.current.total
    publish.single("Tibber/home/price_info/now/total", payload=f"{{\"value\": \"{value}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

def current_price(home):
    price = home.current_subscription.price_info.current.total
    return price

def mqtt_publish_highest_price_points(home):
    # today
    if today_price_points(home, rank=-1) and today_price_points(home, rank=-2):
        logging.debug(f"Tibber: publishing today's highest price points to Mqtt broker...")
        publish.single("Tibber/home/price_info/today/highest/0/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/highest/0/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/highest/0/level", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/highest/0/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        publish.single("Tibber/home/price_info/today/highest/1/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/highest/1/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/highest/1/level", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/highest/1/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    # tomorrow
    if tomorrow_price_points(home, rank=-1) and tomorrow_price_points(home, rank=-2):
        logging.debug(f"Tibber: publishing tomorrow's highest price points to Mqtt broker...")
        publish.single("Tibber/home/price_info/tomorrow/highest/0/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/highest/0/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/highest/0/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/highest/0/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        publish.single("Tibber/home/price_info/tomorrow/highest/1/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/highest/1/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/highest/1/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/highest/1/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

def mqtt_publish_lowest_price_points(home):
    # today
    if today_price_points(home, rank=1) and today_price_points(home, rank=2):
        logging.debug(f"Tibber: publishing today's lowest price points to Mqtt broker...")
        publish.single("Tibber/home/price_info/today/lowest/0/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/lowest/0/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/lowest/0/level", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/lowest/0/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        publish.single("Tibber/home/price_info/today/lowest/1/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/lowest/1/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/lowest/1/level", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/today/lowest/1/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

    # tomorrow
    if tomorrow_price_points(home, rank=1) and tomorrow_price_points(home, rank=2):
        logging.debug(f"Tibber: publishing tomorrow's lowest price points to Mqtt broker...")
        publish.single("Tibber/home/price_info/tomorrow/lowest/0/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/lowest/0/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/lowest/0/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/lowest/0/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

        publish.single("Tibber/home/price_info/tomorrow/lowest/1/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[0]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/lowest/1/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[1]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/lowest/1/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[2]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)
        publish.single("Tibber/home/price_info/tomorrow/lowest/1/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[3]}\"}}", qos=0, retain=True, hostname=cerboGxEndpoint, port=1883)

def tomorrow_price_points(home, rank=1):
    _tomorrow = home.current_subscription.price_info.tomorrow
    _index = rank - 1

    if _tomorrow:
        _sorted_by_price = sorted(_tomorrow, key=lambda hour: hour.total, reverse=False)
        _dto = parser.parse(_sorted_by_price[_index].starts_at, tzinfos=tzinfos)
        _level = _sorted_by_price[_index].level
        _cost = _sorted_by_price[_index].total
        _hour = _dto.time()
        _delta = _dto - datetime.now(timezone.utc).replace(microsecond=0)
        logging.debug(f"Tibber: Tomorrow's lowest pricing is at: {_hour} starting in {_delta}")
        return _hour, _delta, _level, _cost

    else:
        logging.debug("Tibber: Tomorrow's prices not yet published.")
        return "not_yet_published", "not_yet_published", "not_yet_published", "not_yet_published"

def today_price_points(home, rank=1):
    _today = home.current_subscription.price_info.today
    _index = rank - 1

    if _today:
        _sorted_by_price = sorted(_today, key=lambda hour: hour.total, reverse=False)
        _dto = parser.parse(_sorted_by_price[_index].starts_at, tzinfos=tzinfos)
        _level = _sorted_by_price[_index].level
        _cost = _sorted_by_price[_index].total
        _hour = _dto.time()
        _delta = _dto - datetime.now(timezone.utc).replace(microsecond=0)
        logging.debug(f"Tibber: Today's lowest pricing is at: {_hour} starting in {_delta}")

        return _hour, _delta, _level, _cost

def lowest_48h_prices(price_cap=0.22):
    """
    Returns a list of the lowest 4 price data sets in the coming 48 hours

    :return: list: day, hour, level, price
    """
    _account = tibber.Account(dotenv_config('TIBBER_ACCESS_TOKEN'))
    home = _account.homes[0]

    index = 0
    full_list = home.current_subscription.price_info.today
    if home.current_subscription.price_info.tomorrow:
        for item in home.current_subscription.price_info.tomorrow:
            full_list.append(item)

    _sorted_by_price = sorted(full_list, key=lambda hour: hour.total, reverse=False)

    relevant_data = []
    for item in _sorted_by_price:
        _day = 0 if parser.parse(item.starts_at, tzinfos=tzinfos).day == parser.parse(item.starts_at, tzinfos=tzinfos).today().day else 1
        _hour = parser.parse(item.starts_at, tzinfos=tzinfos).hour
        _level = item.level
        _price = item.total

        if parser.parse(item.starts_at, tzinfos=tzinfos) >= datetime.now(timezone.utc):
            if _price <= price_cap:
                logging.debug(f"Day: {_day} Hour: {_hour} Level: {_level} Price: {_price}")
                relevant_data.append([_day, _hour, _level, _price])

        index += 1

    return relevant_data[0:4]

def current_price_level(home):
    return home.current_subscription.price_info.current.level
