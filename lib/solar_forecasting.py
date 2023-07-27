"""
This Python module is used to retrieve solar forecasting data from Victron's VRM Portal API.

It contains two main functions:
    1.  get_victron_solar_forecast: This function fetches solar forecasting data from the VRM Portal API. It authenticates with the API, sends a GET
        request to retrieve the data, processes the response, and updates the global state with the forecasted solar production for the current day.
        The calculated value is determined by querying the VRM API for forecasted solar production from now until sundown and adding that to any
        solar production that has already been produced today.  Currently, sunup and sundown is hardcoded with "Magic Numbers" which set these two
        value to 5AM dand 10PM respectively.

    2.  actual_solar_generation: This function retrieves the current solar power generation values for today from the Cerbo MQTT data bus and returns
        the combined sum from both solar charge controllers.

When run as a script, this module continuously calls the get_victron_solar_forecast function every two minutes, effectively updating the solar forecasting
data on a regular basis. The energy_broker module imports this module and instantiates a scheduler which runs the get_victron_solar_forecast function every
5 minutes.

This module relies on environment variables defined in the .env file
"""
import time
import pytz
import requests
from datetime import datetime
from urllib.parse import urlencode

from lib.constants import logging, dotenv_config
from lib.global_state import GlobalStateClient

STATE = GlobalStateClient()
TIMEZONE = pytz.timezone(dotenv_config('TIMEZONE'))
IDSITE = dotenv_config('VRM_SITE_ID')
LOGIN_URL = dotenv_config('VRM_LOGIN_URL')
LOGIN_DATA = {"username": dotenv_config('VRM_USER'), "password": dotenv_config('VRM_PASS')}
API_URL = dotenv_config('VRM_API_URL')


def get_victron_solar_forecast():
    now_tz = datetime.now(TIMEZONE)
    start_of_today, end_of_today = (int(now_tz.replace(hour=h, minute=0, second=0, microsecond=0).timestamp()) for h in
                                    [5, 22])
    now = int(now_tz.timestamp()) - 60

    try:
        response = requests.post(LOGIN_URL, json=LOGIN_DATA, timeout=5)
        token = response.json().get("token")
    except (requests.ConnectTimeout, requests.ConnectionError) as LoginError:
        logging.info(f"Connectivity issue to VRM Login endpoint: {LoginError}")
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
    }

    url = f"{API_URL}/installations/{IDSITE}/stats?{urlencode(params)}"
    logging.debug(f"Calling VRM API with URL: {url}")

    try:
        response = requests.get(url, headers=headers, timeout=5)
    except requests.ConnectTimeout or requests.ConnectionError as ApiError:
        # log error and let scheduler try again in 5 minutes
        logging.info(f"Connectivity issue to VRM API: {ApiError}")
        return None

    if response.status_code != 200:
        # log error and let scheduler try again in 5 minutes
        logging.info(f"Failed to retrieve data. Status code: {response.status_code}")
        return None

    data = response.json().get("records", [])

    try:
        solar_production_left = round(float(data['solar_yield_forecast'][0][1]), 2)
    except (ValueError, TypeError, IndexError, KeyError) as e:  # noqa
        # catch and log unexpected or missing data and let scheduler try again in 5 minutes
        logging.info(f"Unexpected data received from VRM Api. Received: '{data['solar_yield_forecast'][0][1]}' instead of float type value.")
        pass
        return None

    solar_production = actual_solar_generation() * 1000
    solar_forecast_kwh = round(solar_production_left + solar_production, 2)

    logging.debug(f"Solar_forecasting: retrieved and published daily pv forecast. Actual:{actual_solar_generation()} kWh Forecasted:{solar_forecast_kwh} kWh ToGo: {solar_production_left}")

    STATE.set('pv_projected_today', solar_forecast_kwh)

    return solar_forecast_kwh


def actual_solar_generation():
    c1 = STATE.get('c1_daily_yield')
    c2 = STATE.get('c2_daily_yield')

    actual_generation = round(c1 + c2, 2)

    return actual_generation


def main():
    while True:
        try:
            get_victron_solar_forecast()
            time.sleep(60 * 2)

        except Exception as UnexpectedError:
            logging.info(f"Error: {UnexpectedError}")

        except KeyboardInterrupt:
            print("\n")
            exit(0)


if __name__ == "__main__":
    main()
