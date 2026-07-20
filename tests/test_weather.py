import json
from datetime import datetime, timedelta, timezone


def test_open_meteo_provider_parses_hourly_payload(monkeypatch):
    from lib import weather

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "timezone": "Europe/Amsterdam",
                "hourly": {
                    "time": ["2026-06-28T10:00"],
                    "temperature_2m": [28.5],
                    "apparent_temperature": [30.0],
                    "cloud_cover": [25],
                    "precipitation": [0.1],
                    "wind_speed_10m": [12.0],
                    "shortwave_radiation": [650],
                    "global_tilted_irradiance": [720],
                    "direct_normal_irradiance": [500],
                    "diffuse_radiation": [110],
                },
            }).encode("utf-8")

    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        return Response()

    monkeypatch.setattr(weather.request, "urlopen", fake_urlopen)

    rows = weather.OpenMeteoProvider().fetch(
        lat=52.0,
        lon=5.0,
        tilt=35.0,
        azimuth=180.0,
        forecast_days=2,
    )

    assert "api.open-meteo.com/v1/forecast" in captured["url"]
    assert "latitude=52.0" in captured["url"]
    assert rows[0]["temp_c"] == 28.5
    assert rows[0]["gti_wm2"] == 720
    assert rows[0]["cloud_pct"] == 25


def test_weather_summary_uses_symmetric_degree_days():
    from lib import weather

    start = datetime(2026, 6, 28, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"time": (start + timedelta(hours=i)).isoformat(), "temp_c": temp, "gti_wm2": gti, "cloud_pct": 10}
        for i, (temp, gti) in enumerate([(28.0, 600), (18.0, 0), (24.0, 300), (21.0, 0)])
    ]

    summary = weather.summarize_forecast(
        rows,
        comfort_low=21.0,
        comfort_high=24.0,
        alpha_cool=24.0,
        alpha_heat=12.0,
        max_delta_kwh=15.0,
    )

    day = summary["days"][0]
    assert round(day["cdd"], 3) == round(4.0 / 24.0, 3)
    assert round(day["hdd"], 3) == round(3.0 / 24.0, 3)
    assert day["weather_load_adj_kwh"] == 5.5
    assert day["gti_kwh_m2"] == 0.9


def test_weather_summary_keeps_three_prior_days_for_seasonal_anomaly():
    from lib import weather

    start = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        {
            "time": (start + timedelta(days=index)).isoformat(),
            "temp_c": temp,
            "gti_wm2": 0,
        }
        for index, temp in enumerate((25.0, 25.0, 25.0, 27.0))
    ]

    summary = weather.summarize_forecast(
        rows,
        comfort_low=21,
        comfort_high=24,
        alpha_cool=24,
        alpha_heat=24,
        max_delta_kwh=15,
        hvac_mode="cooling",
    )

    assert summary["days"][-1]["weather_load_adj_kwh"] == 2.0
    assert all("_raw_cdd" not in day for day in summary["days"])


def test_weather_snapshot_persists_latest_and_history(monkeypatch, tmp_path):
    from lib import weather

    latest_path = tmp_path / "latest.json"
    history_path = tmp_path / "weather.ndjson"
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"time": now.isoformat(), "temp_c": 27.0, "gti_wm2": 700, "cloud_pct": 20},
    ]

    monkeypatch.setattr(weather, "_now", lambda: now)
    monkeypatch.setattr(weather, "_settings", lambda: {
        "WEATHER_ENABLED": "True",
        "HOME_ADDRESS_LAT": "52.0",
        "HOME_ADDRESS_LONG": "5.0",
        "PV_PANEL_TILT": "35",
        "PV_PANEL_AZIMUTH": "180",
        "WEATHER_CACHE_PATH": str(latest_path),
        "WEATHER_HISTORY_PATH": str(history_path),
        "WEATHER_FETCH_TTL_MIN": "30",
        "HVAC_T_COMFORT_LOW": "21",
        "HVAC_T_COMFORT_HIGH": "24",
        "HVAC_ALPHA_COOL": "1",
        "HVAC_ALPHA_HEAT": "1",
        "HVAC_LOAD_MAX_DELTA_KWH": "15",
    })

    class Provider:
        def fetch(self, **kwargs):
            return rows

    snapshot = weather.weather_snapshot(provider=Provider(), force=True)

    assert snapshot["available"] is True
    assert latest_path.exists()
    assert history_path.exists()
    assert json.loads(latest_path.read_text())["hours"][0]["temp_c"] == 27.0
    assert json.loads(history_path.read_text().splitlines()[0])["kind"] == "weather"


