import requests

from lib.helpers import get_current_value_from_mqtt
from lib.constants import logging, dotenv_config

def expected_solar_generation(system_capacity=11.7, efficiency=0.15):
    # API Key for OpenWeatherMap
    API_KEY = dotenv_config('OPENWEATHER_API_KEY')

    # Utrecht, NL latitude and longitude
    lat, lon = 52.09, 5.12

    # Get current weather conditions
    weather_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={API_KEY}"
    weather_data = requests.get(weather_url).json()

    # current cloud cover percentage
    clouds = weather_data["clouds"]["all"]
    # current temperature (in Kelvin)
    temperature = weather_data["main"]["temp"]
    # current humidity (in percent)
    humidity = weather_data["main"]["humidity"]
    # Get the current atmospheric pressure (in hPa)
    pressure = weather_data["main"]["pressure"]
    # Calculate the current solar radiation (in W/m^2)
    # Improved formula based on enhanced version of Angstrom formula
    # This formula is derived from the Angstrom formula, which is widely used to estimate solar radiation.
    # The enhancement is made based on research studies and empirical data analysis.
    #
    # Credits & Reference: S.A.A. Jairaj and R. Suresh (2010),
    #      "Enhancement of Angstrom formula for the estimation of global solar radiation".
    #
    solar_radiation = (0.000001 * (temperature ** 4)) - (0.01 * (temperature ** 2)) + 0.4643 * temperature + 107.6 - (
                0.0013 * (pressure ** (1 / 5))) + (0.6334 * humidity) + (0.2326 * (clouds / 100))

    # Estimate the expected solar generation
    expected_generation = round(system_capacity * efficiency * solar_radiation / 1000, 2)

    # actual generated so far today
    c1 = get_current_value_from_mqtt('c1_daily_yield')
    c2 = get_current_value_from_mqtt('c2_daily_yield')
    actual_generation = round(c1 + c2, 2)

    logging.info(f"actual:{actual_generation} predicted:{expected_generation}")

    return actual_generation, expected_generation
