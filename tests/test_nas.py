"""MCP del NAS: SMB (ficheros) + SNMP (almacenamiento) (UC-3)."""

from __future__ import annotations

import pytest

from argos.mcp_servers.nas.rest import NasError, _unc


def test_unc_builds_and_rejects_traversal():
    assert _unc("nas.home", "Public/pelis") == ("//nas.home/Public/pelis", "Public")
    assert _unc("nas.home", "/Public/x/") == ("//nas.home/Public/x", "Public")
    for bad in ("", "..", "Public/../etc", "Public/\x00", "a/b\\..\\c"):
        with pytest.raises(NasError):
            _unc("nas.home", bad)


def _env(monkeypatch, dry="0"):
    for k, v in {
        "ARGOS_NAS_HOST": "nas.home",
        "ARGOS_NAS_USER": "argos",
        "ARGOS_NAS_PASSWORD": "pw",
        "ARGOS_NAS_COMMUNITY": "public",
        "ARGOS_NAS_DRY_RUN": dry,
    }.items():
        monkeypatch.setenv(k, v)


async def test_nas_not_configured(monkeypatch):
    for k in ("ARGOS_NAS_HOST", "ARGOS_NAS_USER", "ARGOS_NAS_PASSWORD", "ARGOS_NAS_COMMUNITY"):
        monkeypatch.delenv(k, raising=False)
    from argos.mcp_servers.nas import server as n

    assert "NO CONFIGURADO" in await n.list("Public")
    assert "NO CONFIGURADO" in await n.storage()
    assert "NO CONFIGURADO" in await n.move("a", "b")


async def test_nas_actions_dry_run(monkeypatch):
    from argos.mcp_servers.nas import server as n

    _env(monkeypatch, dry="1")
    assert "[dry-run]" in await n.move("Public/a", "Public/b")
    assert "[dry-run]" in await n.delete("Public/a")
    assert "[dry-run]" in await n.mkdir("Public/nueva")


async def test_nas_list_uses_smb(monkeypatch):
    from argos.mcp_servers.nas import server as n

    _env(monkeypatch, dry="0")

    class FakeSmb:
        def list_dir(self, path):
            assert path == "Public"
            return [
                {"name": "pelis", "dir": True, "size": 0, "mtime": 0},
                {"name": "leeme.txt", "dir": False, "size": 2048, "mtime": 0},
            ]

    monkeypatch.setattr(n, "_smb", lambda: FakeSmb())
    out = await n.list("Public")
    assert "pelis" in out and "leeme.txt" in out and "2.0KB" in out


async def test_nas_storage_filters_filesystems(monkeypatch):
    from argos.mcp_servers.nas import server as n

    _env(monkeypatch, dry="0")

    async def fake_storage(host, community):
        return [
            {
                "name": "Physical memory",
                "total": 2_000_000_000,
                "used": 1_900_000_000,
                "percent": 95,
            },
            {
                "name": "/Volume1",
                "total": 2_000_000_000_000,
                "used": 1_500_000_000_000,
                "percent": 75,
            },
        ]

    monkeypatch.setattr(n, "snmp_storage", fake_storage)
    out = await n.storage()
    assert "/Volume1" in out and "Physical memory" not in out  # memoria/swap fuera


async def test_nas_in_infra_catalog(root, store, fake_sandbox):
    from argos.config import load_config
    from argos.core.session import catalog

    items = await catalog(load_config(root, {"data_dir": str(root / "var")}), "infra")
    by = {t["name"]: t["risk"] for t in items}
    assert by.get("nas.list") == "read" and by.get("nas.storage") == "read"
    assert by.get("nas.move") == "destructive" and by.get("nas.delete") == "destructive"
    assert by.get("nas.mkdir") == "write"
