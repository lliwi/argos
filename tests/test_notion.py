"""MCP de Notion: búsqueda, lectura, escritura validada por esquema y archivado con HITL (UC-4)."""

from __future__ import annotations

import json

import httpx
import pytest

from argos.mcp_servers.notion.convert import (
    PropertyError,
    markdown_to_blocks,
    normalize_id,
    rich,
    to_notion_props,
)
from argos.mcp_servers.notion.rest import NotionClient

DB = "35c0e17c-c60a-802e-9f62-cbb0e1e2259b"
PAGE = "3a70e17c-c60a-814e-8e2a-c77299359958"
SCHEMA = {
    "Nombre tarea": {"type": "title", "title": {}},
    "Estado": {"type": "status", "status": {
        "options": [{"id": "1", "name": "Sin empezar"}, {"id": "2", "name": "Completado"}],
        "groups": [{"name": "To-do", "option_ids": ["1"]},
                   {"name": "Complete", "option_ids": ["2"]}]}},
    "Área": {"type": "multi_select", "multi_select": {"options": [{"name": "NAS"},
                                                                  {"name": "Seguridad"}]}},
    "Prioridad": {"type": "select", "select": {"options": [{"name": "Alta"}]}},
    "Fecha objetivo": {"type": "date", "date": {}},
    "Requiere aprobación": {"type": "checkbox", "checkbox": {}},
    "Última edición": {"type": "last_edited_time", "last_edited_time": {}},
}
ROW = {"object": "page", "id": PAGE, "url": "https://notion.so/x",
       "parent": {"type": "database_id", "database_id": DB},
       "properties": {"Nombre tarea": {"type": "title",
                                       "title": [{"plain_text": "Actualizar n8n"}]},
                      "Estado": {"type": "status", "status": {"name": "Sin empezar"}},
                      "Área": {"type": "multi_select", "multi_select": [{"name": "NAS"}]}}}


def test_normalize_id_accepts_ids_and_urls():
    want = "3a70e17c-c60a-814e-8e2a-c77299359958"
    assert normalize_id("3a70e17cc60a814e8e2ac77299359958") == want
    assert normalize_id(want.upper()) == want
    assert normalize_id(f"https://www.notion.so/Actualizar-n8n-{want.replace('-', '')}?pvs=4") \
        == want
    for bad in ("", "abc", "../../v1/users", "3a70e17c"):
        assert normalize_id(bad) is None


def test_markdown_to_blocks_and_inline():
    blocks = markdown_to_blocks("# T\n\n- a\n1. b\n- [x] hecho\n> cita\n---\n```py\nx=1\n```\n"
                                "texto **fuerte** con `c` y [web](https://ej.com)")
    assert [b["type"] for b in blocks] == ["heading_1", "bulleted_list_item",
                                          "numbered_list_item", "to_do", "quote", "divider",
                                          "code", "paragraph"]
    assert blocks[3]["to_do"]["checked"] is True
    assert blocks[6]["code"]["language"] == "py"
    parts = blocks[-1]["paragraph"]["rich_text"]
    assert parts[1] == {"type": "text", "text": {"content": "fuerte"},
                        "annotations": {"bold": True}}
    assert parts[-1]["text"]["link"] == {"url": "https://ej.com"}
    assert len(rich("x" * 4500)) == 3          # troceado a 2000 caracteres


def test_properties_follow_schema():
    out = to_notion_props({"Nombre tarea": "Nueva", "Estado": "Sin empezar", "Área": ["NAS"],
                           "Fecha objetivo": "2026-10-05", "Requiere aprobación": "sí"}, SCHEMA)
    assert out["Estado"] == {"status": {"name": "Sin empezar"}}
    assert out["Área"] == {"multi_select": [{"name": "NAS"}]}
    assert out["Fecha objetivo"] == {"date": {"start": "2026-10-05"}}
    assert out["Requiere aprobación"] == {"checkbox": True}
    for bad in ({"Inventada": "x"}, {"Estado": "Hecho"}, {"Área": ["Cocina"]},
                {"Prioridad": "Urgente"}, {"Fecha objetivo": "mañana"},
                {"Última edición": "2026-01-01"}):
        with pytest.raises(PropertyError):
            to_notion_props(bad, SCHEMA)


def _env(monkeypatch, dry="0"):
    monkeypatch.setenv("ARGOS_NOTION_TOKEN", "tok")
    monkeypatch.setenv("ARGOS_NOTION_DRY_RUN", dry)


