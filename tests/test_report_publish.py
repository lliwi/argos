"""Publicar el informe de un perfil aislado en Notion (report.publish + /publish, ADR-0027)."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
from test_server import running_core

from argos.audit.events import PublishRequested
from argos.tools.base import ToolContext, ToolError
from argos.tools.report import PublishReportTool


async def test_publish_tool_reads_workspace_and_emits(root, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "informe.md").write_text("# Informe\n- hallazgo", encoding="utf-8")
    emitted: list = []
    ctx = ToolContext(
        session_id="s1", turn_id="t", trace_id="s1", profile=None, workspace=tmp_path,
        store=None, emit=emitted.append,
    )  # fmt: skip
    tool = PublishReportTool()
    res = await tool.run({"title": "Auditoría X", "parent": "https://notion.so/p/" + "a" * 32}, ctx)
    assert res.ok and res.data["publish_title"] == "Auditoría X"
    assert len(emitted) == 1 and isinstance(emitted[0], PublishRequested)
    assert emitted[0].title == "Auditoría X" and "hallazgo" in emitted[0].markdown
    # Sin informe escrito o con ruta con traversal: rechaza y no emite.
    for bad in ({"title": "x", "parent": "p", "path": "out/no-existe.md"},
                {"title": "x", "parent": "p", "path": "../secrets"}):  # fmt: skip
        try:
            await tool.run(bad, ctx)
            raise AssertionError("debería rechazarse")
        except ToolError:
            pass
    assert len(emitted) == 1


async def test_publish_only_in_isolated_profiles(root, store, fake_sandbox):
    from argos.config import load_config
    from argos.core.session import catalog

    cfg = load_config(root, {"data_dir": str(root / "var")})
    for seg, prof in (("pentest", "pentest"), ("osint", "osint")):
        names = {t["name"] for t in await catalog(load_config(root, {"data_dir": str(root / "var"),
                 "segment": seg}), prof)}  # fmt: skip
        assert "report.publish" in names, prof
    # Perfiles de main no la tienen (no la necesitan; publican con la tool de Notion directa).
    for prof in ("orchestrator", "infra", "personal"):
        names = {t["name"] for t in await catalog(cfg, prof)}
        assert "report.publish" not in names, prof


async def test_publish_endpoint_creates_notion_page_verbatim(cfg, store, fake_sandbox, monkeypatch):
    # Inventario con token de Notion y un cliente Notion falso que captura la llamada.
    (cfg.root / "secrets").mkdir(exist_ok=True)
    (cfg.root / "secrets" / "inventory.yaml").write_text(
        "services:\n  notion:\n    api_key: tok-notion-123\n", encoding="utf-8"
    )
    captured: dict = {}

    def fake_handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"id": "p" * 32, "url": "https://notion.so/created"})

    from argos.mcp_servers.notion import rest as notion_rest

    orig_init = notion_rest.NotionClient.__init__

    def patched_init(self, token, *a, **k):
        orig_init(self, token, transport=httpx.MockTransport(fake_handler))

    monkeypatch.setattr(notion_rest.NotionClient, "__init__", patched_init)

    async with running_core(cfg, store, [], fake_sandbox) as (core, client):
        parent = "https://www.notion.so/Auditorias-" + "b" * 32
        res = await client.publish("Auditoría ISMS", "# Resumen\n- un hallazgo", parent)
    assert res["url"] == "https://notion.so/created"
    assert captured["path"].endswith("/pages") and captured["auth"] == "Bearer tok-notion-123"
    # El id del padre se extrae de la URL; el cuerpo va literal como bloques.
    assert captured["body"]["parent"]["page_id"].replace("-", "") == "b" * 32
    dumped = json.dumps(captured["body"], ensure_ascii=False)
    assert "un hallazgo" in dumped and "Resumen" in dumped


async def test_publish_endpoint_rejects_bad_input(cfg, store, fake_sandbox):
    async with running_core(cfg, store, [], fake_sandbox) as (core, client):
        # parent que no es una URL/id de Notion válido.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.publish("t", "x", "no-es-notion"), 5)
            raise AssertionError("debería fallar")