def test_weather_snapshot_converts_compass_azimuth_for_open_meteo(monkeypatch, tmp_path):
    from lib import weather

    captured = {}
    monkeypatch.setattr(weather, "_settings", lambda: {
        "WEATHER_ENABLED": "True",
        "HOME_ADDRESS_LAT": "52.0",
        "HOME_ADDRESS_LONG": "5.0",
        "PV_PANEL_TILT": "35",
        # Conventional compass bearing: ten degrees east of due south.
        "PV_PANEL_AZIMUTH": "170",
        "WEATHER_CACHE_PATH": str(tmp_path / "latest.json"),
        "WEATHER_HISTORY_PATH": str(tmp_path / "history.ndjson"),
        "HVAC_ALPHA_COOL": "1",
        "HVAC_ALPHA_HEAT": "1",
        "HVAC_LOAD_MAX_DELTA_KWH": "15",
    })

    class Provider:
        def fetch(self, **kwargs):
            captured.update(kwargs)
            return []

    weather.weather_snapshot(provider=Provider(), force=True)

    assert captured["azimuth"] == -10.0
    assert captured["past_days"] == 3


def test_weather_context_uses_seasonal_degree_anomaly_without_horizon_concentration(
    monkeypatch,
):
    from lib import weather

    start = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)
    previous = [
        {
            "time": (start - timedelta(days=days)).isoformat(),
            "temp_c": 25.0,
            "gti_wm2": 0,
        }
        for days in (1, 2, 3)
    ]
    snapshot = {
        "available": True,
        "days": [],
        "hours": previous + [
            {"time": start.isoformat(), "temp_c": 26.0, "gti_wm2": 0},
            {
                "time": (start + timedelta(hours=1)).isoformat(),
                "temp_c": 26.0,
                "gti_wm2": 0,
            },
        ],
        "summary": {},
    }
    monkeypatch.setattr(weather, "weather_snapshot", lambda **kwargs: snapshot)
    monkeypatch.setattr(weather, "WINTER_MODE", False)
    monkeypatch.setattr(weather, "_settings", lambda: {
        "HVAC_LOAD_ENABLED": "True",
        "HVAC_LOAD_APPLY": "False",
        "HVAC_ALPHA_COOL": "24",
        "HVAC_ALPHA_HEAT": "24",
        "HVAC_LOAD_MAX_DELTA_KWH": "15",
        "PV_WEATHER_ENABLED": "False",
        "PV_WEATHER_APPLY": "False",
    })

    one_slot = [{"start": start}]
    two_slots = one_slot + [{"start": start + timedelta(hours=1)}]
    one = weather.weather_context_for_slots(
        one_slot, 1.0, {start: 1.0}, {start: 0.0})
    two = weather.weather_context_for_slots(
        two_slots,
        1.0,
        {slot["start"]: 1.0 for slot in two_slots},
        {slot["start"]: 0.0 for slot in two_slots},
    )

    # Cooling degree anomaly is 1C. alpha=24 kWh/DD over one hour => 1 kWh.
    assert one["load_adjustments"][start] == 1.0
    assert two["load_adjustments"][start] == 1.0
    assert one["summary"]["hvac_mode"] == "cooling"
    assert one["slots"][start.isoformat()]["baseline_load_kwh"] == 1.0
    assert one["slots"][start.isoformat()]["weather_load_shadow_kwh"] == 2.0


