"""Servidor MCP de meteorología (UC-4): previsión por horas de eltiempo.es.

Solo lectura (riesgo `read`): temperatura, lluvia (mm y probabilidad) y viento (km/h y
dirección) por horas, con resumen diario. Pensado para consultas y tareas programadas
(p. ej. aviso matinal de lluvia). Sin credenciales.

Entorno (opcional): ARGOS_WEATHER_CITY (ciudad por defecto, slug de eltiempo.es; barcelona).
"""

from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.weather.eltiempo import WeatherError, fetch_hourly

VERSION = "0.1.0"
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

server = MCPServer(name="weather", version=VERSION)


def _summary(rows: list[dict]) -> list[dict]:
    days: dict[str, list[dict]] = {}
    for r in rows:
        days.setdefault(r["date"], []).append(r)
    out = []
    for date, hs in days.items():
        temps = [h["temp_c"] for h in hs if h["temp_c"] is not None]
        rain = [h["rain_mm"] for h in hs if h["rain_mm"] is not None]
        probs = [h["rain_prob"] for h in hs if h["rain_prob"] is not None]
        wind = [h["wind_kmh"] for h in hs if h["wind_kmh"] is not None]
        rainy = [h["hour"] for h in hs if (h["rain_mm"] or 0) > 0 or (h["rain_prob"] or 0) >= 50]
        out.append({"date": date, "hours": len(hs),
                    "temp_min": min(temps, default=None), "temp_max": max(temps, default=None),
                    "rain_total_mm": round(sum(rain), 1), "rain_prob_max": max(probs, default=None),
                    "wind_max_kmh": max(wind, default=None),
                    "rain_hours": rainy})
    return out


@server.tool(annotations=READ)
async def hourly(city: str = "", hours: int = 24) -> str:
    """Previsión por horas (eltiempo.es): temp_c, rain_mm, rain_prob (%), wind_kmh, wind_dir,
    más un resumen por día (mín/máx, lluvia total, horas con lluvia, viento máximo).
    city = nombre en la URL de eltiempo.es (p. ej. barcelona, madrid, sant-cugat-del-valles);
    vacío = ciudad por defecto. hours = cuántas horas desde ahora (1..72)."""
    city = (city or os.environ.get("ARGOS_WEATHER_CITY") or "barcelona").strip().lower()
    hours = max(1, min(int(hours), 72))
    try:
        rows = (await fetch_hourly(city))[:hours]
    except WeatherError as exc:
        return f"ERROR: {exc}"
    return json.dumps({"city": city, "source": "eltiempo.es", "timezone": "Europe/Madrid",
                       "summary": _summary(rows), "hourly": rows},
                      ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
