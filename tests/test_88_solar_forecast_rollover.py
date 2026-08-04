"""Regression tests for midnight freshness of VRM/MPPT solar inputs."""
from datetime import datetime
from pathlib import Path
import sys

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import solar_forecasting  # noqa: E402


class DummyState:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def test_predawn_previous_day_mppt_yield_is_not_todays_actual(monkeypatch):
    # Observed 2026-07-18: MPPT daily-yield counters retained 39.12 kWh until
    # 02:15. Subtracting that from today's 25.85 kWh VRM forecast clamped the
    # optimizer's remaining PV to zero for more than two hours.
    state = DummyState({
        "c1_daily_yield": 20.0,
        "c2_daily_yield": 19.12,
        "sun_rise": "05:40",
    })
    monkeypatch.setattr(solar_forecasting, "STATE", state)
    now = pytz.timezone("Europe/Amsterdam").localize(datetime(2026, 7, 18, 2, 0))

    assert solar_forecasting.actual_solar_generation(now=now) == 0.0


def test_post_sunrise_current_day_yield_remains_usable(monkeypatch):
    state = DummyState({
        "c1_daily_yield": 2.2,
        "c2_daily_yield": 1.1,
        "sun_rise": "05:40",
    })
    monkeypatch.setattr(solar_forecasting, "STATE", state)
    now = pytz.timezone("Europe/Amsterdam").localize(datetime(2026, 7, 18, 10, 0))

    assert solar_forecasting.actual_solar_generation(now=now) == 3.3


def test_successful_vrm_forecast_is_stamped_with_local_dates(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"records": {
                "solar_yield_forecast": [[0, 25853.0], [0, 30100.0]],
                "vrm_consumption_fc": [],
            }}

    state = DummyState({"c1_daily_yield": 0.0, "c2_daily_yield": 0.0, "sun_rise": "05:40"})
    monkeypatch.setattr(solar_forecasting, "STATE", state)
    monkeypatch.setattr(solar_forecasting, "_vrm_auth_headers", lambda: {"x": "y"})
    monkeypatch.setattr(solar_forecasting.requests, "get", lambda *a, **k: Response())

    solar_forecasting.get_victron_solar_forecast()

    today = datetime.now(solar_forecasting.TIMEZONE).date()
    assert state.get("pv_projected_today_date") == today.isoformat()
    assert state.get("pv_projected_tomorrow_date") == (today + solar_forecasting.timedelta(days=1)).isoformat()
    assert state.get("pv_forecast_updated_at")