def test_winter_hvac_uses_heating_anomaly_and_can_reduce_a_warm_slot(monkeypatch):
    from lib import weather

    start = datetime(2026, 1, 14, 12, 0, tzinfo=timezone.utc)
    snapshot = {
        "available": True,
        "days": [],
        "hours": [
            *[
                {
                    "time": (start - timedelta(days=days)).isoformat(),
                    "temp_c": 10.0,
                    "gti_wm2": 0,
                }
                for days in (1, 2, 3)
            ],
            {"time": start.isoformat(), "temp_c": 14.0, "gti_wm2": 0},
        ],
        "summary": {},
    }
    monkeypatch.setattr(weather, "weather_snapshot", lambda **kwargs: snapshot)
    monkeypatch.setattr(weather, "WINTER_MODE", True)
    monkeypatch.setattr(weather, "_settings", lambda: {
        "HVAC_LOAD_ENABLED": "True",
        "HVAC_LOAD_APPLY": "True",
        "HVAC_ALPHA_COOL": "24",
        "HVAC_ALPHA_HEAT": "24",
        "HVAC_LOAD_MAX_DELTA_KWH": "15",
        "PV_WEATHER_ENABLED": "False",
        "PV_WEATHER_APPLY": "False",
    })

    ctx = weather.weather_context_for_slots(
        [{"start": start}], 1.0, {start: 2.0}, {start: 0.0})

    assert ctx["summary"]["hvac_mode"] == "heating"
    # A negative anomaly is allowed but cannot make forecast load negative.
    assert ctx["load_forecast"][start] == 0.0
    assert ctx["load_adjustments"][start] == -2.0


def test_weather_context_aligns_backward_averaged_radiation_to_preceding_hour(
    monkeypatch,
):
    from lib import weather

    start = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    snapshot = {
        "available": True,
        "days": [],
        "hours": [
            {"time": start.isoformat(), "temp_c": 24.0, "gti_wm2": 100},
            {
                "time": (start + timedelta(hours=1)).isoformat(),
                "temp_c": 24.0,
                "gti_wm2": 800,
            },
        ],
        "summary": {},
    }
    monkeypatch.setattr(weather, "weather_snapshot", lambda **kwargs: snapshot)
    monkeypatch.setattr(weather, "WINTER_MODE", False)
    monkeypatch.setattr(weather, "_settings", lambda: {
        "HVAC_LOAD_ENABLED": "False",
        "HVAC_LOAD_APPLY": "False",
        "PV_WEATHER_ENABLED": "True",
        "PV_WEATHER_APPLY": "False",
        "PV_WEATHER_BLEND": "0.5",
    })

    ctx = weather.weather_context_for_slots(
        [{"start": start}], 1.0, {start: 1.0}, {start: 1.0})

    assert ctx["slots"][start.isoformat()]["gti_forecast_wm2"] == 800
    assert ctx["slots"][start.isoformat()]["baseline_pv_kwh"] == 1.0


def test_weather_context_builds_shadow_load_and_pv_forecasts(monkeypatch):
    from lib import weather

    start = datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc)
    slots = [{"start": start + timedelta(hours=i)} for i in range(2)]
    base_load = {slots[0]["start"]: 1.0, slots[1]["start"]: 3.0}
    base_pv = {slots[0]["start"]: 1.0, slots[1]["start"]: 1.0}
    snapshot = {
        "available": True,
        "days": [{
            "date": "2026-06-28",
            "weather_load_adj_kwh": 4.0,
        }],
        "hours": [
            {"time": slots[0]["start"].isoformat(), "temp_c": 26.0, "gti_wm2": 900, "cloud_pct": 10},
            {"time": slots[1]["start"].isoformat(), "temp_c": 27.0, "gti_wm2": 100, "cloud_pct": 40},
        ],
    }
    monkeypatch.setattr(weather, "weather_snapshot", lambda **kwargs: snapshot)
    monkeypatch.setattr(weather, "_settings", lambda: {
        "HVAC_LOAD_APPLY": "False",
        "PV_WEATHER_APPLY": "False",
        "PV_WEATHER_BLEND": "0.5",
    })

    ctx = weather.weather_context_for_slots(slots, 1.0, base_load, base_pv)

    assert ctx["available"] is True
    # A cache created before past-day weather was added must fail open rather
    # than manufacture an absolute HVAC adjustment.
    assert ctx["load_adjustments"][slots[0]["start"]] == 0.0
    assert ctx["load_adjustments"][slots[1]["start"]] == 0.0
    assert ctx["load_forecast"][slots[0]["start"]] == 1.0
    assert ctx["load_shadow_forecast"][slots[1]["start"]] == 3.0
    assert ctx["pv_shadow_forecast"][slots[0]["start"]] > ctx["pv_shadow_forecast"][slots[1]["start"]]
