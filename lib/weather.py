"""Weather forecast fetch, shadow adjustments, and dashboard summaries.

This module is deliberately fail-open: weather data can improve forecast quality,
but it must never block or crash ESS optimization.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from urllib import parse, request

from lib.config_retrieval import retrieve_setting
from lib.constants import logging
from lib.ess_mode import WINTER_MODE

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = (
    "temperature_2m",
    "apparent_temperature",
    "cloud_cover",
    "precipitation",
    "wind_speed_10m",
    "shortwave_radiation",
    "global_tilted_irradiance",
    "direct_normal_irradiance",
    "diffuse_radiation",
)


def _now():
    return datetime.now(timezone.utc)


def _settings() -> dict:
    keys = [
        "WEATHER_ENABLED",
        "WEATHER_PROVIDER",
        "HOME_ADDRESS_LAT",
        "HOME_ADDRESS_LONG",
        "PV_PANEL_TILT",
        "PV_PANEL_AZIMUTH",
        "WEATHER_FETCH_TTL_MIN",
        "WEATHER_CACHE_PATH",
        "WEATHER_HISTORY_PATH",
        "HVAC_T_COMFORT_LOW",
        "HVAC_T_COMFORT_HIGH",
        "HVAC_ALPHA_COOL",
        "HVAC_ALPHA_HEAT",
        "HVAC_LOAD_MAX_DELTA_KWH",
        "HVAC_LOAD_ENABLED",
        "HVAC_LOAD_APPLY",
        "PV_WEATHER_ENABLED",
        "PV_WEATHER_BLEND",
        "PV_WEATHER_APPLY",
    ]
    return {k: retrieve_setting(k) for k in keys}


def _truthy(value, default=False) -> bool:
    if value in (None, "", "None"):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_ts(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt
    except (AttributeError, ValueError):
        return None


def _cache_path(settings=None) -> str:
    settings = settings or _settings()
    return settings.get("WEATHER_CACHE_PATH") or "data/weather/latest.json"


def _history_path(settings=None) -> str:
    settings = settings or _settings()
    return settings.get("WEATHER_HISTORY_PATH") or "data/weather/weather.ndjson"


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def _append_history(path: str, snapshot: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rec = {
        "kind": "weather",
        "ts": snapshot.get("fetched_at"),
        "source": snapshot.get("source"),
        "summary": snapshot.get("summary"),
        "days": snapshot.get("days", []),
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


class OpenMeteoProvider:
    """Keyless Open-Meteo forecast provider."""

    def fetch(self, *, lat: float, lon: float, tilt: float | None = None,
              azimuth: float | None = None, forecast_days: int = 3,
              past_days: int = 0) -> list[dict]:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY_VARS),
            "forecast_days": max(1, min(7, int(forecast_days or 3))),
            "past_days": max(0, min(7, int(past_days or 0))),
            "timezone": "auto",
        }
        if tilt is not None:
            params["tilt"] = tilt
        if azimuth is not None:
            params["azimuth"] = azimuth
        url = f"{OPEN_METEO_URL}?{parse.urlencode(params)}"
        req = request.Request(url, headers={"User-Agent": "cerbomoticzgx/1.0"})
        with request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return self._parse(payload)

    def _parse(self, payload: dict) -> list[dict]:
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []

        def at(name, idx):
            vals = hourly.get(name) or []
            return vals[idx] if idx < len(vals) else None

        rows = []
        for i, ts in enumerate(times):
            parsed = _parse_ts(ts)
            if parsed is None:
                continue
            rows.append({
                "time": parsed.isoformat(),
                "temp_c": at("temperature_2m", i),
                "apparent_temp_c": at("apparent_temperature", i),
                "cloud_pct": at("cloud_cover", i),
                "precip_mm": at("precipitation", i),
                "wind_kmh": at("wind_speed_10m", i),
                "shortwave_wm2": at("shortwave_radiation", i),
                "gti_wm2": at("global_tilted_irradiance", i),
                "dni_wm2": at("direct_normal_irradiance", i),
                "diffuse_wm2": at("diffuse_radiation", i),
            })
        return rows


def _open_meteo_azimuth(compass_bearing: float | None) -> float | None:
    """Convert a conventional compass bearing to Open-Meteo's south origin.

    Configuration remains user-facing: 0=N, 90=E, 180=S, 270=W. Open-Meteo
    expects 0=S, -90=E, 90=W and +/-180=N.
    """
    if compass_bearing is None:
        return None
    try:
        value = float(compass_bearing)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return (value % 360.0) - 180.0


def summarize_forecast(rows: list[dict], *, comfort_low: float, comfort_high: float,
                       alpha_cool: float, alpha_heat: float,
                       max_delta_kwh: float, hvac_mode: str = "both") -> dict:
    """Aggregate hourly weather into daily HVAC/PV shadow summaries."""
    days = {}
    for row in rows:
        ts = _parse_ts(row.get("time"))
        if ts is None:
            continue
        key = ts.date().isoformat()
        bucket = days.setdefault(key, {
            "date": key,
            "temps": [],
            "clouds": [],
            "cdd": 0.0,
            "hdd": 0.0,
            "gti_kwh_m2": 0.0,
            "shortwave_kwh_m2": 0.0,
        })
        temp = _float(row.get("temp_c"), math.nan)
        if not math.isnan(temp):
            bucket["temps"].append(temp)
            bucket["cdd"] += max(0.0, temp - comfort_high) / 24.0
            bucket["hdd"] += max(0.0, comfort_low - temp) / 24.0
        cloud = _float(row.get("cloud_pct"), math.nan)
        if not math.isnan(cloud):
            bucket["clouds"].append(cloud)
        bucket["gti_kwh_m2"] += max(0.0, _float(row.get("gti_wm2"), 0.0)) / 1000.0
        bucket["shortwave_kwh_m2"] += max(0.0, _float(row.get("shortwave_wm2"), 0.0)) / 1000.0

    out = []
    for day in sorted(days.values(), key=lambda d: d["date"]):
        temps = day.pop("temps")
        clouds = day.pop("clouds")
        out.append({
            **day,
            "_raw_cdd": day["cdd"],
            "_raw_hdd": day["hdd"],
            "temp_min_c": round(min(temps), 1) if temps else None,
            "temp_max_c": round(max(temps), 1) if temps else None,
            "temp_mean_c": round(sum(temps) / len(temps), 1) if temps else None,
            "cloud_avg_pct": round(sum(clouds) / len(clouds)) if clouds else None,
            "cdd": round(day["cdd"], 3),
            "hdd": round(day["hdd"], 3),
            "gti_kwh_m2": round(day["gti_kwh_m2"], 3),
            "shortwave_kwh_m2": round(day["shortwave_kwh_m2"], 3),
            "weather_load_adj_kwh": 0.0,
        })

    for index, day in enumerate(out):
        if hvac_mode == "cooling":
            degree = day["_raw_cdd"]
            previous = [item["_raw_cdd"] for item in out[max(0, index - 3):index]]
            raw_delta = (
                alpha_cool * (degree - sum(previous) / len(previous))
                if len(previous) >= 2 else 0.0
            )
        elif hvac_mode == "heating":
            degree = day["_raw_hdd"]
            previous = [item["_raw_hdd"] for item in out[max(0, index - 3):index]]
            raw_delta = (
                alpha_heat * (degree - sum(previous) / len(previous))
                if len(previous) >= 2 else 0.0
            )
        else:
            # Compatibility for direct callers analysing absolute symmetric
            # degree demand. Runtime forecasting always selects one seasonal mode.
            raw_delta = day["_raw_cdd"] * alpha_cool + day["_raw_hdd"] * alpha_heat
        capped = max(-max_delta_kwh, min(max_delta_kwh, raw_delta))
        day["weather_load_adj_kwh"] = round(capped, 3)

    for day in out:
        day.pop("_raw_cdd", None)
        day.pop("_raw_hdd", None)

    return {
        "hours": rows,
        "days": out,
        "summary": {
            "hours": len(rows),
            "days": len(out),
            "max_temp_c": max((d["temp_max_c"] for d in out if d["temp_max_c"] is not None), default=None),
            "max_load_adj_kwh": max((d["weather_load_adj_kwh"] for d in out), default=0.0),
            "total_gti_kwh_m2": round(sum(d["gti_kwh_m2"] for d in out), 3),
        },
    }


def weather_snapshot(provider=None, force: bool = False) -> dict:
    """Return a cached Open-Meteo summary, fetching when stale."""
    settings = _settings()
    if not _truthy(settings.get("WEATHER_ENABLED"), True):
        return {"available": False, "reason": "disabled"}

    cache_path = _cache_path(settings)
    ttl_min = max(1, _int(settings.get("WEATHER_FETCH_TTL_MIN"), 30))
    cached = _read_json(cache_path)
    lat = _float(settings.get("HOME_ADDRESS_LAT"), math.nan)
    lon = _float(settings.get("HOME_ADDRESS_LONG"), math.nan)
    if math.isnan(lat) or math.isnan(lon):
        return cached or {"available": False, "reason": "missing HOME_ADDRESS_LAT/HOME_ADDRESS_LONG"}

    provider_id = (settings.get("WEATHER_PROVIDER") or "open-meteo").strip().lower()
    tilt = _float(settings.get("PV_PANEL_TILT"), None)
    provider_azimuth = _open_meteo_azimuth(
        _float(settings.get("PV_PANEL_AZIMUTH"), None))
    request_config = {
        "provider": provider_id,
        "latitude": lat,
        "longitude": lon,
        "tilt": tilt,
        "provider_azimuth": provider_azimuth,
        "forecast_days": 3,
        "past_days": 3,
    }
    if cached and not force and cached.get("request_config") == request_config:
        fetched = _parse_ts(cached.get("fetched_at"))
        if fetched and _now() - fetched < timedelta(minutes=ttl_min):
            return cached

    provider = provider or OpenMeteoProvider()
    try:
        rows = provider.fetch(
            lat=lat,
            lon=lon,
            tilt=tilt,
            azimuth=provider_azimuth,
            forecast_days=3,
            # The load forecast is a trailing three-day model. Matching past
            # weather lets HVAC correct the temperature anomaly instead of
            # adding absolute heating/cooling demand a second time.
            past_days=3,
        )
        summary = summarize_forecast(
            rows,
            comfort_low=_float(settings.get("HVAC_T_COMFORT_LOW"), 21.0),
            comfort_high=_float(settings.get("HVAC_T_COMFORT_HIGH"), 24.0),
            alpha_cool=_float(settings.get("HVAC_ALPHA_COOL"), 1.0),
            alpha_heat=_float(settings.get("HVAC_ALPHA_HEAT"), 1.0),
            max_delta_kwh=_float(settings.get("HVAC_LOAD_MAX_DELTA_KWH"), 15.0),
            hvac_mode=_hvac_mode(settings),
        )
        snapshot = {
            "available": True,
            "source": provider_id,
            "fetched_at": _now().isoformat(),
            "latitude": lat,
            "longitude": lon,
            "request_config": request_config,
            **summary,
        }
        _write_json_atomic(cache_path, snapshot)
        _append_history(_history_path(settings), snapshot)
        return snapshot
    except Exception as e:
        logging.warning("Weather: fetch failed, using last cached forecast if available: %s", e)
        return cached or {"available": False, "reason": str(e)}


def _local_hour_key(value: datetime) -> tuple[str, int]:
    return value.date().isoformat(), value.hour


def _temperature_by_hour(snapshot: dict) -> dict:
    out = {}
    for row in snapshot.get("hours") or []:
        ts = _parse_ts(row.get("time"))
        if ts is not None:
            out[_local_hour_key(ts)] = row
    return out


def _radiation_by_hour(snapshot: dict) -> dict:
    """Index backward-averaged hourly radiation by its covered interval."""
    out = {}
    for row in snapshot.get("hours") or []:
        ts = _parse_ts(row.get("time"))
        if ts is not None:
            out[_local_hour_key(ts - timedelta(hours=1))] = row
    return out


def _hvac_mode(settings: dict | None = None) -> str:
    # This installation's split heat pumps are cooling-only in Summer Mode and
    # provide all space heating in Winter Mode; they cannot do both concurrently.
    return "heating" if WINTER_MODE else "cooling"


def _active_degree(temp_c, *, mode: str, comfort_low: float,
                   comfort_high: float) -> float | None:
    try:
        temp = float(temp_c)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(temp):
        return None
    if mode == "heating":
        return max(0.0, comfort_low - temp)
    return max(0.0, temp - comfort_high)


def weather_context_for_slots(price_slots: list, slot_duration_h: float,
                              load_forecast: dict, pv_forecast: dict) -> dict:
    """Build shadow load/PV forecasts for optimizer slots.

    By default this returns adjusted forecasts separately while leaving the live
    forecasts unchanged. If the APPLY gates are enabled, callers can use the
    returned ``load_forecast`` / ``pv_forecast`` values directly.
    """
    settings = _settings()
    snapshot = weather_snapshot()
    if not snapshot.get("available"):
        return {"available": False, "reason": snapshot.get("reason")}

    temp_by_hour = _temperature_by_hour(snapshot)
    radiation_by_hour = _radiation_by_hour(snapshot)
    load_apply = _truthy(settings.get("HVAC_LOAD_APPLY"), False)
    pv_apply = _truthy(settings.get("PV_WEATHER_APPLY"), False)
    load_enabled = _truthy(settings.get("HVAC_LOAD_ENABLED"), True)
    pv_enabled = _truthy(settings.get("PV_WEATHER_ENABLED"), True)
    pv_blend = max(0.0, min(1.0, _float(settings.get("PV_WEATHER_BLEND"), 0.5)))
    hvac_mode = _hvac_mode(settings)
    comfort_low = _float(settings.get("HVAC_T_COMFORT_LOW"), 21.0)
    comfort_high = _float(settings.get("HVAC_T_COMFORT_HIGH"), 24.0)
    hvac_alpha = _float(
        settings.get("HVAC_ALPHA_HEAT" if hvac_mode == "heating" else "HVAC_ALPHA_COOL"),
        1.0,
    )
    max_load_delta = max(
        0.0, _float(settings.get("HVAC_LOAD_MAX_DELTA_KWH"), 15.0))

    slots_by_day = {}
    for slot in price_slots:
        start = slot["start"]
        slots_by_day.setdefault(start.date().isoformat(), []).append(start)

    load_adjustments = {}
    load_shadow = dict(load_forecast)
    pv_shadow = dict(pv_forecast)
    slot_context = {}

    for day, starts in slots_by_day.items():
        provisional = {}
        for start in starts:
            adjustment = 0.0
            current_row = temp_by_hour.get(_local_hour_key(start)) or {}
            current_degree = _active_degree(
                current_row.get("temp_c"),
                mode=hvac_mode,
                comfort_low=comfort_low,
                comfort_high=comfort_high,
            )
            historical_degrees = []
            for days_ago in (1, 2, 3):
                prior_date = (start.date() - timedelta(days=days_ago)).isoformat()
                prior_row = temp_by_hour.get((prior_date, start.hour)) or {}
                degree = _active_degree(
                    prior_row.get("temp_c"),
                    mode=hvac_mode,
                    comfort_low=comfort_low,
                    comfort_high=comfort_high,
                )
                if degree is not None:
                    historical_degrees.append(degree)
            if load_enabled and current_degree is not None and len(historical_degrees) >= 2:
                reference_degree = sum(historical_degrees) / len(historical_degrees)
                adjustment = (
                    hvac_alpha
                    * (current_degree - reference_degree)
                    * max(0.0, float(slot_duration_h))
                    / 24.0
                )
            provisional[start] = adjustment

        net_adjustment = sum(provisional.values())
        if max_load_delta <= 0.0:
            scale = 0.0
        elif abs(net_adjustment) > max_load_delta:
            scale = max_load_delta / abs(net_adjustment)
        else:
            scale = 1.0
        for start in starts:
            base = max(0.0, _float(load_forecast.get(start), 0.0))
            shadow = max(0.0, base + provisional[start] * scale)
            adjustment = shadow - base
            load_adjustments[start] = round(adjustment, 4)
            load_shadow[start] = shadow

        base_pv_total = sum(max(0.0, _float(pv_forecast.get(s), 0.0)) for s in starts)
        gti_weights = []
        for start in starts:
            gti_weights.append(max(
                0.0,
                _float((radiation_by_hour.get(_local_hour_key(start)) or {}).get("gti_wm2"), 0.0),
            ))
        gti_sum = sum(gti_weights)
        if pv_enabled and base_pv_total > 0 and gti_sum > 0:
            for start, gti in zip(starts, gti_weights):
                shaped = base_pv_total * gti / gti_sum
                base = max(0.0, _float(pv_forecast.get(start), 0.0))
                pv_shadow[start] = (1.0 - pv_blend) * base + pv_blend * shaped

    for slot in price_slots:
        start = slot["start"]
        weather_row = temp_by_hour.get(_local_hour_key(start)) or {}
        radiation_row = radiation_by_hour.get(_local_hour_key(start)) or {}
        base_load = _float(load_forecast.get(start), 0.0)
        base_pv = _float(pv_forecast.get(start), 0.0)
        slot_context[start.isoformat()] = {
            "temp_forecast_c": weather_row.get("temp_c"),
            "gti_forecast_wm2": radiation_row.get("gti_wm2"),
            "cloud_forecast_pct": weather_row.get("cloud_pct"),
            "baseline_load_kwh": round(base_load, 4),
            "weather_load_adj_kwh": load_adjustments.get(start, 0.0),
            "weather_load_shadow_kwh": round(
                _float(load_shadow.get(start), base_load), 4),
            "baseline_pv_kwh": round(base_pv, 4),
            "weather_pv_shadow_kwh": round(
                _float(pv_shadow.get(start), base_pv), 4),
        }

    try:
        local_today = datetime.now(price_slots[0]["start"].tzinfo).date().isoformat()
    except (IndexError, KeyError, AttributeError):
        local_today = _now().date().isoformat()

    return {
        "available": True,
        "snapshot": snapshot,
        "slots": slot_context,
        "load_adjustments": load_adjustments,
        "load_shadow_forecast": load_shadow,
        "pv_shadow_forecast": pv_shadow,
        "load_forecast": load_shadow if load_apply else load_forecast,
        "pv_forecast": pv_shadow if pv_apply else pv_forecast,
        "summary": {
            "source": snapshot.get("source"),
            "fetched_at": snapshot.get("fetched_at"),
            "hvac_apply": load_apply,
            "pv_apply": pv_apply,
            "hvac_enabled": load_enabled,
            "hvac_mode": hvac_mode,
            "pv_enabled": pv_enabled,
            "load_adj_today_kwh": round(sum(
                adjustment for start, adjustment in load_adjustments.items()
                if start.date().isoformat() == local_today
            ), 3),
            "max_temp_c": (snapshot.get("summary") or {}).get("max_temp_c"),
            "max_load_adj_kwh": (snapshot.get("summary") or {}).get("max_load_adj_kwh"),
            "pv_shadow_abs_delta_kwh": round(sum(
                abs(_float(pv_shadow.get(k), 0.0) - _float(pv_forecast.get(k), 0.0))
                for k in pv_shadow
            ), 3),
            "pv_shadow_net_delta_kwh": round(sum(
                _float(pv_shadow.get(k), 0.0) - _float(pv_forecast.get(k), 0.0)
                for k in pv_shadow
            ), 3),
        },
    }
