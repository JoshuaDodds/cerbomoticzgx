import time
import pytz
import requests
from datetime import datetime
from urllib.parse import urlencode

from lib.constants import logging, dotenv_config
from lib.global_state import GlobalStateClient
from lib.helpers import get_current_value_from_mqtt

STATE = GlobalStateClient()
timezone = pytz.timezone(dotenv_config('TIMEZONE'))
idSite = dotenv_config('VRM_SITE_ID')
login_url = dotenv_config('VRM_LOGIN_URL')
login_data = {"username": dotenv_config('VRM_USER'), "password": dotenv_config('VRM_PASS')}
api_url = dotenv_config('VRM_API_URL')

def get_victron_solar_forecast():
    now_tz = datetime.now(timezone)
    start_of_today, end_of_today = (int(now_tz.replace(hour=h, minute=0, second=0, microsecond=0).timestamp()) for h in [5, 22])
    now = int(now_tz.timestamp()) - 60

    # Log in and get the token
    try:
        response = requests.post(login_url, json=login_data, timeout=5)
        token = response.json().get("token")
    except requests.ConnectTimeout or requests.ConnectionError as e:  # noqa
        logging.info("Connectivity issue to VRM Login endpoint...")
        return

    if not token:
        logging.info("Failed to get the token for VRM Portal API access (solar_forecasting). Check login credentials in .env config file.")
        return None

    headers = {
        'Content-Type': 'application/json',
        'x-authorization': f'Bearer {token}'
    }

    params = {
        'type': "forecast",
        "start": now,
        "end": end_of_today,
        "interval": "days",
        # "attributeCodes[]": 1221,  # see: https://www.victronenergy.com/live/venus-os:large#using_data_from_vrm
    }

    url = f"{api_url}/installations/{idSite}/stats?{urlencode(params)}"
    logging.debug(f"Calling VRM API with URL: {url}")

    try:
        response = requests.get(url, headers=headers, timeout=5)
    except requests.ConnectTimeout or requests.ConnectionError as e:  # noqa
        logging.info("Connectivity issue to VRM API...")

    if response.status_code != 200:
        logging.info(f"Failed to retrieve data. Status code: {response.status_code}")
        return None

    data = response.json().get("records", [])

    solar_production = actual_solar_generation() * 1000
    solar_production_left = round(data['solar_yield_forecast'][0][1], 2)
    solar_forecast_kwh = round(solar_production_left + solar_production, 2)

    logging.debug(f"Solar_forecasting: retrieved and published daily pv forecast. Actual:{actual_solar_generation()} kWh Forecasted:{solar_forecast_kwh} kWh ToGo: {solar_production_left}")

    STATE.set('pv_projected_today', solar_forecast_kwh)
    return solar_forecast_kwh

def actual_solar_generation():
    # c1 = STATE.get('c1_daily_yield')
    # c2 = STATE.get('c2_daily_yield')

    c1 = get_current_value_from_mqtt('c1_daily_yield')
    c2 = get_current_value_from_mqtt('c2_daily_yield')

    actual_generation = round(c1 + c2, 2)

    return actual_generation


if __name__ == "__main__":
    while True:
        try:
            get_victron_solar_forecast()
            time.sleep(60 * 2)

        except Exception as e:
            logging.info(f"Error: {e}")

        except KeyboardInterrupt:
            print(f"\n")
            exit(0)
