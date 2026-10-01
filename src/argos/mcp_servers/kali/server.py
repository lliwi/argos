"""Servidor MCP de Kali para auditoría de servicios propios (UC-2).

Toda la gobernanza vive aquí y en Argos, no en el contenedor Kali:
- El alcance (RF-SEC-06) y la autorización (RF-LEG-01) se comprueban antes de cada llamada; sin
  autorización o con un objetivo fuera de alcance, la acción se rechaza (fail-closed).
- Los argumentos extra se sanean: sin metacaracteres de shell y sin hosts fuera de alcance.
- En `dry_run` (RF-19, por defecto en el perfil pentest) se devuelve el comando previsto sin
  ejecutarlo.
- Cada tool se declara `offensive` (meta `argos_risk`), lo que obliga a aprobación humana en
  Argos antes de ejecutarse (RF-GOV-04).

Configuración por entorno (la inyecta Argos al conectar el MCP):
  ARGOS_KALI_URL, ARGOS_KALI_TOKEN, ARGOS_PENTEST_SCOPE (coma), ARGOS_PENTEST_AUTH,
  ARGOS_PENTEST_DRY_RUN ("1"/"0").
"""

from __future__ import annotations

import os
import re

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.kali.rest import KaliError, KaliRestClient
from argos.pentest import Scope, ScopeError, extract_targets

VERSION = "0.1.0"

# Metacaracteres de shell prohibidos en argumentos extra: el objetivo es evitar inyección aunque
# el servidor Kali fuese permisivo.
_FORBIDDEN = re.compile(r"[;&|`$><\n\r\\\"']|\$\(|\|\|")

OFFENSIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
OFF_META = {"argos_risk": "offensive"}
MANAGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)

# Nombre de paquete apt válido: nunca contiene espacios, rutas ni metacaracteres, así que no
# puede transportar un comando. Es lo único que el agente aporta a `kali.install`.
_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,80}$")

server = MCPServer(name="kali", version=VERSION)


def _scope() -> Scope:
    allow = tuple(
        h.strip() for h in os.environ.get("ARGOS_PENTEST_SCOPE", "").split(",") if h.strip()
    )
    return Scope(allow, os.environ.get("ARGOS_PENTEST_AUTH") or None)


def _dry_run() -> bool:
    return os.environ.get("ARGOS_PENTEST_DRY_RUN", "1") != "0"


def _client() -> KaliRestClient:
    return KaliRestClient(
        os.environ.get("ARGOS_KALI_URL", "http://kali:8000"),
        os.environ.get("ARGOS_KALI_TOKEN") or None,
    )


def _validate(tool: str, target: str, extra_args: str) -> None:
    if _FORBIDDEN.search(extra_args or ""):
        raise ScopeError("argumentos con metacaracteres de shell no permitidos")
    # Todos los hosts, tanto el objetivo como los que aparezcan en los argumentos, deben estar en
    # alcance. Así un -oN a otro host o un --script contra un tercero también se rechaza.
    targets = extract_targets(target) + extract_targets(extra_args)
    _scope().check(targets or extract_targets(target))
    if not extract_targets(target):
        raise ScopeError("objetivo vacío o no reconocible")


# Clave con la que cada endpoint del servidor Kali espera el objetivo.
_TARGET_KEY = {
    "nmap": "target",
    "nikto": "target",
    "gobuster": "url",
    "dirb": "url",
    "sqlmap": "url",
    "wpscan": "url",
}


async def _run(tool: str, target: str, extra_args: str) -> str:
    try:
        _validate(tool, target, extra_args)
    except ScopeError as exc:
        return f"RECHAZADO: {exc}"
    payload = {_TARGET_KEY[tool]: target, "additional_args": extra_args}
    if _dry_run():
        return (
            f"[dry-run] no se ejecuta. Herramienta {tool} contra {target} "
            f"(args: {extra_args or 'por defecto'})"
        )
    client = _client()
    try:
        return await client.run_tool(tool, payload)
    except KaliError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=OFFENSIVE, meta=OFF_META)
async def nmap(target: str, additional_args: str = "-sV -T4") -> str:
    """Escaneo de puertos/servicios con nmap contra un host propio en alcance."""
    return await _run("nmap", target, additional_args)


@server.tool(annotations=OFFENSIVE, meta=OFF_META)
async def dirb(target: str, additional_args: str = "") -> str:
    """Descubrimiento de rutas con dirb sobre una URL propia en alcance."""
    return await _run("dirb", target, additional_args)


@server.tool(annotations=OFFENSIVE, meta=OFF_META)
async def nikto(target: str, additional_args: str = "") -> str:
    """Escáner de vulnerabilidades web contra un servicio propio en alcance."""
    return await _run("nikto", target, additional_args)


@server.tool(annotations=OFFENSIVE, meta=OFF_META)
async def gobuster(target: str, additional_args: str = "") -> str:
    """Descubrimiento de rutas/directorios en un servicio web propio en alcance."""
    return await _run("gobuster", target, additional_args)


@server.tool(annotations=OFFENSIVE, meta=OFF_META)
async def wpscan(target: str, additional_args: str = "") -> str:
    """Auditoría de un WordPress propio en alcance."""
    return await _run("wpscan", target, additional_args)


@server.tool(annotations=OFFENSIVE, meta=OFF_META)
async def sqlmap(target: str, additional_args: str = "--batch") -> str:
    """Prueba de inyección SQL contra un endpoint propio en alcance."""
    return await _run("sqlmap", target, additional_args)


# --- gestión del propio contenedor Kali (instalar herramientas, refrescar índices) -------------
# No hay ejecución de comandos libres: `packages` son nombres de paquete apt validados y el
# comando lo compone el cliente REST. Requiere autorización de pentest (perfil configurado).


def _parse_packages(spec: str) -> list[str]:
    names = [p for p in re.split(r"[,\s]+", (spec or "").strip()) if p]
    bad = [p for p in names if not _PKG_RE.match(p)]
    if bad:
        raise ScopeError(f"nombre(s) de paquete no válidos: {', '.join(bad)}")
    return names


@server.tool(annotations=MANAGE)
async def install(packages: str) -> str:
    """Instala herramientas apt en el contenedor Kali (nombres separados por espacio o coma),
    p. ej. 'ffuf feroxbuster'. Solo instala paquetes, no ejecuta comandos."""
    if not _scope().authorized:
        return (
            "RECHAZADO: perfil de pentest sin alcance/autorización; configura `scope` y "
            "`authorization_ref` antes de provisionar el entorno."
        )
    try:
        names = _parse_packages(packages)
    except ScopeError as exc:
        return f"RECHAZADO: {exc}"
    if not names:
        return "RECHAZADO: no se indicó ningún paquete."
    if _dry_run():
        return f"[dry-run] no se instala. Paquetes previstos: {' '.join(names)}"
    client = _client()
    try:
        return await client.apt_install(names)
    except KaliError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=MANAGE)
async def apt_update() -> str:
    """Refresca el índice de paquetes apt del contenedor Kali (antes de instalar)."""
    if not _scope().authorized:
        return "RECHAZADO: perfil de pentest sin alcance/autorización."
    if _dry_run():
        return "[dry-run] no se ejecuta. Se refrescaría el índice de paquetes (apt-get update)."
    client = _client()
    try:
        return await client.apt_update()
    except KaliError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
