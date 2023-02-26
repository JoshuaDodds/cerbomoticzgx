import time
import requests
import datetime

from lib.helpers import get_current_value_from_mqtt
from lib.constants import logging, dotenv_config

API_KEY = dotenv_config('OPENWEATHER_API_KEY')
lat, lon = 52.09, 5.12


def live_forecast_expected_solar_generation(system_capacity=11.7, efficiency=0.14):
    weather_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={API_KEY}"
    weather_data = requests.get(weather_url).json()

    clouds = weather_data["clouds"]["all"]
    temperature = weather_data["main"]["temp"]
    humidity = weather_data["main"]["humidity"]
    pressure = weather_data["main"]["pressure"]

    # Calculate the expected solar radiation (in W/m^2)
    # Improved formula based on enhanced version of Angstrom formula which is widely used to estimate solar radiation.
    # Credits & Reference: S.A.A. Jairaj and R. Suresh (2010), "Enhancement of Angstrom formula for the estimation
    #   of global solar radiation".
    solar_radiation = (0.000001 * (temperature ** 4)) - (0.01 * (temperature ** 2)) + 0.4643 * temperature + 107.6 - (
                0.0013 * (pressure ** (1 / 5))) + (0.6334 * humidity) + (0.2326 * (clouds / 100))
    # Estimate the expected solar generation
    expected_generation = round(system_capacity * efficiency * solar_radiation / 1000, 2)
    return expected_generation


def daily_forecast_expected_solar_generation(system_capacity=11.7, efficiency=0.14):
    # Get 24-hour forecast data
    today_midnight = int(datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    forecast_url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={API_KEY}&cnt=24&dt={today_midnight}"
    # forecast_url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={API_KEY}&cnt=12"
    forecast_data = requests.get(forecast_url).json()

    total_clouds = 0
    total_temperature = 0
    total_humidity = 0
    total_pressure = 0

    # Get average cloud cover percentage, temperature, humidity, and pressure over 24 hours
    for hour in forecast_data["list"]:
        total_clouds += hour["clouds"]["all"]
        total_temperature += hour["main"]["temp"]
        total_humidity += hour["main"]["humidity"]
        total_pressure += hour["main"]["pressure"]

    num_hours = len(forecast_data["list"])
    clouds = total_clouds / num_hours
    temperature = total_temperature / num_hours
    humidity = total_humidity / num_hours
    pressure = total_pressure / num_hours

    # Calculate the expected solar radiation (in W/m^2)
    # Improved formula based on enhanced version of Angstrom formula which is widely used to estimate solar radiation.
    # Credits & Reference: S.A.A. Jairaj and R. Suresh (2010), "Enhancement of Angstrom formula for the estimation
    #   of global solar radiation".
    solar_radiation = (0.000001 * (temperature ** 4)) - (0.01 * (temperature ** 2)) + 0.4643 * temperature + 107.6 - (
                0.0013 * (pressure ** (1 / 5))) + (0.6334 * humidity) + (0.2326 * (clouds / 100))

    # Estimate the expected solar generation
    expected_generation = round(system_capacity * efficiency * solar_radiation / 1000, 2)

    return expected_generation

def actual_solar_generation():
    # actual generated so far today
    c1 = get_current_value_from_mqtt('c1_daily_yield')
    c2 = get_current_value_from_mqtt('c2_daily_yield')
    actual_generation = round(c1 + c2, 2)

    return actual_generation

def update_domoticz(actual, live):
    if actual and live:
        dz_url = f"http://dz-insecure.hs.mfis.net:80/json.htm?type=command&param=udevice&idx=634&nvalue=0&svalue={round(actual * 1000)};0;{round(live * 1000)};0;0;0"
        _dz_response = requests.get(dz_url)


if __name__ == "__main__":
    while True:
        try:
            actual_generated = actual_solar_generation()
            live_forecast = live_forecast_expected_solar_generation(system_capacity=11.7, efficiency=0.1998)
            daily_forecast = daily_forecast_expected_solar_generation(system_capacity=11.7, efficiency=0.1998)

            logging.info(f"Actual:{actual_generated} kWh Live Forecast:{live_forecast} kWh Daily Forecast: {daily_forecast} kWh")
            # update_domoticz(actual_generated, live_forecast)
            time.sleep(60 * 15)

        except Exception as e:
            logging.info(f"Error: {e}")
