"""Broker de sandbox: el único proceso con acceso al socket Docker (ADR-0008).

Un socket Unix por segmento; el segmento se deduce del socket por el que llega la petición, así
que un núcleo no puede actuar sobre otro segmento. Cada petición se valida (broker_policy) y se
traduce a la clase DockerSandbox existente con la configuración del propio broker.

Protocolo: una línea JSON de petición y una línea JSON de respuesta por conexión.
Uso: python -m argos.sandbox.broker
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from argos.config import Config, load_config
from argos.sandbox import broker_policy as policy
from argos.sandbox.docker_sandbox import DockerSandbox, EgressPolicy

log = logging.getLogger("argos.broker")


class Broker:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.egress: dict[str, EgressPolicy] = {}
        for seg, sc in cfg.segments.items():
            pol = EgressPolicy(cfg.egress_path(seg))
            pol.set_default([*cfg.egress.allowlist, *sc.egress_extra])
            self.egress[seg] = pol
        self.boxes: dict[tuple[str, str], DockerSandbox] = {}
        self.log_path = cfg.base_path / "broker" / "broker.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, segment: str, op: str, sid: str | None, **extra: Any) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "segment": segment,
            "op": op,
            "session_id": sid,
            **extra,
        }
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def handle(self, segment: str, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        sid = policy.session_id(req.get("session_id"))
        key = (segment, sid)

        if op == "create":
            if key not in self.boxes:
                ws = policy.workspace(self.cfg.base_path / "segments" / segment, sid)
                extra = policy.domains(req.get("allow_extra"))
                box = DockerSandbox(
                    session_id=sid,
                    workspace=ws,
                    cfg=self.cfg.sandbox,
                    policy=self.egress[segment],
                    network=self.cfg.segment_network(segment),
                    proxy_url=self.cfg.segment_proxy(segment),
                    segment=segment,
                    allowlist_extra=extra,
                    env=policy.env(req.get("env")),
                )
                await box.start()
                self.boxes[key] = box
                self.record(segment, "create", sid, allow_extra=extra, env_keys=sorted(box.env))
            return {"id": self.boxes[key].id}

        box = self.boxes.get(key)
        if op == "destroy":
            if box:
                await box.destroy()
                self.boxes.pop(key, None)
                self.record(segment, "destroy", sid)
            return {"ok": True}
        if box is None:
            raise policy.PolicyError("no hay sandbox para esta sesión")
        if op == "exec":
            cmd = policy.command(req.get("command"))
            res = await box.exec(cmd, policy.timeout(req.get("timeout_s")))
            blocked = [asdict(b) for b in box.egress_blocked_since_last()]
            self.record(segment, "exec", sid, exit_code=res.exit_code, duration_ms=res.duration_ms)
            return {"result": asdict(res), "egress_blocked": blocked}
        if op == "reset":
            await box.reset()
            self.record(segment, "reset", sid)
            return {"ok": True}
        raise policy.PolicyError(f"operación desconocida: {op!r}")

    def connection_handler(self, segment: str):
        async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), 30)
                req = json.loads(line)
                resp = await self.handle(segment, req)
            except policy.PolicyError as exc:
                resp = {"error": str(exc), "kind": "validation"}
            except Exception as exc:  # noqa: BLE001 — el error vuelve al núcleo como respuesta
                log.exception("fallo en el broker")
                resp = {"error": f"{type(exc).__name__}: {exc}", "kind": "sandbox"}
            writer.write((json.dumps(resp) + "\n").encode())
            try:
                await writer.drain()
            finally:
                writer.close()

        return on_client

    async def serve(self) -> None:
        servers = []
        for seg in self.cfg.segments:
            path = self.cfg.broker_socket(seg)
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            path.unlink(missing_ok=True)
            servers.append(
                await asyncio.start_unix_server(self.connection_handler(seg), path=str(path))
            )
            os.chmod(path, 0o600)
            log.info("segmento %s: %s", seg, path)
        try:
            await asyncio.gather(*(s.serve_forever() for s in servers))
        finally:
            for box in list(self.boxes.values()):
                await box.destroy()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(Broker(load_config()).serve())


if __name__ == "__main__":
    main()
