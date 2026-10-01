"""Servidor MCP de Cloudflare para gestión de los dominios propios (UC-3).

- Lectura (zonas, registros DNS, túneles, analítica): riesgo `read`, sin aprobación.
- Cambios DNS (crear/modificar/borrar registro): riesgo `destructive` (meta) — publican o retiran
  servicios en Internet —, obligan a aprobación humana en Argos (RF-GOV-04) y respetan dry-run
  (RF-19).
- El token llega por entorno desde el inventario, nunca del modelo. Sin Workers, WAF, ajustes de
  zona ni gestión de tokens. Si el token no tiene un permiso, la herramienta dice cuál falta.

Entorno (lo inyecta Argos): ARGOS_CF_TOKEN, ARGOS_CF_DRY_RUN ("1"/"0").
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.cloudflare.rest import (
    CloudflareClient,
    CloudflareError,
    CloudflarePermissionError,
)

VERSION = "0.1.0"
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
ACTION_META = {"argos_risk": "destructive"}

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LABELS = re.compile(
    r"^(\*|[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?)"
    r"(\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?)*$"
)
_RECORD_ID = re.compile(r"^[0-9a-f]{32}$")
# Tipos simples (contenido = cadena). SRV/CAA/etc. llevan datos estructurados: fuera de alcance.
TYPES = {"A", "AAAA", "CNAME", "TXT", "MX"}
# Qué permiso del token habilita cada cosa (para mensajes accionables).
PERM_HINT = {
    "dns": "Zone › DNS › Edit (o Read para listar)",
    "tunnels": "Account › Cloudflare Tunnel › Read",
    "analytics": "Zone › Analytics › Read",
}

server = MCPServer(name="cloudflare", version=VERSION)


def _configured() -> str | None:
    if not os.environ.get("ARGOS_CF_TOKEN"):
        return (
            "Cloudflare no está configurado: falta el api_key (token) en "
            "secrets/inventory.yaml (servicio 'cloudflare')."
        )
    return None


def _client() -> CloudflareClient:
    return CloudflareClient(os.environ["ARGOS_CF_TOKEN"])


def _dry_run() -> bool:
    return os.environ.get("ARGOS_CF_DRY_RUN", "0") != "0"


def _err(exc: CloudflareError, what: str) -> str:
    if isinstance(exc, CloudflarePermissionError):
        return (
            f"SIN PERMISO: el token de Cloudflare no tiene acceso ({exc}). "
            f"Permiso necesario: {PERM_HINT[what]}."
        )
    return f"ERROR: {exc}"


def _fqdn(name: str, zone: str) -> str | None:
    """Normaliza el nombre del registro a FQDN dentro de la zona; None si es inválido o ajeno."""
    name = name.strip().lower().rstrip(".")
    if name in ("", "@"):
        return zone
    if name == zone:
        return name
    if name.endswith(f".{zone}"):
        return name if _LABELS.match(name[: -len(zone) - 1]) else None
    # Relativo solo de una etiqueta: "x.otro.org" sería ambiguo (¿subdominio o dominio ajeno?).
    return f"{name}.{zone}" if "." not in name and _LABELS.match(name) else None


def _content_ok(rtype: str, content: str) -> bool:
    if not content or len(content) > 2048 or any(c in content for c in "\r\n\x00"):
        return False
    if rtype == "A":
        return bool(re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", content))
    if rtype == "AAAA":
        return ":" in content and bool(re.fullmatch(r"[0-9a-fA-F:.]+", content))
    if rtype in ("CNAME", "MX"):
        return bool(_DOMAIN.match(content.lower().rstrip(".")))
    return True  # TXT: texto libre de una línea


async def _zone(client: CloudflareClient, zone: str) -> dict | None:
    zone = zone.strip().lower().rstrip(".")
    for z in await client.zones():
        if zone in (z.get("name"), z.get("id")):
            return z
    return None


def _brief(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "type": r.get("type"),
        "name": r.get("name"),
        "content": r.get("content"),
        "proxied": r.get("proxied"),
        "ttl": r.get("ttl"),
        **({"priority": r["priority"]} if r.get("priority") is not None else {}),
    }


@server.tool(annotations=READ)
async def zones() -> str:
    """Lista las zonas (dominios) gestionadas: nombre, estado y plan."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    client = _client()
    try:
        return json.dumps(
            [
                {
                    "name": z.get("name"),
                    "id": z.get("id"),
                    "status": z.get("status"),
                    "plan": (z.get("plan") or {}).get("name"),
                }
                for z in await client.zones()
            ],
            ensure_ascii=False,
            indent=2,
        )
    except CloudflareError as exc:
        return _err(exc, "dns")
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def dns_list(zone: str, type: str = "", name: str = "") -> str:
    """Lista los registros DNS de una zona (p. ej. zone=ejemplo.com). Filtros opcionales: type
    (A, CNAME…) y name (subdominio). Los CNAME a *.cfargotunnel.com son Cloudflare Tunnel."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    rtype = type.strip().upper()
    if rtype and rtype not in TYPES | {"NS", "SRV", "CAA", "PTR"}:
        return "tipo de registro inválido"
    client = _client()
    try:
        z = await _zone(client, zone)
        if not z:
            return f"zona '{zone}' no encontrada (usa cloudflare.zones)"
        fqdn = _fqdn(name, z["name"]) if name else ""
        if fqdn is None:
            return "name inválido"
        recs = await client.dns_records(z["id"], rtype, fqdn)
        return json.dumps([_brief(r) for r in recs], ensure_ascii=False, indent=2)[:12000]
    except CloudflareError as exc:
        return _err(exc, "dns")
    finally:
        await client.aclose()


@server.tool(annotations=ACTION, meta=ACTION_META)
async def dns_create(
    zone: str,
    type: str,
    name: str,
    content: str,
    proxied: bool = False,
    ttl: int = 1,
    priority: int = 10,
) -> str:
    """Crea un registro DNS (type A, AAAA, CNAME, TXT o MX). name relativo ("www", "@") o FQDN
    dentro de la zona. ttl=1 es automático. proxied solo aplica a A/AAAA/CNAME. Requiere
    aprobación."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    rtype = type.strip().upper()
    if rtype not in TYPES:
        return f"RECHAZADO: tipo no soportado (usa uno de {sorted(TYPES)})"
    if not _content_ok(rtype, content):
        return f"RECHAZADO: contenido inválido para un registro {rtype}"
    if not (ttl == 1 or 60 <= ttl <= 86400) or not 0 <= priority <= 65535:
        return "RECHAZADO: ttl (1 o 60..86400) o priority fuera de rango"
    client = _client()
    try:
        z = await _zone(client, zone)
        if not z:
            return f"zona '{zone}' no encontrada (usa cloudflare.zones)"
        fqdn = _fqdn(name, z["name"])
        if fqdn is None:
            return "RECHAZADO: name inválido o fuera de la zona"
        record = {"type": rtype, "name": fqdn, "content": content, "ttl": ttl}
        if rtype in ("A", "AAAA", "CNAME"):
            record["proxied"] = bool(proxied)
        if rtype == "MX":
            record["priority"] = priority
        if _dry_run():
            return f"[dry-run] no se ejecuta. Se crearía en {z['name']}: {json.dumps(record)}"
        created = await client.dns_create(z["id"], record)
        return "Registro creado: " + json.dumps(_brief(created), ensure_ascii=False)
    except CloudflareError as exc:
        return _err(exc, "dns")
    finally:
        await client.aclose()


