"""Servidor MCP del NAS (TerraMaster): ficheros por SMB y almacenamiento por SNMP (UC-3).

- Lectura (`list`, `read`, `storage`): riesgo `read`, sin aprobación.
- `mkdir`: riesgo `write` (crear carpeta, bajo riesgo).
- `move` y `delete`: riesgo `destructive` (meta), aprobación humana (RF-GOV-04) y dry-run (RF-19).
- Credenciales (usuario/contraseña SMB, community SNMP) del inventario, nunca del modelo. Sin SSH
  ni ejecución de comandos: solo operaciones de fichero y lectura de contadores.

Entorno (lo inyecta Argos): ARGOS_NAS_HOST, ARGOS_NAS_USER, ARGOS_NAS_PASSWORD,
ARGOS_NAS_COMMUNITY, ARGOS_NAS_DRY_RUN.
"""

from __future__ import annotations

import asyncio
import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.nas.rest import NasError, NasSmb, snmp_storage

VERSION = "0.1.0"
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
ACTION_META = {"argos_risk": "destructive"}

server = MCPServer(name="nas", version=VERSION)


def _host() -> str | None:
    return os.environ.get("ARGOS_NAS_HOST") or None


def _smb() -> NasSmb | None:
    host, user, pw = (os.environ.get(k) for k in
                      ("ARGOS_NAS_HOST", "ARGOS_NAS_USER", "ARGOS_NAS_PASSWORD"))
    return NasSmb(host, user or "", pw or "") if host and user else None


def _dry_run() -> bool:
    return os.environ.get("ARGOS_NAS_DRY_RUN", "0") != "0"


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


_UNCONFIGURED = "NO CONFIGURADO: falta nas (url/user/password/community) en secrets/inventory.yaml."


@server.tool(annotations=READ)
async def list(path: str = "") -> str:
    """Lista un directorio del NAS por SMB. `path` = recurso/carpeta, p. ej. 'Public/peliculas'."""
    smb = _smb()
    if smb is None:
        return _UNCONFIGURED
    try:
        items = await asyncio.to_thread(smb.list_dir, path)
    except NasError as exc:
        return f"ERROR: {exc}"
    brief = [{"nombre": e["name"], "tipo": "dir" if e["dir"] else "fichero",
              "tamaño": "" if e["dir"] else _size(e["size"])} for e in items]
    return json.dumps(brief, ensure_ascii=False, indent=2)[:12000] if brief else "(vacío)"


@server.tool(annotations=READ)
async def read(path: str) -> str:
    """Lee un fichero de texto del NAS por SMB (hasta 200 KB)."""
    smb = _smb()
    if smb is None:
        return _UNCONFIGURED
    try:
        return await asyncio.to_thread(smb.read_file, path)
    except NasError as exc:
        return f"ERROR: {exc}"


@server.tool(annotations=READ)
async def storage() -> str:
    """Muestra el uso de los volúmenes del NAS por SNMP (total, usado, %)."""
    host, community = _host(), os.environ.get("ARGOS_NAS_COMMUNITY")
    if not host or not community:
        return _UNCONFIGURED
    try:
        rows = await snmp_storage(host, community)
    except NasError as exc:
        return f"ERROR: {exc}"
    # Solo sistemas de ficheros (rutas que empiezan por "/"): son los volúmenes de disco. Se deja
    # fuera memoria/swap para no confundirlos con "disco lleno". Si no hay, se muestran todos.
    fs = [r for r in rows if r["name"].startswith("/")] or rows
    out = [{"volumen": r["name"], "total": _size(r["total"]), "usado": _size(r["used"]),
            "porcentaje": f"{r['percent']}%"} for r in fs]
    return json.dumps(out, ensure_ascii=False, indent=2)[:12000] if out else "(sin datos)"


@server.tool(annotations=WRITE)
async def mkdir(path: str) -> str:
    """Crea una carpeta en el NAS por SMB."""
    smb = _smb()
    if smb is None:
        return _UNCONFIGURED
    if _dry_run():
        return f"[dry-run] no se crea. Carpeta prevista: {path}"
    try:
        await asyncio.to_thread(smb.mkdir, path)
        return f"carpeta creada: {path}"
    except NasError as exc:
        return f"ERROR: {exc}"


@server.tool(annotations=ACTION, meta=ACTION_META)
async def move(src: str, dst: str) -> str:
    """Mueve o renombra un fichero/carpeta dentro del mismo recurso del NAS. Requiere aprobación."""
    smb = _smb()
    if smb is None:
        return _UNCONFIGURED
    if _dry_run():
        return f"[dry-run] no se mueve. Previsto: {src} -> {dst}"
    try:
        await asyncio.to_thread(smb.move, src, dst)
        return f"movido: {src} -> {dst}"
    except NasError as exc:
        return f"ERROR: {exc}"


@server.tool(annotations=ACTION, meta=ACTION_META)
async def delete(path: str) -> str:
    """Borra un fichero (o una carpeta vacía) del NAS. Requiere aprobación. No borra carpetas con
    contenido."""
    smb = _smb()
    if smb is None:
        return _UNCONFIGURED
    if _dry_run():
        return f"[dry-run] no se borra. Previsto: {path}"
    try:
        await asyncio.to_thread(smb.delete, path)
        return f"borrado: {path}"
    except NasError as exc:
        return f"ERROR: {exc}"


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
