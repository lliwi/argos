"""Verdad de referencia en vivo para evaluar la eficacia real de Argos (checks `live`).

Tras cada tarea, el evaluador consulta el servicio real DIRECTAMENTE (sin modelo, con las
credenciales del inventario) y contrasta la respuesta del agente con el dato verdadero. Así se
mide si Argos acierta, no solo si llamó a la herramienta. Todas las sondas son de solo lectura.

Cada sonda devuelve un `Truth`:
- count: un número que la respuesta debe dar (0 admite "ninguno", "no hay"…).
- names: nombres que la respuesta debe mencionar todos (y el recuento si son muchos).
- number: un valor numérico que la respuesta debe dar con cierta tolerancia.
- choice: una de varias opciones (p. ej. bici/coche); `value` son las aceptables.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from argos.config import Config
from argos.inventory import load_inventory


@dataclass
class Truth:
    kind: str  # count | names | number | choice
    value: Any
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ProbeError(RuntimeError):
    pass


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower())


# --------------------------------------------------------------------------- sondas


async def portainer_stopped(cfg: Config, params: dict[str, Any]) -> Truth:
    from argos.mcp_servers.portainer.rest import PortainerClient

    inv = load_inventory(cfg.root)
    pt = inv.get("portainer")
    key = inv.secret("portainer", "api_key")
    if not pt.get("url") or not key:
        raise ProbeError("portainer sin configurar en el inventario")
    client = PortainerClient(pt["url"], key, endpoint=int(pt.get("endpoint", 1)))
    try:
        items = await client.containers(all_=True)
    finally:
        await client.aclose()
    names = sorted(
        (c.get("Names") or ["?"])[0].lstrip("/") for c in items if c.get("State") != "running"
    )
    return Truth("names", names, f"{len(names)} parados de {len(items)}: {names}")


async def nas_volume_used(cfg: Config, params: dict[str, Any]) -> Truth:
    from argos.mcp_servers.nas.rest import snmp_storage

    inv = load_inventory(cfg.root)
    nas = inv.get("nas")
    community = inv.secret("nas", "community")
    if not nas.get("url") or not community:
        raise ProbeError("nas sin configurar en el inventario")
    volume = params.get("volume", "/Volume1")
    rows = await snmp_storage(nas["url"], community)
    row = next((r for r in rows if r["name"] == volume), None)
    if row is None:
        raise ProbeError(f"volumen {volume} no encontrado")
    used = float(row["percent"])
    return Truth(
        "number",
        used,
        f"{volume} usado {used}% (libre {100 - used}%)",
        {"alternatives": [100 - used]},
    )  # vale dar % usado o % libre


async def cloudflare_tunnels(cfg: Config, params: dict[str, Any]) -> Truth:
    from argos.mcp_servers.cloudflare.rest import CloudflareClient

    inv = load_inventory(cfg.root)
    token = inv.secret("cloudflare", "api_key") or inv.secret("cloudflare", "token")
    if not token:
        raise ProbeError("cloudflare sin configurar en el inventario")
    client = CloudflareClient(token)
    try:
        accounts = {(z.get("account") or {}).get("id") for z in await client.zones()} - {None}
        tunnels = [t for a in sorted(accounts) for t in await client.tunnels(a)]
    finally:
        await client.aclose()
    bad = sorted(t.get("name", "?") for t in tunnels if t.get("status") != "healthy")
    if bad:
        return Truth("names", bad, f"túneles con problemas: {bad} de {len(tunnels)}")
    return Truth("count", len(tunnels), f"{len(tunnels)} túneles, todos healthy", {"all_ok": True})


async def notion_todo_pending(cfg: Config, params: dict[str, Any]) -> Truth:
    from argos.mcp_servers.notion.convert import prop_value
    from argos.mcp_servers.notion.rest import NotionClient

    inv = load_inventory(cfg.root)
    token = inv.secret("notion", "api_key") or inv.secret("notion", "token")
    if not token:
        raise ProbeError("notion sin configurar en el inventario")
    client = NotionClient(token)
    try:
        rows = await client.query(
            params["database_id"],
            {
                "and": [
                    {"property": "Estado", "status": {"does_not_equal": "Completado"}},
                    {"property": "Estado", "status": {"does_not_equal": "Cancelado"}},
                ]
            },
            None,
            200,
        )
    finally:
        await client.aclose()
    names = sorted(
        prop_value(r["properties"][params.get("title", "Nombre tarea")]) or "?" for r in rows
    )
    return Truth("names", names, f"{len(names)} pendientes: {names}")


async def ha_lights_on(cfg: Config, params: dict[str, Any]) -> Truth:
    from argos.mcp_servers.homeassistant.rest import HomeAssistantClient

    inv = load_inventory(cfg.root)
    ha = inv.get("homeassistant")
    token = inv.secret("homeassistant", "token") or inv.secret("homeassistant", "api_key")
    if not ha.get("url") or not token:
        raise ProbeError("homeassistant sin configurar en el inventario")
    client = HomeAssistantClient(ha["url"], token)
    try:
        states = await client.states()
    finally:
        await client.aclose()
    on = sorted(
        (s.get("attributes") or {}).get("friendly_name") or s["entity_id"]
        for s in states
        if str(s.get("entity_id", "")).startswith("light.") and s.get("state") == "on"
    )
    # Los nombres suelen ser IDs Zigbee poco legibles: se mide el recuento.
    return Truth("count", len(on), f"{len(on)} luces encendidas: {on}")


def commute_decision(rows: list[dict], hours: set[int], rules: dict[str, float]) -> tuple[str, str]:
    """bici/coche con las reglas del trayecto sobre las horas indicadas (de un mismo día)."""
    reasons = []
    for r in rows:
        if int(r["hour"][:2]) not in hours:
            continue
        if (r.get("rain_mm") or 0) > rules["rain_mm"] or (r.get("rain_prob") or 0) >= rules[
            "rain_prob"
        ]:
            reasons.append(f"lluvia {r['hour']}")
        if (r.get("wind_kmh") or 0) >= rules["wind_kmh"]:
            reasons.append(f"viento {r['hour']}")
        if r.get("temp_c") is not None and r["temp_c"] < rules["temp_c"]:
            reasons.append(f"frío {r['hour']}")
    return ("coche" if reasons else "bici"), ", ".join(reasons) or "sin incidencias"


async def commute(cfg: Config, params: dict[str, Any]) -> Truth:
    """Decisión correcta del trayecto de mañana. «Entre 7 y 8» es ambiguo (¿incluye las 8:00?):
    si la decisión cambia según se incluyan o no las horas finales, se aceptan ambas."""
    from argos.mcp_servers.weather.eltiempo import fetch_hourly

    rules = {
        "rain_mm": 0.2,
        "rain_prob": 40,
        "wind_kmh": 30,
        "temp_c": 5,
        **params.get("rules", {}),
    }
    tz = ZoneInfo(params.get("timezone", "Europe/Madrid"))
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date().isoformat()
    rows = [r for r in await fetch_hourly(params.get("city", "barcelona")) if r["date"] == tomorrow]
    if not rows:
        raise ProbeError(f"sin previsión para {tomorrow}")
    strict, why_s = commute_decision(rows, {7, 8, 17, 18}, rules)
    loose, why_l = commute_decision(rows, {7, 17}, rules)
    return Truth(
        "choice",
        sorted({strict, loose}),
        f"{tomorrow}: con 7-8/17-18 → {strict} ({why_s}); con 7/17 → {loose} ({why_l})",
    )


PROBES = {
    "portainer.stopped": portainer_stopped,
    "nas.volume_used": nas_volume_used,
    "cloudflare.tunnels": cloudflare_tunnels,
    "notion.todo_pending": notion_todo_pending,
    "homeassistant.lights_on": ha_lights_on,
    "weather.commute": commute,
}


# --------------------------------------------------------------------------- comparación

_ZERO = re.compile(
    r"\b(ningun[oa]?|no hay|0|cero|todos? (estan|funcionan|operativ|healthy)|"
    r"todas? (estan )?apagad|nada)\b"
)


def _has_number(text: str, n: float, tol: float = 0.0) -> bool:
    for m in re.finditer(r"-?\d+(?:[.,]\d+)?", text):
        if abs(float(m.group().replace(",", ".")) - n) <= tol:
            return True
    return False


def judge(truth: Truth, answer: str, tolerance: float = 0.0) -> tuple[bool, str]:
    """¿La respuesta final coincide con la verdad? Devuelve (ok, detalle)."""
    text = norm(answer)
    if truth.kind == "count":
        n = int(truth.value)
        ok = (
            _has_number(text, n)
            or (truth.extra.get("all_ok") and bool(_ZERO.search(text)))
            or (n == 0 and bool(_ZERO.search(text)))
        )
        return ok, f"esperado {n} · {truth.detail}"
    if truth.kind == "names":
        names = list(truth.value)
        if not names:
            return bool(_ZERO.search(text)), f"esperado «ninguno» · {truth.detail}"
        missing = [n for n in names if norm(n) not in text]
        if len(names) > 10:  # muchos: basta con el recuento correcto
            return _has_number(text, len(names)), f"esperado {len(names)} · {truth.detail}"
        return not missing, (
            f"faltan {missing}" if missing else "todos mencionados"
        ) + f" · {truth.detail}"
    if truth.kind == "number":
        candidates = [float(truth.value), *truth.extra.get("alternatives", [])]
        ok = any(_has_number(text, c, tolerance) for c in candidates)
        return ok, f"esperado {candidates} ±{tolerance} · {truth.detail}"
    if truth.kind == "choice":
        first = norm((answer or "").strip().splitlines()[0] if answer else "")
        said = [c for c in ("bici", "coche") if c in first] or [
            c for c in ("bici", "coche") if c in text
        ][:1]
        ok = len(said) == 1 and said[0] in truth.value
        return ok, f"dijo {said or '?'}; válido {truth.value} · {truth.detail}"
    raise ValueError(f"tipo de verdad desconocido: {truth.kind}")


async def live_check(spec: dict[str, Any], answer: str, cfg: Config) -> tuple[bool, str]:
    probe = PROBES.get(spec["probe"])
    if probe is None:
        raise ValueError(f"sonda desconocida: {spec['probe']}")
    try:
        truth = await probe(cfg, spec.get("params", {}))
    except Exception as exc:  # noqa: BLE001 — sin verdad no se puede puntuar: se indica
        return False, f"sonda {spec['probe']} falló: {type(exc).__name__}: {exc}"
    return judge(truth, answer, float(spec.get("tolerance", 0)))
