"""Servidor MCP de Notion (UC-4): buscar, leer y escribir en el workspace propio.

- Lectura (`search`, `read_page`, `database_schema`, `query_database`): riesgo `read`. Lo leído es
  DATO: el workspace contiene material externo (OSINT, newsletters, transcripciones) y Argos lo
  envuelve como <untrusted>; por eso este MCP vive en un perfil sin credenciales potentes (P2).
- Escritura reversible (`create_page`, `append`, `update_properties`): riesgo `write`, auditado.
- Archivar (`archive`, equivale a borrar): riesgo `destructive` (meta) → aprobación humana
  (RF-GOV-04) y dry-run (RF-19).
- El token llega por entorno desde el inventario, nunca del modelo. El alcance real es lo que se
  haya compartido con la integración en Notion. Guía de uso: skill `notion`.

Entorno (lo inyecta Argos): ARGOS_NOTION_TOKEN, ARGOS_NOTION_DRY_RUN ("1"/"0").
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.notion.convert import (
    PropertyError,
    block_text,
    brief,
    markdown_to_blocks,
    normalize_id,
    row,
    schema,
    title_of,
    to_notion_props,
)
from argos.mcp_servers.notion.rest import NotionClient, NotionError

VERSION = "0.1.0"
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
ACTION_META = {"argos_risk": "destructive"}
MAX_READ_CHARS = 30000
MAX_WRITE_CHARS = 50000

server = MCPServer(name="notion", version=VERSION)


def _configured() -> str | None:
    if not os.environ.get("ARGOS_NOTION_TOKEN"):
        return (
            "Notion no está configurado: falta el api_key en secrets/inventory.yaml "
            "(servicio 'notion')."
        )
    return None


def _client() -> NotionClient:
    return NotionClient(os.environ["ARGOS_NOTION_TOKEN"])


def _dry_run() -> bool:
    return os.environ.get("ARGOS_NOTION_DRY_RUN", "0") != "0"


def _dump(obj: Any, limit: int = 12000) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)[:limit]


def _parse_json(raw: str, kind: type, what: str) -> Any:
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except ValueError as exc:
        raise PropertyError(f"{what}: JSON inválido ({exc})") from exc
    if not isinstance(val, kind):
        raise PropertyError(f"{what}: se esperaba un {kind.__name__} JSON")
    return val


@server.tool(annotations=READ)
async def search(query: str = "", type: str = "", limit: int = 20) -> str:
    """Busca páginas y bases de datos por título (más recientes primero). type = "page",
    "database" o vacío. Devuelve id, título, url, última edición y padre."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    if type not in ("", "page", "database"):
        return "type debe ser 'page', 'database' o vacío"
    client = _client()
    try:
        res = await client.search(query, type, max(1, min(int(limit), 100)))
        return _dump([brief(o) for o in res])
    except NotionError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


async def _render(
    client: NotionClient, block_id: str, depth: int, budget: list[int], out: list[str]
) -> None:
    number = 0
    for b in await client.children(block_id):
        if budget[0] <= 0:
            return
        number = number + 1 if b.get("type") == "numbered_list_item" else 0
        line = "  " * depth + block_text(b, number or 1)
        out.append(line)
        budget[0] -= len(line) + 1
        if (
            b.get("has_children")
            and depth < 2
            and b.get("type") not in ("child_page", "child_database")
        ):
            await _render(client, b["id"], depth + 1, budget, out)


