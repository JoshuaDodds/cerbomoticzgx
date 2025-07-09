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
from datetime import datetime, timedelta
from urllib.parse import urlencode

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.global_state import GlobalStateClient

STATE = GlobalStateClient()
TIMEZONE = pytz.timezone(retrieve_setting('TIMEZONE'))
IDSITE = retrieve_setting('VRM_SITE_ID')
LOGIN_URL = retrieve_setting('VRM_LOGIN_URL')
LOGIN_DATA = {"username": retrieve_setting('VRM_USER'), "password": retrieve_setting('VRM_PASS')}
API_URL = retrieve_setting('VRM_API_URL')


def get_consumption_readings():
    now_tz = datetime.now(TIMEZONE)
    start_of_today = int(now_tz.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_of_today = int((now_tz + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    try:
        response = requests.post(LOGIN_URL, json=LOGIN_DATA, timeout=5)
        token = response.json().get("token")
    except (requests.ConnectTimeout, requests.ConnectionError) as LoginError:
        logging.info(f"Connectivity issue to VRM Login endpoint: {LoginError}")
        return None

    if not token:
        logging.info("Failed to get the token for VRM Portal API access. Check login credentials.")
        return None

    headers = {
        'Content-Type': 'application/json',
        'x-authorization': f'Bearer {token}'
    }

    params = {
        'type': "consumption",
        'start': start_of_today,
        'end': end_of_today,
        'interval': "days"
    }

    url = f"{retrieve_setting('VRM_API_URL')}/installations/{retrieve_setting('VRM_SITE_ID')}/stats?{urlencode(params)}"
    logging.debug(f"Calling VRM consumption stats endpoint: {url}")

    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            logging.info(f"Failed to retrieve consumption stats: {resp.status_code}")
            return None

        data = resp.json()
        logging.debug(f"VRM Response: {data}")

        totals = data.get('totals', {})
        gc = totals.get('Gc', 0.0)
        bc = totals.get('Bc', 0.0)
        pc = totals.get('Pc', 0.0)

        total_wh = round((gc + bc + pc) * 1000, 2)  # return in Wh
        logging.debug(f"VRM Total Consumption: {total_wh}")
        return total_wh

    except requests.RequestException as e:
        logging.info(f"Error calling VRM consumption stats: {e}")
        return None


def get_victron_solar_forecast():
    # Note: deprecated because this only grabbed data from 0500 to 2200 in 24h period
    # now_tz = datetime.now(TIMEZONE)
    # start_of_today, end_of_today = (int(now_tz.replace(hour=h, minute=0, second=0, microsecond=0).timestamp()) for h in [5, 22])
    # now = int(now_tz.timestamp()) - 60
    now_tz = datetime.now(TIMEZONE)
    start_of_today = int(now_tz.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_of_today = int((now_tz + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    try:
        response = requests.post(LOGIN_URL, json=LOGIN_DATA, timeout=5)
        token = response.json().get("token")
    except (requests.ConnectTimeout, requests.ConnectionError) as LoginError:
        logging.info(f"Connectivity issue to VRM Login endpoint: {LoginError}")
        return

    if not token:
        logging.info("Failed to get the token for VRM Portal API access (solar_forecasting). Check login credentials in .env config file.")
        return None  # We will try again in 5 minutes when the scheduler invokes this method in a new thread again.

    headers = {
        'Content-Type': 'application/json',
        'x-authorization': f'Bearer {token}'
    }

    params = {
        'type': "forecast",
        "start": start_of_today,
        "end": end_of_today,
        "interval": "days",
    }

    url = f"{API_URL}/installations/{IDSITE}/stats?{urlencode(params)}"
    logging.debug(f"Calling VRM API with URL: {url}")

    try:
        response = requests.get(url, headers=headers, timeout=5)
    except (requests.ConnectTimeout, requests.ConnectionError, requests.exceptions.ReadTimeout) as ApiError:
        # log error and let scheduler try again in 5 minutes
        logging.info(f"Connectivity issue to VRM API: {ApiError}")
        return None

    if response.status_code != 200:
        if 500 <= response.status_code < 600:
            # log error and let scheduler try again in 5 minutes
            logging.info(f"Server error when retrieving data. Status code: {response.status_code}")
        else:
            logging.info(f"Failed to retrieve data. Status code: {response.status_code}")
        return None

    data = response.json().get("records", [])
    logging.debug(f"Full VRM stats response: {data}")

    if data:
        try:
            # VRM solar forecast data
            solar_production = actual_solar_generation() * 1000
            solar_production_left = round(float(data['solar_yield_forecast'][0][1]), 2) - solar_production
            solar_forecast_kwh = round(solar_production_left + solar_production, 2)

            logging.debug(
                f"Daily pv forecast: Actual:{actual_solar_generation()} Forecasted:{solar_forecast_kwh} kWh ToGo: {solar_production_left}")

            STATE.set('pv_projected_today', solar_forecast_kwh)
            STATE.set('pv_projected_remaining', solar_production_left)

            # VRM consumption forecast data
            try:
                consumption_wh_forecasted = round(float(data['vrm_consumption_fc'][0][1]), 2)
                consumption_wh_actual = round(get_consumption_readings(), 2)
                consumption_wh_remaining = consumption_wh_forecasted - consumption_wh_actual

                logging.debug(f"Consumption Today: {consumption_wh_actual} kWh Forecasted consumption remaining: {consumption_wh_remaining} kWh")

                STATE.set('consumption_total_projected', consumption_wh_forecasted)
                STATE.set('consumption_projected_remaining', consumption_wh_remaining)
                STATE.set('consumption_total_cumulative', consumption_wh_actual)

            except (ValueError, TypeError, IndexError, KeyError):
                logging.info("Consumption forecast data missing or malformed.")

            return solar_forecast_kwh

        except (ValueError, TypeError, IndexError, KeyError) as e:  # noqa
            logging.info(f"Unexpected or no data received from VRM API.")
            return None

    else:
        return None


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
