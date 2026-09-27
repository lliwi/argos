"""Sandbox de ejecución: contenedor Docker persistente por sesión (ADR-0002, §8).

- Red interna sin salida; el único egress es el proxy con allowlist (RF-EX-04).
- Límites de CPU/memoria/pids y timeout por comando (RF-EX-05).
- `reset()` vuelve a la imagen limpia (RF-EX-09).
Se usa la CLI `docker` vía subprocess: cada operación es un comando explícito y auditable.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from argos.config import SandboxCfg

MAX_CAPTURE = 1_000_000


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


@dataclass
class EgressDecision:
    host: str
    port: int
    decision: str
    reason: str
    ts: str = ""


class SandboxError(RuntimeError):
    pass


class Sandbox(Protocol):
    id: str

    async def exec(self, command: str, timeout_s: int | None = None) -> ExecResult: ...
    def egress_blocked_since_last(self) -> list[EgressDecision]: ...
    async def reset(self) -> None: ...
    async def destroy(self) -> None: ...


async def _run(*args: str, limit_s: float = 60, stdin: bytes | None = None) -> ExecResult:
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=limit_s)
    except TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return ExecResult(None, out.decode(errors="replace")[:MAX_CAPTURE],
                          err.decode(errors="replace")[:MAX_CAPTURE],
                          int((time.monotonic() - start) * 1000), timed_out=True)
    return ExecResult(proc.returncode, out.decode(errors="replace")[:MAX_CAPTURE],
                      err.decode(errors="replace")[:MAX_CAPTURE],
                      int((time.monotonic() - start) * 1000))


class EgressPolicy:
    """Fichero de política compartido con el proxy: allowlist base + extras por IP de cliente."""

    def __init__(self, egress_dir: Path) -> None:
        self.dir = egress_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = egress_dir / "policy.json"
        self.events = egress_dir / "events.jsonl"
        self.events.touch(exist_ok=True)

    def _update(self, fn) -> None:
        with open(self.dir / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
            data.setdefault("default", [])
            data.setdefault("clients", {})
            fn(data)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.path)

    def set_default(self, allowlist: list[str]) -> None:
        self._update(lambda d: d.__setitem__("default", sorted(set(allowlist))))

    def set_client(self, ip: str, extra: list[str]) -> None:
        self._update(lambda d: d["clients"].__setitem__(ip, sorted(set(extra))))

    def remove_client(self, ip: str) -> None:
        self._update(lambda d: d["clients"].pop(ip, None))


@dataclass
class DockerSandbox:
    session_id: str
    workspace: Path
    cfg: SandboxCfg
    policy: EgressPolicy
    network: str
    proxy_url: str
    segment: str = "main"
    allowlist_extra: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = f"argos-sbx-{self.session_id[:12]}"
        self.volume = f"{self.id}-home"
        self.ip: str | None = None
        self._started = False
        self._egress_offset = 0

    async def start(self) -> None:
        if self._started:
            return
        (self.workspace / "in").mkdir(parents=True, exist_ok=True)
        (self.workspace / "out").mkdir(parents=True, exist_ok=True)
        proxy = self.proxy_url
        env = {
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": "localhost,127.0.0.1", **self.env,
        }
        args = [
            "docker", "run", "-d", "--name", self.id, "--hostname", "sandbox",
            "--label", "argos.sandbox=1", "--label", f"argos.session={self.session_id}",
            "--label", f"argos.segment={self.segment}",
            "--network", self.network,
            "--cpus", self.cfg.cpus, "--memory", self.cfg.memory,
            "--pids-limit", str(self.cfg.pids),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,size=512m",
            "-v", f"{self.volume}:/home/agent",
            "-v", f"{(self.workspace / 'in').resolve()}:/workspace/in:ro",
            "-v", f"{(self.workspace / 'out').resolve()}:/workspace/out:rw",
            "-w", "/workspace/out",
        ]
        for key in env:
            # Solo el nombre: el valor viaja por el entorno del proceso docker, nunca por argv
            # (argv es visible en `ps` y quedaría en logs).
            args += ["-e", key]
        args += [self.cfg.image, "sleep", "infinity"]
        proc_env = {**os.environ, **env}
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=proc_env)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise SandboxError(f"docker run falló: {err.decode(errors='replace').strip()[:500]}")
        ip_tpl = f'{{{{(index .NetworkSettings.Networks "{self.network}").IPAddress}}}}'
        res = await _run("docker", "inspect", "-f", ip_tpl, self.id)
        self.ip = res.stdout.strip() or None
        if self.ip:
            self.policy.set_client(self.ip, self.allowlist_extra)
        self._egress_offset = self.policy.events.stat().st_size
        self._started = True

    async def exec(self, command: str, timeout_s: int | None = None) -> ExecResult:
        await self.start()
        timeout = timeout_s or self.cfg.command_timeout_s
        # `timeout` dentro del contenedor mata el proceso aunque el cliente docker muera.
        res = await _run("docker", "exec", "-i", "-w", "/workspace/out", self.id,
                         "timeout", "-k", "5", str(timeout), "bash", "-c", command,
                         limit_s=timeout + 15)
        if res.exit_code == 124:
            res.timed_out = True
        return res

    def egress_blocked_since_last(self) -> list[EgressDecision]:
        """Bloqueos del proxy atribuibles a este sandbox desde la última consulta (CA-6)."""
        if not self.ip:
            return []
        out: list[EgressDecision] = []
        with open(self.policy.events, encoding="utf-8") as fh:
            fh.seek(self._egress_offset)
            for line in fh:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("client_ip") == self.ip and ev.get("decision") == "blocked":
                    out.append(EgressDecision(ev["host"], int(ev.get("port", 0)), "blocked",
                                              ev.get("reason", ""), ev.get("ts", "")))
            self._egress_offset = fh.tell()
        return out

    async def reset(self) -> None:
        await self.destroy()
        await self.start()

    async def destroy(self) -> None:
        if self.ip:
            self.policy.remove_client(self.ip)
        await _run("docker", "rm", "-f", "-v", self.id)
        await _run("docker", "volume", "rm", "-f", self.volume)
        self._started = False
        self.ip = None
