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
              azimuth: float | None = None, forecast_days: int = 3) -> list[dict]:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY_VARS),
            "forecast_days": max(1, min(7, int(forecast_days or 3))),
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


def summarize_forecast(rows: list[dict], *, comfort_low: float, comfort_high: float,
                       alpha_cool: float, alpha_heat: float,
                       max_delta_kwh: float) -> dict:
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
        raw_delta = day["cdd"] * alpha_cool + day["hdd"] * alpha_heat
        capped = min(max_delta_kwh, max(0.0, raw_delta))
        out.append({
            **day,
            "temp_min_c": round(min(temps), 1) if temps else None,
            "temp_max_c": round(max(temps), 1) if temps else None,
            "temp_mean_c": round(sum(temps) / len(temps), 1) if temps else None,
            "cloud_avg_pct": round(sum(clouds) / len(clouds)) if clouds else None,
            "cdd": round(day["cdd"], 3),
            "hdd": round(day["hdd"], 3),
            "gti_kwh_m2": round(day["gti_kwh_m2"], 3),
            "shortwave_kwh_m2": round(day["shortwave_kwh_m2"], 3),
            "weather_load_adj_kwh": round(capped, 3),
        })

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
    if cached and not force:
        fetched = _parse_ts(cached.get("fetched_at"))
        if fetched and _now() - fetched < timedelta(minutes=ttl_min):
            return cached

    lat = _float(settings.get("HOME_ADDRESS_LAT"), math.nan)
    lon = _float(settings.get("HOME_ADDRESS_LONG"), math.nan)
    if math.isnan(lat) or math.isnan(lon):
        return cached or {"available": False, "reason": "missing HOME_ADDRESS_LAT/HOME_ADDRESS_LONG"}

    provider_id = (settings.get("WEATHER_PROVIDER") or "open-meteo").strip().lower()
    provider = provider or OpenMeteoProvider()
    try:
        rows = provider.fetch(
            lat=lat,
            lon=lon,
            tilt=_float(settings.get("PV_PANEL_TILT"), None),
            azimuth=_float(settings.get("PV_PANEL_AZIMUTH"), None),
            forecast_days=3,
        )
        summary = summarize_forecast(
            rows,
            comfort_low=_float(settings.get("HVAC_T_COMFORT_LOW"), 21.0),
            comfort_high=_float(settings.get("HVAC_T_COMFORT_HIGH"), 24.0),
            alpha_cool=_float(settings.get("HVAC_ALPHA_COOL"), 1.0),
            alpha_heat=_float(settings.get("HVAC_ALPHA_HEAT"), 1.0),
            max_delta_kwh=_float(settings.get("HVAC_LOAD_MAX_DELTA_KWH"), 15.0),
        )
        snapshot = {
            "available": True,
            "source": provider_id,
            "fetched_at": _now().isoformat(),
            "latitude": lat,
            "longitude": lon,
            **summary,
        }
        _write_json_atomic(cache_path, snapshot)
        _append_history(_history_path(settings), snapshot)
        return snapshot
    except Exception as e:
        logging.warning("Weather: fetch failed, using last cached forecast if available: %s", e)
        return cached or {"available": False, "reason": str(e)}


def _row_by_hour(snapshot: dict) -> dict:
    out = {}
    for row in snapshot.get("hours") or []:
        ts = _parse_ts(row.get("time"))
        if ts is not None:
            out[ts.replace(minute=0, second=0, microsecond=0)] = row
    return out


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

    by_day = {d["date"]: d for d in snapshot.get("days") or []}
    by_hour = _row_by_hour(snapshot)
    load_apply = _truthy(settings.get("HVAC_LOAD_APPLY"), False)
    pv_apply = _truthy(settings.get("PV_WEATHER_APPLY"), False)
    load_enabled = _truthy(settings.get("HVAC_LOAD_ENABLED"), True)
    pv_enabled = _truthy(settings.get("PV_WEATHER_ENABLED"), True)
    pv_blend = max(0.0, min(1.0, _float(settings.get("PV_WEATHER_BLEND"), 0.5)))

    slots_by_day = {}
    for slot in price_slots:
        start = slot["start"]
        slots_by_day.setdefault(start.date().isoformat(), []).append(start)

    load_adjustments = {}
    load_shadow = dict(load_forecast)
    pv_shadow = dict(pv_forecast)
    slot_context = {}

    for day, starts in slots_by_day.items():
        day_weather = by_day.get(day, {})
        delta = _float(day_weather.get("weather_load_adj_kwh"), 0.0) if load_enabled else 0.0
        weights = [max(0.0, _float(load_forecast.get(s), 0.0)) for s in starts]
        wsum = sum(weights) or len(starts) or 1
        for start, weight in zip(starts, weights):
            adj = delta * ((weight if sum(weights) else 1.0) / wsum)
            load_adjustments[start] = round(adj, 4)
            load_shadow[start] = _float(load_forecast.get(start), 0.0) + adj

        base_pv_total = sum(max(0.0, _float(pv_forecast.get(s), 0.0)) for s in starts)
        gti_weights = []
        for start in starts:
            hour_key = start.replace(minute=0, second=0, microsecond=0)
            gti_weights.append(max(0.0, _float((by_hour.get(hour_key) or {}).get("gti_wm2"), 0.0)))
        gti_sum = sum(gti_weights)
        if pv_enabled and base_pv_total > 0 and gti_sum > 0:
            for start, gti in zip(starts, gti_weights):
                shaped = base_pv_total * gti / gti_sum
                base = max(0.0, _float(pv_forecast.get(start), 0.0))
                pv_shadow[start] = (1.0 - pv_blend) * base + pv_blend * shaped

    for slot in price_slots:
        start = slot["start"]
        hour_key = start.replace(minute=0, second=0, microsecond=0)
        w = by_hour.get(hour_key) or {}
        slot_context[start.isoformat()] = {
            "temp_forecast_c": w.get("temp_c"),
            "gti_forecast_wm2": w.get("gti_wm2"),
            "cloud_forecast_pct": w.get("cloud_pct"),
            "weather_load_adj_kwh": load_adjustments.get(start, 0.0),
            "weather_pv_shadow_kwh": pv_shadow.get(start),
        }

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
            "pv_enabled": pv_enabled,
            "load_adj_today_kwh": next((d.get("weather_load_adj_kwh")
                                        for d in snapshot.get("days", [])
                                        if d.get("date") == _now().date().isoformat()), None),
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
