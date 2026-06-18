import tibber
import time
import requests
import json
import os

from datetime import datetime, timezone, timedelta
from dateutil import parser, tz

# Tibber GraphQL endpoint used for direct price queries (needed to request
# quarter-hourly resolution, which the tibber python library does not expose).
TIBBER_GQL_URL = "https://api.tibber.com/v1-beta/gql"

from lib.config_retrieval import retrieve_setting
from lib.constants import logging, systemId0
from lib.domoticz_updater import domoticz_update
from lib.clients.mqtt_client_factory import VictronClient
from gql.transport.exceptions import TransportClosed, TransportQueryError
from websockets.exceptions import ConnectionClosedError

logging.getLogger("gql.transport").setLevel(logging.ERROR)

tzinfos = {"UTC": tz.gettz(retrieve_setting('TIMEZONE'))}
account = tibber.Account(retrieve_setting('TIBBER_ACCESS_TOKEN'))
_home = account.homes[0]

client = VictronClient().get_client()

_PRICE_CACHE = {}
DEFAULT_PRICE_CACHE_PATH = "/dev/shm/cerbo_tibber_price_cache.json"

def live_measurements(home=_home or None):
    @home.event("live_measurement")
    async def log_accumulated(data):
        try:
            ts = datetime.now().replace(microsecond=0)
            logging.debug(f"Tibber: Imported: {data.accumulated_consumption or 0.000} kWh / {data.accumulated_cost or 0.00} {data.currency} :: "
                          f"Exported: {data.accumulated_production or 0.000} kWh / {data.accumulated_reward or 0.00} {data.currency} :: "
                          f"Pwr Factor: {data.power_factor or 0.000} :: Avg Pwr: {data.average_power} Watts")

            # update mqtt topics
            client.publish("Tibber/home/energy/day/imported", payload=f"{{\"value\": \"{data.accumulated_consumption}\"}}", retain=True)
            client.publish("Tibber/home/energy/day/cost", payload=f"{{\"value\": \"{data.accumulated_cost or 0.00}\"}}", retain=True)
            client.publish("Tibber/home/energy/day/exported", payload=f"{{\"value\": \"{data.accumulated_production}\"}}", retain=True)
            client.publish("Tibber/home/energy/day/reward", payload=f"{{\"value\": \"{data.accumulated_reward or 0.00}\"}}", retain=True)
            client.publish("Tibber/home/energy/day/import_peak", payload=f"{{\"value\": \"{data.max_power}\"}}", retain=True)
            client.publish("Tibber/home/energy/day/export_peak", payload=f"{{\"value\": \"{data.max_power_production}\"}}", retain=True)
            client.publish("Tibber/home/energy/day/average_power", payload=f"{{\"value\": \"{data.average_power}\"}}", retain=True)
            client.publish("Tibber/home/energy/day/last_update", payload=f"{{\"value\": \"{ts}\"}}", retain=True)

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

        except Exception as CallbackError:
            logging.info(f"tibber_api: Error encountered during live measurement data callback method log_accumulated(). Error: {CallbackError}")

    # Start the live feed. This runs forever unless a transport error occurs in which case we need to restart
    # in most cases to resolve this.
    logging.info(f"Tibber: Live measurements starting...")
    try:
        home.start_live_feed(user_agent=f"cerbomoticzgx/{retrieve_setting('VERSION')}",
                             retries=10,
                             retry_interval=10)
    except (TransportClosed, ConnectionClosedError, TransportQueryError) as e:
        # TransportQueryError covers Tibber refusing to start the live stream
        # (e.g. "unable to start stream ... for device") — typically a transient
        # Tibber-side issue or a stale/duplicate session. Route it through the
        # same supervised restart instead of letting it crash main().
        logging.warning(
            "Tibber Error: %s. It seems we have a network/connectivity issue. "
            "This can also be caused by a Tibber API outage or a stale live-feed "
            "session. Attempting a service restart...",
            e,
        )
        # this will trigger event_handler to restart the whole service
        client.publish("Cerbomoticzgx/system/shutdown", payload=f"{{\"value\": \"True\"}}", retain=True)