def _fake(monkeypatch):
    from argos.mcp_servers.notion import server as n

    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        path, m = req.url.path, req.method
        if path == "/v1/search":
            return httpx.Response(200, json={"results": [ROW], "has_more": False})
        if path == f"/v1/databases/{DB}":
            return httpx.Response(200, json={"object": "database", "id": DB, "title": [
                {"plain_text": "TODO"}], "properties": SCHEMA})
        if path == f"/v1/databases/{DB}/query":
            return httpx.Response(200, json={"results": [ROW], "has_more": False})
        if path == f"/v1/pages/{PAGE}" and m == "GET":
            return httpx.Response(200, json=ROW)
        if path == f"/v1/pages/{PAGE}" and m == "PATCH":
            return httpx.Response(200, json=ROW)
        if path == f"/v1/blocks/{PAGE}/children" and m == "GET":
            return httpx.Response(200, json={"has_more": False, "results": [
                {"id": "b1", "type": "heading_2", "has_children": False,
                 "heading_2": {"rich_text": [{"plain_text": "Notas"}]}},
                {"id": "b2", "type": "to_do", "has_children": False,
                 "to_do": {"checked": False, "rich_text": [{"plain_text": "revisar cookie"}]}}]})
        if path == "/v1/pages" and m == "POST":
            return httpx.Response(200, json={"id": "new", "url": "https://notion.so/new"})
        if path.endswith("/children") and m == "PATCH":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"code": "object_not_found", "message": "no"})

    monkeypatch.setattr(n, "_client", lambda: NotionClient(
        "tok", transport=httpx.MockTransport(handler)))
    return n, calls


async def test_not_configured(monkeypatch):
    monkeypatch.delenv("ARGOS_NOTION_TOKEN", raising=False)
    from argos.mcp_servers.notion import server as n

    assert "NO CONFIGURADO" in await n.search("x")
    assert "NO CONFIGURADO" in await n.archive(PAGE)


async def test_search_and_read(monkeypatch):
    _env(monkeypatch)
    n, calls = _fake(monkeypatch)
    assert "Actualizar n8n" in await n.search("n8n")
    out = await n.read_page(PAGE)
    assert "# Actualizar n8n" in out and "## Notas" in out and "- [ ] revisar cookie" in out
    assert all(c.headers["notion-version"] == "2022-06-28" for c in calls)
    rows = json.loads(await n.query_database(
        DB, filter='{"property":"Estado","status":{"equals":"Sin empezar"}}'))
    assert rows[0]["Estado"] == "Sin empezar" and rows[0]["Área"] == ["NAS"]
    assert json.loads(calls[-1].content)["filter"]["property"] == "Estado"
    assert "ERROR" in await n.query_database(DB, filter="no-json")


async def test_create_row_validated_by_schema(monkeypatch):
    _env(monkeypatch)
    n, calls = _fake(monkeypatch)
    out = await n.create_page(DB, title="Revisar backups", parent_type="database",
                              properties='{"Estado":"Sin empezar","Área":["NAS"]}',
                              content="- [ ] comprobar Volume1")
    assert json.loads(out)["created"] is True
    body = json.loads(next(c for c in calls if c.url.path == "/v1/pages").content)
    assert body["parent"] == {"database_id": DB}
    assert body["properties"]["Nombre tarea"]["title"][0]["text"]["content"] == "Revisar backups"
    assert body["children"][0]["type"] == "to_do"
    bad = await n.create_page(DB, title="x", parent_type="database",
                              properties='{"Estado":"Inventado"}')
    assert "no es una opción" in bad


async def test_update_properties_only_on_rows(monkeypatch):
    _env(monkeypatch)
    n, calls = _fake(monkeypatch)
    out = await n.update_properties(PAGE, '{"Estado":"Completado"}')
    assert "Actualizado" in out
    patch = next(c for c in calls if c.method == "PATCH")
    assert json.loads(patch.content) == {"properties": {"Estado": {"status":
                                                                   {"name": "Completado"}}}}
    assert "RECHAZADO" in await n.update_properties("nope", '{"Estado":"Completado"}')


async def test_archive_dry_run_and_real(monkeypatch):
    _env(monkeypatch, dry="1")
    n, calls = _fake(monkeypatch)
    assert "[dry-run]" in await n.archive(PAGE)
    assert all(c.method == "GET" for c in calls)
    _env(monkeypatch, dry="0")
    assert "Archivada" in await n.archive(PAGE)
    assert json.loads(calls[-1].content) == {"archived": True}


async def test_notion_in_personal_catalog(root, store, fake_sandbox):
    from argos.config import load_config
    from argos.core.session import catalog

    cfg = load_config(root, {"data_dir": str(root / "var")})
    by = {t["name"]: t["risk"] for t in await catalog(cfg, "personal")}
    assert by.get("notion.search") == "read" and by.get("notion.read_page") == "read"
    assert by.get("notion.create_page") == "write" and by.get("notion.append") == "write"
    assert by.get("notion.archive") == "destructive"
    assert "skill:notion" in by
    infra = {t["name"] for t in await catalog(cfg, "infra")}
    assert not any(n.startswith("notion.") for n in infra)     # P2: no en perfiles potentes