@server.tool(annotations=READ)
async def read_page(page_id: str) -> str:
    """Lee una página (id o URL de Notion): título, propiedades y contenido como texto tipo
    Markdown (hasta 2 niveles de anidación). Las subpáginas se listan con su id."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    pid = normalize_id(page_id)
    if not pid:
        return "RECHAZADO: id de página inválido"
    client = _client()
    try:
        page = await client.page(pid)
        props = {k: v for k, v in row(page).items() if k not in ("id", "url")}
        out = [
            f"# {title_of(page)}",
            f"id: {page['id']}  url: {page.get('url')}",
            f"propiedades: {json.dumps(props, ensure_ascii=False)}",
            "",
        ]
        budget = [MAX_READ_CHARS]
        await _render(client, pid, 0, budget, out)
        if budget[0] <= 0:
            out.append("[… contenido truncado]")
        return "\n".join(out)
    except NotionError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def database_schema(database_id: str) -> str:
    """Esquema de una base de datos: cada propiedad con su tipo y, en select/multi_select/status,
    sus opciones válidas. Consúltalo SIEMPRE antes de crear o actualizar filas."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    did = normalize_id(database_id)
    if not did:
        return "RECHAZADO: id de base de datos inválido"
    client = _client()
    try:
        db = await client.database(did)
        return _dump(
            {"id": db["id"], "title": title_of(db), "url": db.get("url"), "properties": schema(db)}
        )
    except NotionError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def query_database(
    database_id: str, filter: str = "", sorts: str = "", limit: int = 50
) -> str:
    """Filas de una base de datos con propiedades simplificadas. filter y sorts son JSON con la
    sintaxis del API de Notion, p. ej. filter={"property":"Estado","status":{"equals":"En
    progreso"}} y sorts=[{"property":"Fecha objetivo","direction":"ascending"}]."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    did = normalize_id(database_id)
    if not did:
        return "RECHAZADO: id de base de datos inválido"
    client = _client()
    try:
        flt = _parse_json(filter, dict, "filter")
        srt = _parse_json(sorts, list, "sorts")
        rows = await client.query(did, flt, srt, max(1, min(int(limit), 200)))
        return _dump([row(p) for p in rows], 20000)
    except (NotionError, PropertyError) as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=WRITE)
async def create_page(
    parent_id: str,
    title: str = "",
    content: str = "",
    properties: str = "",
    parent_type: str = "page",
) -> str:
    """Crea una página. parent_type="page": subpágina de parent_id con ese title.
    parent_type="database": fila nueva en la base parent_id; properties es un JSON de valores
    simples por nombre de propiedad (p. ej. {"Nombre tarea":"…","Estado":"Sin empezar",
    "Área":["NAS"],"Fecha objetivo":"2026-10-05"}); si falta el título se usa title.
    content = cuerpo en Markdown sencillo (#, -, 1., - [ ], >, ```, ---, **negrita**, `código`,
    [enlace](https://…)). Devuelve id y url."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    pid = normalize_id(parent_id)
    if not pid or parent_type not in ("page", "database"):
        return "RECHAZADO: parent_id o parent_type inválido"
    if len(content) > MAX_WRITE_CHARS:
        return f"RECHAZADO: contenido demasiado largo (máx {MAX_WRITE_CHARS} caracteres)"
    client = _client()
    try:
        values = _parse_json(properties, dict, "properties") or {}
        if parent_type == "database":
            db = await client.database(pid)
            sch = db.get("properties") or {}
            title_prop = next(n for n, p in sch.items() if p["type"] == "title")
            if title and title_prop not in values:
                values[title_prop] = title
            if not values.get(title_prop):
                return f"RECHAZADO: falta el título ('{title_prop}')"
            props = to_notion_props(values, sch)
            parent = {"database_id": pid}
        else:
            if values:
                return "RECHAZADO: properties solo aplica a filas de base de datos"
            if not title:
                return "RECHAZADO: falta title"
            props = to_notion_props({"title": title}, {"title": {"type": "title"}})
            parent = {"page_id": pid}
        page = await client.create_page(parent, props, markdown_to_blocks(content))
        return _dump({"created": True, "id": page["id"], "url": page.get("url")})
    except (NotionError, PropertyError) as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=WRITE)
async def append(page_id: str, content: str) -> str:
    """Añade contenido (Markdown sencillo, como en create_page) al final de una página."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    pid = normalize_id(page_id)
    if not pid:
        return "RECHAZADO: id de página inválido"
    if not content.strip() or len(content) > MAX_WRITE_CHARS:
        return f"RECHAZADO: contenido vacío o demasiado largo (máx {MAX_WRITE_CHARS})"
    client = _client()
    try:
        blocks = markdown_to_blocks(content)
        await client.append(pid, blocks)
        return f"Añadidos {len(blocks)} bloques a {pid}."
    except NotionError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=WRITE)
async def update_properties(page_id: str, properties: str) -> str:
    """Actualiza propiedades de una fila de base de datos. properties = JSON de valores simples
    por nombre (p. ej. {"Estado":"Completado","Evidencia / Resultado":"…"}). Solo cambia lo
    indicado; valida contra el esquema real (opciones existentes, tipos)."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    pid = normalize_id(page_id)
    if not pid:
        return "RECHAZADO: id de página inválido"
    client = _client()
    try:
        values = _parse_json(properties, dict, "properties")
        if not values:
            return "nada que cambiar"
        page = await client.page(pid)
        parent = page.get("parent") or {}
        if parent.get("type") != "database_id":
            return "RECHAZADO: la página no es una fila de base de datos"
        db = await client.database(parent["database_id"])
        props = to_notion_props(values, db.get("properties") or {})
        await client.update_page(pid, {"properties": props})
        return f"Actualizado {title_of(page)!r}: {', '.join(values)}"
    except (NotionError, PropertyError) as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=ACTION, meta=ACTION_META)
async def archive(page_id: str) -> str:
    """Archiva (borra; recuperable desde la papelera de Notion unos días) una página o fila.
    Requiere aprobación."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    pid = normalize_id(page_id)
    if not pid:
        return "RECHAZADO: id de página inválido"
    client = _client()
    try:
        page = await client.page(pid)
        name = title_of(page) or pid
        if _dry_run():
            return f"[dry-run] no se ejecuta. Se archivaría: {name!r} ({page.get('url')})"
        await client.update_page(pid, {"archived": True})
        return f"Archivada {name!r}."
    except NotionError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
