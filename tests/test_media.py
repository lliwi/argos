"""MCP de descargas: Jackett (buscar) + Transmission (descargar/gestionar) (UC-3)."""

from __future__ import annotations

import httpx

from argos.mcp_servers.media.rest import JackettClient, TransmissionClient


async def test_jackett_search_sorts_by_seeders():
    def handler(request):
        assert "/api/v2.0/indexers/all/results" in request.url.path
        assert request.url.params.get("Query") == "linux"
        return httpx.Response(200, json={"Results": [
            {"Title": "A", "Seeders": 3, "Size": 100, "MagnetUri": "magnet:?a"},
            {"Title": "B", "Seeders": 50, "Size": 200, "MagnetUri": "magnet:?b"}]})

    c = JackettClient("http://jk:9117", "k", transport=httpx.MockTransport(handler))
    res = await c.search("linux")
    await c.aclose()
    assert [r["Title"] for r in res] == ["A", "B"]     # el cliente no ordena; lo hace el server


async def test_transmission_409_handshake_and_add():
    calls = {"n": 0}

    def handler(request):
        body = request.read().decode()
        if calls["n"] == 0:
            calls["n"] += 1
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "SID-123"})
        assert request.headers.get("X-Transmission-Session-Id") == "SID-123"
        if "torrent-add" in body:
            return httpx.Response(200, json={"result": "success",
                                 "arguments": {"torrent-added": {"id": 7, "name": "x"}}})
        return httpx.Response(200, json={"result": "success", "arguments": {"torrents": []}})

    c = TransmissionClient("http://tr:9091", transport=httpx.MockTransport(handler))
    added = await c.add("magnet:?xt=urn:btih:deadbeef")
    await c.aclose()
    assert added == {"id": 7, "name": "x"} and calls["n"] == 1


def _env(monkeypatch, dry="0"):
    monkeypatch.setenv("ARGOS_JACKETT_URL", "http://jk:9117")
    monkeypatch.setenv("ARGOS_JACKETT_KEY", "k")
    monkeypatch.setenv("ARGOS_TRANSMISSION_URL", "http://tr:9091")
    monkeypatch.setenv("ARGOS_MEDIA_DRY_RUN", dry)


async def test_media_add_validates_input(monkeypatch):
    from argos.mcp_servers.media import server as m

    _env(monkeypatch, dry="0")
    m._LAST.clear()
    assert "RECHAZADO" in await m.add()                       # ni index ni link
    assert "RECHAZADO" in await m.add(link="file:///etc/passwd")   # no magnet
    assert "RECHAZADO" in await m.add(link="http://jk/dl?apikey=x")  # http suelto: usar index
    assert "RECHAZADO" in await m.add(index=1)                # sin búsqueda previa


async def test_media_add_by_index_resolves_link_serverside(monkeypatch):
    from argos.mcp_servers.media import server as m

    _env(monkeypatch, dry="1")
    # simula una búsqueda previa: el enlace real (con api key) queda en el servidor
    m._LAST[:] = [{"title": "Peli", "link": "http://jk:9117/dl/x?jackett_apikey=REAL&file=Peli"}]
    out = await m.add(index=1)
    assert "[dry-run]" in out and "Peli" in out
    assert "RECHAZADO" in await m.add(index=9)                # fuera de rango


async def test_media_dry_run_magnet_and_control(monkeypatch):
    from argos.mcp_servers.media import server as m

    _env(monkeypatch, dry="1")
    assert "[dry-run]" in await m.add(link="magnet:?xt=urn:btih:deadbeef")
    assert "[dry-run]" in await m.control(5, "remove")
    assert "RECHAZADO" in await m.control(5, "wipe")


async def test_media_not_configured(monkeypatch):
    for v in ("ARGOS_JACKETT_URL", "ARGOS_JACKETT_KEY", "ARGOS_TRANSMISSION_URL"):
        monkeypatch.delenv(v, raising=False)
    from argos.mcp_servers.media import server as m

    assert "NO CONFIGURADO" in await m.search("x")
    assert "NO CONFIGURADO" in await m.downloads()


async def test_media_in_infra_catalog(root, store, fake_sandbox):
    from argos.config import load_config
    from argos.core.session import catalog

    items = await catalog(load_config(root, {"data_dir": str(root / "var")}), "infra")
    by = {t["name"]: t["risk"] for t in items}
    assert by.get("media.search") == "read" and by.get("media.downloads") == "read"
    assert by.get("media.add") == "destructive" and by.get("media.control") == "destructive"