def dip_peak_data(caller=None, level="CHEAP", day=0, price_cap=0.22):
    """
    :param: str: level = "CHEAP", "EXPENSIVE", "NORMAL"
    :param: int: 0 = "today" or 1 = "tomorrow"
    """
    data = []

    _account = tibber.Account(retrieve_setting('TIBBER_ACCESS_TOKEN'))
    home = _account.homes[0]

    prices = home.current_subscription.price_info.today if day == 0 else home.current_subscription.price_info.tomorrow

    if not prices:
        return data

    for i in range(1, len(prices) + 1):
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
        _account = tibber.Account(retrieve_setting('TIBBER_ACCESS_TOKEN'))
        home = _account.homes[0]

        mqtt_publish_lowest_price_points(home)
        mqtt_publish_highest_price_points(home)
        mqtt_publish_current_price(home)

        # Publish all price points for AI optimizer. Accept any truthy form of
        # the flag ("1", "true", "yes", "on", "True") for consistency with the
        # EnergyBroker truthiness check.
        ai_flag = str(retrieve_setting('AI_POWERED_ESS_ALGORITHM') or "").strip().lower()
        if ai_flag in {"1", "true", "yes", "on"}:
            mqtt_publish_all_prices(home)

        # c = _account.websession.close()
        # c.close()
        # del home, _account

        logging.debug(f"Tibber: (called from {caller}): retrieved and published Tibber pricing data to mqtt bus.")

    except Exception as e:
        logging.error(f"Tibber: (publish_pricing_data) (Error): {e}")

def mqtt_publish_all_prices(home):
    """
    Publishes all available price points (today and tomorrow) to MQTT for the AI optimizer.
    """
    try:
        prices = []
        if home.current_subscription.price_info.today:
            prices.extend(home.current_subscription.price_info.today)
        if home.current_subscription.price_info.tomorrow:
            prices.extend(home.current_subscription.price_info.tomorrow)

        if prices:
            # Sort by time just in case, though usually they are sorted
            prices.sort(key=lambda x: x.starts_at)

            # Create a simplified list of dicts
            price_list = []
            for p in prices:
                 price_list.append({
                     "start": p.starts_at,
                     "total": p.total,
                     "level": p.level
                 })

            # Publish as a single JSON blob
            import json
            payload = json.dumps(price_list)
            client.publish("Tibber/home/price_info/all", payload=payload, qos=0, retain=True)
            logging.debug(f"Tibber: Published {len(price_list)} price points to Tibber/home/price_info/all")

    except Exception as e:
        logging.error(f"Tibber: Error publishing all prices: {e}")

def _price_cache_path(resolution: str) -> str:
    configured = retrieve_setting('TIBBER_PRICE_CACHE_PATH') or DEFAULT_PRICE_CACHE_PATH
    root, ext = os.path.splitext(configured)
    ext = ext or ".json"
    return f"{root}_{resolution}{ext}"


def _parse_price_start(value):
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return parser.parse(text, tzinfos=tzinfos)


def _normalise_price_points(points: list) -> list:
    normalised = []
    for p in points or []:
        start = p.get("start")
        try:
            start_iso = _parse_price_start(start).isoformat()
            normalised.append({
                "start": start_iso,
                "total": float(p["total"]),
                "level": p.get("level"),
            })
        except (KeyError, TypeError, ValueError):
            continue
    normalised.sort(key=lambda x: x["start"])
    return normalised


def _cache_price_points(resolution: str, points: list) -> None:
    """Persist the last good price horizon in memory and /dev/shm.

    This gives the optimizer a safe fallback when Tibber has a transient API
    timeout. Best-effort only: cache write failures must never block planning.
    """
    normalised = _normalise_price_points(points)
    if not normalised:
        return

    payload = {
        "resolution": resolution,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "points": normalised,
    }
    _PRICE_CACHE[resolution] = payload

    try:
        path = _price_cache_path(resolution)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as e:
        logging.debug("Tibber: could not write %s price cache: %s", resolution, e)