@server.tool(annotations=ACTION, meta=ACTION_META)
async def dns_update(
    zone: str, record_id: str, content: str = "", proxied: str = "", ttl: int = 0
) -> str:
    """Modifica un registro DNS existente (id de cloudflare.dns_list): content nuevo, proxied
    ("true"/"false") y/o ttl. Solo cambia lo indicado. Requiere aprobación."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    if not _RECORD_ID.match(record_id):
        return "RECHAZADO: record_id inválido"
    if proxied not in ("", "true", "false"):
        return "RECHAZADO: proxied debe ser 'true', 'false' o vacío"
    if ttl and not (ttl == 1 or 60 <= ttl <= 86400):
        return "RECHAZADO: ttl fuera de rango (1 o 60..86400)"
    client = _client()
    try:
        z = await _zone(client, zone)
        if not z:
            return f"zona '{zone}' no encontrada (usa cloudflare.zones)"
        current = next(
            (r for r in await client.dns_records(z["id"]) if r.get("id") == record_id), None
        )
        if not current:
            return f"registro {record_id} no encontrado en {z['name']}"
        changes: dict = {}
        if content:
            if current.get("type") not in TYPES or not _content_ok(current["type"], content):
                return f"RECHAZADO: contenido inválido para un registro {current.get('type')}"
            changes["content"] = content
        if proxied:
            changes["proxied"] = proxied == "true"
        if ttl:
            changes["ttl"] = ttl
        if not changes:
            return "nada que cambiar"
        if _dry_run():
            return (
                f"[dry-run] no se ejecuta. {current.get('type')} {current.get('name')}: "
                f"{json.dumps(changes)}"
            )
        updated = await client.dns_update(z["id"], record_id, changes)
        return "Registro actualizado: " + json.dumps(_brief(updated), ensure_ascii=False)
    except CloudflareError as exc:
        return _err(exc, "dns")
    finally:
        await client.aclose()


@server.tool(annotations=ACTION, meta=ACTION_META)
async def dns_delete(zone: str, record_id: str) -> str:
    """Borra un registro DNS (id de cloudflare.dns_list). Requiere aprobación."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    if not _RECORD_ID.match(record_id):
        return "RECHAZADO: record_id inválido"
    client = _client()
    try:
        z = await _zone(client, zone)
        if not z:
            return f"zona '{zone}' no encontrada (usa cloudflare.zones)"
        current = next(
            (r for r in await client.dns_records(z["id"]) if r.get("id") == record_id), None
        )
        if not current:
            return f"registro {record_id} no encontrado en {z['name']}"
        desc = f"{current.get('type')} {current.get('name')} -> {current.get('content')}"
        if _dry_run():
            return f"[dry-run] no se ejecuta. Se borraría: {desc}"
        await client.dns_delete(z["id"], record_id)
        return f"Registro borrado: {desc}"
    except CloudflareError as exc:
        return _err(exc, "dns")
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def tunnels() -> str:
    """Lista los Cloudflare Tunnels de la cuenta: nombre, estado (healthy/degraded/down) y
    conectores activos."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    client = _client()
    try:
        accounts = {(z.get("account") or {}).get("id") for z in await client.zones()} - {None}
        out = []
        for acc in sorted(accounts):
            for t in await client.tunnels(acc):
                conns = t.get("connections") or []
                out.append(
                    {
                        "name": t.get("name"),
                        "id": t.get("id"),
                        "status": t.get("status"),
                        "connectors": len({c.get("client_id") for c in conns}),
                        "colos": sorted({c.get("colo_name") for c in conns} - {None}),
                    }
                )
        if not out:
            # Sin permiso de cuenta Cloudflare responde 200 con lista vacía, no 403.
            return (
                "No se ve ningún túnel. Si hay CNAME a *.cfargotunnel.com, al token le falta "
                f"el permiso {PERM_HINT['tunnels']}."
            )
        return json.dumps(out, ensure_ascii=False, indent=2)[:12000]
    except CloudflareError as exc:
        return _err(exc, "tunnels")
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def analytics(zone: str, days: int = 1) -> str:
    """Tráfico de una zona en los últimos días (1..30): peticiones, cacheadas, bytes, amenazas,
    visitas y visitantes únicos por día."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    days = max(1, min(int(days), 30))
    client = _client()
    try:
        z = await _zone(client, zone)
        if not z:
            return f"zona '{zone}' no encontrada (usa cloudflare.zones)"
        since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days - 1)).isoformat()
        data = await client.zone_analytics(z["id"], since)
        rows = [
            {
                "date": (g.get("dimensions") or {}).get("date"),
                **(g.get("sum") or {}),
                "uniques": (g.get("uniq") or {}).get("uniques"),
            }
            for g in data.get("httpRequests1dGroups") or []
        ]
        return json.dumps({"zone": z["name"], "days": rows}, ensure_ascii=False, indent=2)
    except CloudflareError as exc:
        return _err(exc, "analytics")
    finally:
        await client.aclose()


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
