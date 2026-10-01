"""Acceso al NAS: ficheros por SMB (smbprotocol) y almacenamiento por SNMP (puresnmp).

Operaciones acotadas: listar/leer/mover/borrar/crear-carpeta por SMB, y métricas de volúmenes por
SNMP. No hay ejecución de comandos (no se usa SSH): la superficie es de fichero y de lectura de
contadores, no de shell.
"""

from __future__ import annotations

import re

import smbclient

MAX_READ = 200_000
_SEGMENT = re.compile(r"^[^\\/:*?\"<>|\x00]+$")  # un segmento de ruta válido (sin traversal)


class NasError(RuntimeError):
    pass


def _unc(host: str, path: str) -> tuple[str, str]:
    """Convierte `share/sub/dir` en `//host/share/sub/dir`, validando cada segmento.

    Rechaza rutas vacías, absolutas o con `..` (traversal). Devuelve (unc, share)."""
    parts = [p for p in re.split(r"[\\/]+", (path or "").strip()) if p not in ("", ".")]
    if not parts:
        raise NasError("indica al menos el recurso compartido (p. ej. Public/carpeta)")
    for p in parts:
        if p == ".." or not _SEGMENT.match(p):
            raise NasError(f"segmento de ruta no permitido: {p!r}")
    return "//" + host + "/" + "/".join(parts), parts[0]


class NasSmb:
    """Cliente SMB síncrono (sus métodos se ejecutan en un hilo desde el servidor MCP)."""

    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host
        smbclient.ClientConfig(username=username, password=password)

    def list_dir(self, path: str) -> list[dict]:
        unc, _ = _unc(self.host, path)
        try:
            out = []
            for entry in smbclient.scandir(unc):
                info = entry.stat()
                out.append(
                    {
                        "name": entry.name,
                        "dir": entry.is_dir(),
                        "size": 0 if entry.is_dir() else info.st_size,
                        "mtime": int(info.st_mtime),
                    }
                )
            return sorted(out, key=lambda e: (not e["dir"], e["name"].lower()))[:1000]
        except Exception as exc:  # noqa: BLE001
            raise NasError(f"SMB list {path}: {exc}") from exc

    def read_file(self, path: str) -> str:
        unc, _ = _unc(self.host, path)
        try:
            with smbclient.open_file(unc, mode="rb") as fh:
                data = fh.read(MAX_READ + 1)
        except Exception as exc:  # noqa: BLE001
            raise NasError(f"SMB read {path}: {exc}") from exc
        truncated = len(data) > MAX_READ
        text = data[:MAX_READ].decode("utf-8", errors="replace")
        if "\x00" in text[:1000]:
            raise NasError(f"{path} parece binario; solo se leen ficheros de texto")
        return text + ("\n[... truncado ...]" if truncated else "")

    def move(self, src: str, dst: str) -> None:
        src_unc, s1 = _unc(self.host, src)
        dst_unc, s2 = _unc(self.host, dst)
        if s1 != s2:
            raise NasError("origen y destino deben estar en el mismo recurso compartido")
        try:
            smbclient.rename(src_unc, dst_unc)
        except Exception as exc:  # noqa: BLE001
            raise NasError(f"SMB move {src} -> {dst}: {exc}") from exc

    def delete(self, path: str) -> None:
        unc, _ = _unc(self.host, path)
        try:
            info = smbclient.stat(unc)
            if info.st_mode & 0o40000:  # directorio: solo si está vacío
                smbclient.rmdir(unc)
            else:
                smbclient.remove(unc)
        except Exception as exc:  # noqa: BLE001
            raise NasError(f"SMB delete {path}: {exc}") from exc

    def mkdir(self, path: str) -> None:
        unc, _ = _unc(self.host, path)
        try:
            smbclient.makedirs(unc, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise NasError(f"SMB mkdir {path}: {exc}") from exc


async def snmp_storage(host: str, community: str) -> list[dict]:
    """Tabla hrStorageTable por SNMP: volúmenes con total/usado en bytes."""
    from puresnmp import V2C, Client, PyWrapper

    client = PyWrapper(Client(host, V2C(community)))

    async def walk(col: str) -> dict[str, object]:
        out: dict[str, object] = {}
        async for oid, val in client.walk(f"1.3.6.1.2.1.25.2.3.1.{col}"):
            out[str(oid).split(".")[-1]] = val
        return out

    try:
        descr, unit, size, used = [await walk(c) for c in ("3", "4", "5", "6")]
    except Exception as exc:  # noqa: BLE001
        raise NasError(f"SNMP {host}: {exc}") from exc
    rows = []
    for idx, raw in descr.items():
        name = (
            bytes(raw).decode(errors="replace") if isinstance(raw, bytes | bytearray) else str(raw)
        )
        u = int(unit.get(idx, 0) or 0)
        total = int(size.get(idx, 0) or 0) * u
        use = int(used.get(idx, 0) or 0) * u
        if total:
            rows.append(
                {"name": name, "total": total, "used": use, "percent": round(100 * use / total)}
            )
    return rows