def _load_price_cache(resolution: str) -> dict | None:
    cached = _PRICE_CACHE.get(resolution)
    if cached:
        return cached

    try:
        with open(_price_cache_path(resolution)) as fh:
            cached = json.load(fh)
        if cached.get("resolution") == resolution:
            _PRICE_CACHE[resolution] = cached
            return cached
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return None


def _cached_price_points(resolution: str) -> list:
    """Return cached prices only when they still cover the current slot horizon."""
    cached = _load_price_cache(resolution)
    if not cached:
        return []

    points = _normalise_price_points(cached.get("points") or [])
    if not points:
        return []

    try:
        starts = [_parse_price_start(p["start"]) for p in points]
        starts.sort()
        now = datetime.now(starts[0].tzinfo)
        if len(starts) > 1:
            gaps = [(starts[i] - starts[i - 1]).total_seconds() for i in range(1, len(starts))]
            positive_gaps = [g for g in gaps if g > 0]
            slot_h = min(positive_gaps) / 3600.0 if positive_gaps else 0.25
        else:
            slot_h = 0.25 if resolution == "QUARTER_HOURLY" else 1.0

        # Need at least one slot whose window has not fully elapsed. Old cache
        # from yesterday must not drive today's optimizer.
        current_slot_cutoff = now - timedelta(hours=slot_h)
        if starts[-1] <= current_slot_cutoff:
            return []
        return points
    except (TypeError, ValueError) as e:
        logging.debug("Tibber: ignoring malformed %s price cache: %s", resolution, e)
        return []


