"""Servidor MCP de descargas: Jackett (buscar) + Transmission (descargar/gestionar) (UC-3).

- `search` y `downloads`: riesgo `read`, sin aprobación.
- `add` y `control` (start/stop/remove): riesgo `destructive` (meta), obligan a aprobación humana
  (RF-GOV-04) y respetan dry-run (RF-19).
- URLs y api key vienen del inventario, nunca del modelo. Sin ejecución de comandos.

Entorno (lo inyecta Argos): ARGOS_JACKETT_URL, ARGOS_JACKETT_KEY, ARGOS_TRANSMISSION_URL,
ARGOS_TRANSMISSION_USER, ARGOS_TRANSMISSION_PASS, ARGOS_MEDIA_DRY_RUN.
"""

from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.media.rest import JackettClient, MediaError, TransmissionClient

VERSION = "0.1.0"
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
ACTION_META = {"argos_risk": "destructive"}

_STATUS = {
    0: "parado",
    1: "en cola (check)",
    2: "verificando",
    3: "en cola",
    4: "descargando",
    5: "en cola (seed)",
    6: "compartiendo",
}

server = MCPServer(name="media", version=VERSION)


def _jackett() -> JackettClient | None:
    url, key = os.environ.get("ARGOS_JACKETT_URL"), os.environ.get("ARGOS_JACKETT_KEY")
    return JackettClient(url, key) if url and key else None


def _transmission() -> TransmissionClient | None:
    url = os.environ.get("ARGOS_TRANSMISSION_URL")
    return (
        TransmissionClient(
            url,
            os.environ.get("ARGOS_TRANSMISSION_USER", ""),
            os.environ.get("ARGOS_TRANSMISSION_PASS", ""),
        )
        if url
        else None
    )


def _dry_run() -> bool:
    return os.environ.get("ARGOS_MEDIA_DRY_RUN", "0") != "0"


def _size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# Últimos resultados de búsqueda de ESTA sesión (el MCP es un subproceso por sesión). El enlace
# real (que puede llevar la api key de Jackett) se guarda aquí y se resuelve al descargar por
# número, para que la api key NUNCA pase por el modelo (que la vería redactada → 401).
_LAST: list[dict] = []


@server.tool(annotations=READ)
async def search(query: str, category: str = "") -> str:
    """Busca en los indexadores de Jackett (category opcional: movies, tv, books, music).
    Devuelve los mejores resultados numerados. Para descargar uno, usa `media.add` con su
    número (`index`)."""
    client = _jackett()
    if client is None:
        return "NO CONFIGURADO: falta jackett (url + api_key) en secrets/inventory.yaml."
    try:
        results = await client.search(query, category)
    except MediaError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()
    results.sort(key=lambda r: r.get("Seeders") or 0, reverse=True)
    _LAST.clear()
    out = []
    for i, r in enumerate(results[:15], start=1):
        # El enlace real (magnet o /dl con api key) se guarda; no se muestra al modelo.
        _LAST.append({"title": r.get("Title"), "link": r.get("MagnetUri") or r.get("Link") or ""})
        out.append(
            {
                "index": i,
                "title": r.get("Title"),
                "size": _size(r.get("Size")),
                "seeders": r.get("Seeders"),
                "category": r.get("CategoryDesc"),
                "tracker": r.get("Tracker"),
            }
        )
    if not out:
        return "(sin resultados)"
    return (
        "Resultados (descarga uno con media.add index=<número>):\n"
        + json.dumps(out, ensure_ascii=False, indent=2)[:12000]
    )


@server.tool(annotations=READ)
async def downloads() -> str:
    """Lista las descargas actuales en Transmission (nombre, progreso, estado, velocidad)."""
    client = _transmission()
    if client is None:
        return "NO CONFIGURADO: falta transmission (url) en secrets/inventory.yaml."
    try:
        ts = await client.torrents()
    except MediaError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()
    brief = [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "progreso": f"{round((t.get('percentDone') or 0) * 100)}%",
            "estado": _STATUS.get(t.get("status"), t.get("status")),
            "velocidad": _size(t.get("rateDownload")) + "/s",
        }
        for t in ts
    ]
    return json.dumps(brief, ensure_ascii=False, indent=2)[:12000] if brief else "(sin descargas)"


@server.tool(annotations=ACTION, meta=ACTION_META)
async def add(index: int = 0, link: str = "") -> str:
    """Añade una descarga a Transmission. Lo normal: `index` = número de un resultado de
    `media.search` (el enlace real, con su api key si la lleva, lo resuelve el servidor y NUNCA
    pasa por ti). Alternativa: `link` = un magnet directo. Requiere aprobación."""
    title = ""
    if index:
        if not _LAST:
            return "RECHAZADO: no hay búsqueda reciente; usa primero media.search."
        if not 1 <= index <= len(_LAST):
            return f"RECHAZADO: índice fuera de rango (1..{len(_LAST)})."
        entry = _LAST[index - 1]
        real_link, title = entry["link"], entry["title"]
        if not real_link:
            return "RECHAZADO: ese resultado no tiene enlace de descarga."
    else:
        real_link = link.strip()
        if not real_link:
            return "RECHAZADO: indica el índice de un resultado (index) o un magnet (link)."
        # Un enlace suelto solo se acepta si es magnet: una URL http podría llevar secretos
        # que el modelo habría recibido redactados (→ inservibles). Para /dl usa index.
        if not real_link.lower().startswith("magnet:?"):
            return "RECHAZADO: para enlaces que no son magnet, usa media.add con el index."
    client = _transmission()
    if client is None:
        return "NO CONFIGURADO: falta transmission (url) en secrets/inventory.yaml."
    if _dry_run():
        return f"[dry-run] no se añade. Se descargaría: {title or real_link[:80]}"
    try:
        added = await client.add(real_link)
        return f"añadido: #{added.get('id')} {added.get('name') or title or '(sin nombre)'}"
    except MediaError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=ACTION, meta=ACTION_META)
async def control(torrent_id: int, action: str) -> str:
    """Controla una descarga por id: action = start | stop | remove. Requiere aprobación.
    (remove quita el torrent pero no borra los ficheros ya descargados.)"""
    if action not in ("start", "stop", "remove"):
        return "RECHAZADO: action debe ser start, stop o remove."
    client = _transmission()
    if client is None:
        return "NO CONFIGURADO: falta transmission (url) en secrets/inventory.yaml."
    if _dry_run():
        return f"[dry-run] no se ejecuta. Acción prevista: {action} sobre #{torrent_id}."
    try:
        await client.action(int(torrent_id), action)
        return f"{action} aplicado a #{torrent_id}."
    except MediaError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
