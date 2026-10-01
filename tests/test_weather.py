"""MCP de meteorología: previsión por horas de eltiempo.es (UC-4)."""

from __future__ import annotations

import json

import httpx
import pytest

from argos.mcp_servers.weather import eltiempo
from argos.mcp_servers.weather.eltiempo import WeatherError, fetch_hourly, parse_hourly


def _hour(h, temp, mm, prob, speed, direction, rain_cls=" rain"):
    return f"""
    <li><div><p class="text-roboto-condensed hours">{h}</p>
      <img alt="Icono" src="x.svg"/><p class="degrees" data-temperature="{temp}">{temp} °</p></div>
    <div><div class="precipitations{rain_cls}"><span class="bar level_1"></span>
      <p class="measure">{mm} mm</p><p class="percentage">{prob}%</p></div>
      <div class="wind"><i class="icon icon-sm icon-direction {direction}"></i>
      <p><span class="wind-text-value velocity" data-speed="{speed}">{speed}</span>
      <span class="wind-text-unit" data-speed-unit="1">km/h</span></p></div></div></li>"""


PAGE = f"""<html><body><ul class="nav"><li>ruido</li></ul>
<ul class="meteograma" id="meteograma">
  <li><h2 class="days-title" id="29092026">Hoy</h2><ul class="days">
    {_hour("22:00", 24, "0.2", 60, 10, "east")}
    {_hour("23:00", 23, "0", 20, 8, "north-east", rain_cls="")}
  </ul></li>
  <li><h2 class="days-title" id="30092026">Mañana</h2><ul class="days">
    {_hour("00:00", 22, "1,5", 90, 25, "south-west")}
  </ul></li>
</ul><ul><li><p class="hours">99:99</p></li></ul></body></html>"""


def test_parse_hourly_extracts_numbers_per_hour():
    rows = parse_hourly(PAGE)
    assert rows == [
        {
            "date": "2026-09-29",
            "hour": "22:00",
            "temp_c": 24.0,
            "rain_mm": 0.2,
            "rain_prob": 60,
            "wind_kmh": 10.0,
            "wind_dir": "E",
        },
        {
            "date": "2026-09-29",
            "hour": "23:00",
            "temp_c": 23.0,
            "rain_mm": 0.0,
            "rain_prob": 20,
            "wind_kmh": 8.0,
            "wind_dir": "NE",
        },
        {
            "date": "2026-09-30",
            "hour": "00:00",
            "temp_c": 22.0,
            "rain_mm": 1.5,
            "rain_prob": 90,
            "wind_kmh": 25.0,
            "wind_dir": "SO",
        },
    ]


def test_parse_unknown_page_is_empty():
    assert parse_hourly("<html><p class='hours'>10:00</p></html>") == []


async def test_fetch_validates_city_and_errors():
    for bad in ("../admin", "Barcelona", "a b", "", "x" * 100):
        with pytest.raises(WeatherError):
            await fetch_hourly(bad)

    def handler(req):
        return httpx.Response(404) if "nada" in req.url.path else httpx.Response(200, text="<p/>")

    t = httpx.MockTransport(handler)
    with pytest.raises(WeatherError, match="no tiene la ciudad"):
        await fetch_hourly("nada", transport=t)
    with pytest.raises(WeatherError, match="formato"):
        await fetch_hourly("barcelona", transport=t)


async def test_hourly_tool_returns_summary(monkeypatch):
    from argos.mcp_servers.weather import server as w

    seen = []

    async def fake_fetch(city):
        seen.append(city)
        return parse_hourly(PAGE)

    monkeypatch.setattr(w, "fetch_hourly", fake_fetch)
    monkeypatch.delenv("ARGOS_WEATHER_CITY", raising=False)
    data = json.loads(await w.hourly(hours=2))
    assert seen == ["barcelona"] and len(data["hourly"]) == 2
    assert data["summary"] == [
        {
            "date": "2026-09-29",
            "hours": 2,
            "temp_min": 23.0,
            "temp_max": 24.0,
            "rain_total_mm": 0.2,
            "rain_prob_max": 60,
            "wind_max_kmh": 10.0,
            "rain_hours": ["22:00"],
        }
    ]
    await w.hourly(city="Madrid")
    assert seen[-1] == "madrid"


async def test_hourly_tool_reports_errors(monkeypatch):
    from argos.mcp_servers.weather import server as w

    async def boom(city):
        raise eltiempo.WeatherError("caído")

    monkeypatch.setattr(w, "fetch_hourly", boom)
    assert (await w.hourly()).startswith("ERROR: caído")


async def test_weather_in_catalogs(root, store, fake_sandbox):
    from argos.config import load_config
    from argos.core.session import catalog

    cfg = load_config(root, {"data_dir": str(root / "var")})
    for profile in ("personal", "orchestrator"):
        by = {t["name"]: t["risk"] for t in await catalog(cfg, profile)}
        assert by.get("weather.hourly") == "read", profile