def _fetch_price_points_graphql_once(resolution: str) -> list:
    """Query the Tibber GraphQL API directly for price points at the requested
    resolution.

    Tibber added ``priceInfo(resolution: QUARTER_HOURLY)`` (15-minute prices),
    which it bills on; the tibber python library does not expose this argument,
    so we issue the query ourselves. ``resolution`` is 'QUARTER_HOURLY' or
    'HOURLY'. Returns [] on any failure so the caller can fall back.
    """
    token = retrieve_setting('TIBBER_ACCESS_TOKEN')
    if not token:
        return []

    query = (
        "{ viewer { homes { currentSubscription { priceInfo("
        f"resolution: {resolution}"
        ") { today { total startsAt level } tomorrow { total startsAt level } } } } } }"
    )
    try:
        resp = requests.post(
            TIBBER_GQL_URL,
            json={"query": query},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning("Tibber: priceInfo(%s) query failed with HTTP %s", resolution, resp.status_code)
            return []

        payload = resp.json()
        if payload.get("errors"):
            logging.warning("Tibber: priceInfo(%s) returned GraphQL errors: %s", resolution, payload["errors"])
            return []

        homes = payload["data"]["viewer"]["homes"]
        price_info = homes[0]["currentSubscription"]["priceInfo"]

        points = []
        for bucket in ("today", "tomorrow"):
            for p in (price_info.get(bucket) or []):
                points.append({"start": p["startsAt"], "total": p["total"], "level": p.get("level")})
        return points

    except (requests.RequestException, KeyError, TypeError, ValueError, IndexError) as e:
        logging.warning("Tibber: priceInfo(%s) fetch error: %s", resolution, e)
        return []


def _fetch_price_points_graphql(resolution: str, attempts: int = 3, retry_delay_s: float = 2.0) -> list:
    """Fetch GraphQL prices with short retry/backoff for transient failures."""
    attempts = max(1, int(attempts or 1))
    for attempt in range(1, attempts + 1):
        points = _fetch_price_points_graphql_once(resolution)
        if points:
            _cache_price_points(resolution, points)
            return points
        if attempt < attempts:
            logging.info(
                "Tibber: priceInfo(%s) unavailable; retrying (%s/%s).",
                resolution, attempt + 1, attempts,
            )
            time.sleep(max(0.0, retry_delay_s))
    return []


def _get_all_price_points_via_library() -> list:
    """Fallback: hourly price points via the tibber python library."""
    try:
        _account = tibber.Account(retrieve_setting('TIBBER_ACCESS_TOKEN'))
        home = _account.homes[0]

        prices = []
        if home.current_subscription.price_info.today:
             prices.extend([{'start': p.starts_at, 'total': p.total, 'level': p.level} for p in home.current_subscription.price_info.today])
        if home.current_subscription.price_info.tomorrow:
             prices.extend([{'start': p.starts_at, 'total': p.total, 'level': p.level} for p in home.current_subscription.price_info.tomorrow])

        return prices
    except Exception as e:
        logging.error(f"Tibber: Error getting all prices: {e}")
        return []


def get_all_price_points():
    """
    Returns a list of all available price points (today and tomorrow) as dicts
    of {'start': ISO-8601 str, 'total': float, 'level': str}. Used by the AI
    optimizer.

    Prefers quarter-hourly (15-minute) prices via a direct GraphQL query, which
    is how Tibber bills as of October 2025. Configure with TIBBER_PRICE_RESOLUTION
    ('QUARTER_HOURLY' default, or 'HOURLY'). Falls back to hourly library data if
    the direct query yields nothing (e.g. market/home does not support it yet).
    """
    resolution = str(retrieve_setting('TIBBER_PRICE_RESOLUTION') or 'QUARTER_HOURLY').strip().upper()
    if resolution not in ('QUARTER_HOURLY', 'HOURLY'):
        resolution = 'QUARTER_HOURLY'

    points = _fetch_price_points_graphql(resolution)
    if points:
        return points

    if resolution == 'QUARTER_HOURLY':
        cached = _cached_price_points('QUARTER_HOURLY')
        if cached:
            logging.warning("Tibber: Quarter-hourly fetch failed; using cached quarter-hourly prices.")
            return cached

        # Try hourly via GraphQL before falling all the way back to the library.
        points = _fetch_price_points_graphql('HOURLY')
        if points:
            logging.warning("Tibber: Quarter-hourly prices unavailable and cache unusable; using hourly GraphQL prices.")
            return points

    logging.warning("Tibber: Falling back to hourly price points via the tibber library.")
    return _get_all_price_points_via_library()

def _current_quarter_hour_price():
    """Return the price of the 15-minute slot containing 'now', or None.

    Uses the same quarter-hourly feed the optimizer uses so the published
    'now' price matches the AI's view (the tibber library's current price is
    hourly).
    """
    try:
        points = get_all_price_points()
        if not points:
            return None
        parsed = []
        for p in points:
            start = p['start']
            if not isinstance(start, datetime):
                start = parser.parse(start, tzinfos=tzinfos)
            parsed.append((start, p['total']))
        parsed.sort(key=lambda x: x[0])
        now = datetime.now(parsed[0][0].tzinfo)
        current = None
        for start, total in parsed:
            if start <= now:
                current = total
            else:
                break
        return current
    except Exception as e:
        logging.debug(f"Tibber: could not derive 15-min current price: {e}")
        return None


def mqtt_publish_current_price(home):
    # Publish the current 15-minute price when available (matches the optimizer);
    # fall back to the library's hourly current price.
    value = _current_quarter_hour_price()
    if value is None:
        value = home.current_subscription.price_info.current.total
    client.publish("Tibber/home/price_info/now/total", payload=f"{{\"value\": \"{value}\"}}", qos=0, retain=True)

def current_price(home):
    price = home.current_subscription.price_info.current.total
    return price

def mqtt_publish_highest_price_points(home):
    # today
    if today_price_points(home, rank=-1) and today_price_points(home, rank=-3):
        logging.debug(f"Tibber: publishing today's highest price points to Mqtt broker...")
        client.publish("Tibber/home/price_info/today/highest/0/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/0/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/0/level", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/0/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=0)[3]}\"}}", qos=0, retain=True)

        client.publish("Tibber/home/price_info/today/highest/1/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/1/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/1/level", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/1/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=-1)[3]}\"}}", qos=0, retain=True)

        client.publish("Tibber/home/price_info/today/highest/2/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=-2)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/2/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=-2)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/2/level", payload=f"{{\"value\": \"{today_price_points(home, rank=-2)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/highest/2/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=-2)[3]}\"}}", qos=0, retain=True)

    # tomorrow
    if tomorrow_price_points(home, rank=-1) and tomorrow_price_points(home, rank=-3):
        logging.debug(f"Tibber: publishing tomorrow's highest price points to Mqtt broker...")
        client.publish("Tibber/home/price_info/tomorrow/highest/0/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/highest/0/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/highest/0/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/highest/0/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=0)[3]}\"}}", qos=0, retain=True)

        client.publish("Tibber/home/price_info/tomorrow/highest/1/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/highest/1/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/highest/1/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/highest/1/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=-1)[3]}\"}}", qos=0, retain=True)

