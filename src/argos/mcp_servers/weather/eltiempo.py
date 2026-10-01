"""Previsión por horas de eltiempo.es (vista `?v=por_hora`).

Se extraen solo valores numéricos (temperatura, lluvia, viento) de atributos/estructura conocidos;
ningún texto libre de la página llega al modelo. Si el HTML cambia y no se reconoce nada, se
devuelve error en vez de datos a medias.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

BASE = "https://www.eltiempo.es"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130 Safari/537.36"
)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
_DIRS = {
    "north": "N",
    "north-east": "NE",
    "east": "E",
    "south-east": "SE",
    "south": "S",
    "south-west": "SO",
    "west": "O",
    "north-west": "NO",
}


class WeatherError(RuntimeError):
    pass


def valid_slug(city: str) -> bool:
    return bool(_SLUG.match(city)) and len(city) <= 80


def _num(text: str) -> float | None:
    m = _NUM.search(text or "")
    return float(m.group().replace(",", ".")) if m else None


class _Meteograma(HTMLParser):
    """Recorre `ul#meteograma`: h2.days-title (id=DDMMYYYY) y, por cada hora, un `li`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._in = False
        self._depth = 0  # profundidad de <ul> dentro del meteograma
        self._date = ""
        self._row: dict | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "ul" and a.get("id") == "meteograma":
            self._in, self._depth = True, 1
            return
        if not self._in:
            return
        if tag == "ul":
            self._depth += 1
        elif tag == "h2" and "days-title" in cls:
            d = a.get("id") or ""
            self._date = f"{d[4:8]}-{d[2:4]}-{d[0:2]}" if re.fullmatch(r"\d{8}", d) else ""
        elif tag == "li" and self._depth == 2:
            self._row = {"date": self._date}
        elif self._row is None:
            return
        elif tag == "p" and "hours" in cls:
            self._capture = "hour"
        elif tag == "p" and "degrees" in cls:
            self._row["temp_c"] = _num(a.get("data-temperature") or "")
        elif tag == "p" and "measure" in cls:
            self._capture = "rain_mm"
        elif tag == "p" and "percentage" in cls:
            self._capture = "rain_prob"
        elif tag == "i" and "icon-direction" in cls:
            dirs = [c for c in cls if c in _DIRS]
            self._row["wind_dir"] = _DIRS[dirs[0]] if dirs else None
        elif tag == "span" and "velocity" in cls:
            self._row["wind_kmh"] = _num(a.get("data-speed") or "")

    def handle_data(self, data: str) -> None:
        if self._row is None or not self._capture or not data.strip():
            return
        if self._capture == "hour":
            m = re.search(r"\b(\d{1,2}):(\d{2})\b", data)
            self._row["hour"] = f"{int(m.group(1)):02d}:{m.group(2)}" if m else None
        elif self._capture == "rain_mm":
            self._row["rain_mm"] = _num(data)
        elif self._capture == "rain_prob":
            v = _num(data)
            self._row["rain_prob"] = int(v) if v is not None else None
        self._capture = None

    def handle_endtag(self, tag: str) -> None:
        if not self._in:
            return
        if tag == "li" and self._depth == 2 and self._row is not None:
            if self._row.get("hour") and self._row.get("temp_c") is not None:
                self.rows.append(self._row)
            self._row = None
        elif tag == "ul":
            self._depth -= 1
            if self._depth == 0:
                self._in = False


def parse_hourly(html: str) -> list[dict]:
    p = _Meteograma()
    p.feed(html)
    keys = ("date", "hour", "temp_c", "rain_mm", "rain_prob", "wind_kmh", "wind_dir")
    return [{k: r.get(k) for k in keys} for r in p.rows]


async def fetch_hourly(city: str, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    if not valid_slug(city):
        raise WeatherError(
            "ciudad inválida (usa el nombre de la URL de eltiempo.es, p. ej. "
            "'barcelona' o 'sant-cugat-del-valles')"
        )
    url = f"{BASE}/{city}.html"
    try:
        async with httpx.AsyncClient(
            timeout=20,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"},
        ) as http:
            resp = await http.get(url, params={"v": "por_hora"})
    except httpx.HTTPError as exc:
        raise WeatherError(f"no se pudo contactar con eltiempo.es: {exc}") from exc
    if resp.status_code == 404:
        raise WeatherError(f"eltiempo.es no tiene la ciudad '{city}'")
    if resp.status_code >= 400:
        raise WeatherError(f"eltiempo.es respondió HTTP {resp.status_code}")
    rows = parse_hourly(resp.text)
    if not rows:
        raise WeatherError(
            "no se reconoce la previsión por horas en la página "
            "(¿ha cambiado el formato de eltiempo.es?)"
        )
    return rows
