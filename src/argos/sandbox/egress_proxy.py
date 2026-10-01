"""Proxy HTTP/HTTPS (CONNECT) con allowlist para el egress del sandbox (RF-EX-04, CA-6).

Autónomo (solo stdlib) para correr en su propio contenedor. Política en `policy.json`
(recargada si cambia): `{"default": [dominios], "clients": {"<ip>": [dominios extra]}}`.
Cada decisión se escribe en `events.jsonl`; los bloqueos devuelven 403 con motivo legible.

Uso: python -m argos.sandbox.egress_proxy --dir /var/egress --port 3128
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("argos.egress")


def host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    host_ip = _as_ip(host)
    for entry in allowlist:
        entry = entry.strip().lower()
        if "/" in entry or _as_ip(entry) is not None:
            # Entrada IP o rango CIDR: solo aplica si el destino es una IP dentro del rango.
            if host_ip is not None:
                try:
                    if host_ip in ipaddress.ip_network(entry, strict=False):
                        return True
                except ValueError:
                    pass
            continue
        dom = entry.lstrip("*.").rstrip(".")
        if host == dom or host.endswith("." + dom):
            return True
    return False


def _as_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class Policy:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime = -1.0
        self._data: dict = {"default": [], "clients": {}}

    def allowlist_for(self, client_ip: str) -> list[str]:
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._data = json.loads(self.path.read_text())
                self._mtime = mtime
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # sin política válida => solo lo último conocido (por defecto: nada)
        return list(self._data.get("default", [])) + list(
            self._data.get("clients", {}).get(client_ip, [])
        )


class EgressProxy:
    def __init__(self, directory: Path) -> None:
        self.policy = Policy(directory / "policy.json")
        self.events_path = directory / "events.jsonl"

    def record(
        self, client_ip: str, method: str, host: str, port: int, decision: str, reason: str
    ) -> None:
        ev = {
            "ts": datetime.now(UTC).isoformat(),
            "client_ip": client_ip,
            "method": method,
            "host": host,
            "port": port,
            "decision": decision,
            "reason": reason,
        }
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client_ip = writer.get_extra_info("peername")[0]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.close()
            return
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = request_line.split(" ", 2)
        except ValueError:
            await self._reply(writer, 400, "peticion malformada")
            return

        if method.upper() == "CONNECT":
            host, _, port_s = target.rpartition(":")
            port = int(port_s or 443)
        else:
            # Petición HTTP en claro con URI absoluta: http://host[:port]/path
            if not target.startswith("http://"):
                await self._reply(writer, 400, "se requiere URI absoluta")
                return
            hostport = target[7:].split("/", 1)[0]
            host, _, port_s = hostport.partition(":")
            port = int(port_s or 80)
        host = host.strip("[]")

        allow = self.policy.allowlist_for(client_ip)
        if not host_allowed(host, allow):
            reason = f"argos-egress: el dominio '{host}' no esta en la allowlist del sandbox"
            self.record(client_ip, method, host, port, "blocked", reason)
            await self._reply(writer, 403, reason)
            return
        self.record(client_ip, method, host, port, "allowed", "allowlist")

        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=20
            )
        except (OSError, TimeoutError) as exc:
            await self._reply(writer, 502, f"argos-egress: no se pudo conectar a {host}: {exc}")
            return

        if method.upper() == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            path = "/" + target[7:].split("/", 1)[1] if "/" in target[7:] else "/"
            headers = [
                h
                for h in header_lines
                if h and not h.lower().startswith(("proxy-connection:", "proxy-authorization:"))
            ]
            up_writer.write(
                (f"{method} {path} {version}\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode(
                    "latin-1"
                )
            )
            await up_writer.drain()

        await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                dst.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int, message: str) -> None:
        body = (message + "\n").encode()
        reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(code, "Error")
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\nContent-Length: "
            f"{len(body)}\r\nX-Argos-Egress: {code}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()


async def serve(directory: Path, host: str, port: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    proxy = EgressProxy(directory)
    server = await asyncio.start_server(proxy.handle, host, port)
    log.info("argos-egress escuchando en %s:%s (politica %s)", host, port, proxy.policy.path)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=os.environ.get("ARGOS_EGRESS_DIR", "/var/egress"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3128)
    ns = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(Path(ns.dir), ns.host, ns.port))


if __name__ == "__main__":
    main()