def mqtt_publish_lowest_price_points(home):
    # today
    if today_price_points(home, rank=1) and today_price_points(home, rank=3):
        logging.debug(f"Tibber: publishing today's lowest price points to Mqtt broker...")
        client.publish("Tibber/home/price_info/today/lowest/0/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/0/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/0/level", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/0/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=1)[3]}\"}}", qos=0, retain=True)

        client.publish("Tibber/home/price_info/today/lowest/1/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/1/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/1/level", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/1/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=2)[3]}\"}}", qos=0, retain=True)

        client.publish("Tibber/home/price_info/today/lowest/2/hour", payload=f"{{\"value\": \"{today_price_points(home, rank=3)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/2/delta", payload=f"{{\"value\": \"{today_price_points(home, rank=3)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/2/level", payload=f"{{\"value\": \"{today_price_points(home, rank=3)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/today/lowest/2/cost", payload=f"{{\"value\": \"{today_price_points(home, rank=3)[3]}\"}}", qos=0, retain=True)

    # tomorrow
    if tomorrow_price_points(home, rank=1) and tomorrow_price_points(home, rank=2):
        logging.debug(f"Tibber: publishing tomorrow's lowest price points to Mqtt broker...")
        client.publish("Tibber/home/price_info/tomorrow/lowest/0/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/lowest/0/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/lowest/0/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/lowest/0/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=1)[3]}\"}}", qos=0, retain=True)

        client.publish("Tibber/home/price_info/tomorrow/lowest/1/hour", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[0]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/lowest/1/delta", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[1]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/lowest/1/level", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[2]}\"}}", qos=0, retain=True)
        client.publish("Tibber/home/price_info/tomorrow/lowest/1/cost", payload=f"{{\"value\": \"{tomorrow_price_points(home, rank=2)[3]}\"}}", qos=0, retain=True)

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

def lowest_48h_prices(price_cap=0.22, max_items=4):
    """
    Returns a list of the lowest 4 price data sets in the coming 48 hours

    :return: list: day, hour, level, price
    """
    _account = tibber.Account(retrieve_setting('TIBBER_ACCESS_TOKEN'))
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

    return relevant_data[0:max_items]

def lowest_24h_prices(price_cap=0.22, max_items=4):
    """
    Returns a list of the lowest 4 price data sets in the coming 24 hours

    :return: list: day, hour, level, price
    """
    _account = tibber.Account(retrieve_setting('TIBBER_ACCESS_TOKEN'))
    home = _account.homes[0]

    full_list = home.current_subscription.price_info.today

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

    return relevant_data[0:max_items]

def current_price_level(home):
    return home.current_subscription.price_info.current.level
