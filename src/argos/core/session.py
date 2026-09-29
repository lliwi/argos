"""Ciclo de vida de una sesión: montaje, captura de versiones (RF-OB-10), bucle y cierre.

Es la API interna que consumen los canales (RF-04): `run_session()` no sabe si le habla una TUI,
Matrix o el runner de evaluación; el canal se pasa como dato y queda en auditoría (RF-08).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from argos.audit.events import MemoryEvent, SessionEnded, SessionStarted
from argos.audit.redact import Redactor
from argos.audit.store import AuditStore
from argos.config import Config, harness_commit, sha256_text
from argos.core.context import ContextManager, LoadToolsTool, ReadRefTool
from argos.core.conversation import compose_task, conversation_block
from argos.core.loop import AgentLoop, LoopResult
from argos.core.subagent import DelegateTool
from argos.governance.approval import Approver, NoApprover
from argos.governance.budget import BudgetTracker, tokens_spent_today
from argos.governance.killswitch import KillSwitch
from argos.inventory import load_inventory
from argos.model.base import ModelProvider
from argos.model.factory import engine_instructions
from argos.sandbox.broker_client import BrokerSandbox
from argos.sandbox.docker_sandbox import DockerSandbox, EgressPolicy, Sandbox
from argos.secrets import load_profile_secrets
from argos.skills import LoadSkillTool, SkillRegistry, skills_index
from argos.state import StateStore, render_memories
from argos.tools.inventory import InventoryTool
from argos.tools.mcp_client import (
    McpConnections,
    cloudflare_server,
    homeassistant_server,
    kali_server,
    media_server,
    nas_server,
    portainer_server,
    reminders_server,
)
from argos.tools.memory import MemorySave, MemorySearch, MemoryUpdate
from argos.tools.registry import ToolRegistry
from argos.tools.scratchpad import SCRATCHPAD_TOOLS
from argos.tools.shell import ShellExecTool
from argos.tools.workspace import WORKSPACE_TOOLS

_SLOTS: asyncio.Semaphore | None = None  # RF-GOV-03 (en proceso; entre procesos: Fase 2)


class SessionRefused(RuntimeError):
    pass


@dataclass
class SessionOptions:
    task: str
    profile: str = "personal"
    channel: str = "cli"
    dry_run: bool | None = None          # None => lo que diga el perfil
    parent_session_id: str | None = None
    allow_domains: list[str] = field(default_factory=list)
    session_budget_tokens: int | None = None
    input_files: dict[str, str] = field(default_factory=dict)  # nombre -> contenido (in/)
    state_dir: Path | None = None        # estado durable de tools (p. ej. evals aisladas)
    workspace: Path | None = None        # subagentes: comparten el workspace del padre
    depth: int = 0                       # 0 = sesión raíz
    session_id: str | None = None        # lo fija la API para poder devolverlo al instante
    thread_id: str | None = None         # conversación a la que pertenece (argos.state)
    trace_id: str | None = None


@dataclass
class SessionResult:
    session_id: str
    status: str
    message: str
    steps: int
    tokens: int
    workspace: Path


class LazySandbox:
    """Crea el contenedor solo si la sesión llega a usar el shell."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._inner: DockerSandbox | None = None

    @property
    def id(self) -> str:
        return self._inner.id if self._inner else "(sin crear)"

    def _get(self) -> DockerSandbox:
        if self._inner is None:
            self._inner = self._factory()
        return self._inner

    async def exec(self, command, timeout_s=None):
        return await self._get().exec(command, timeout_s)

    def egress_blocked_since_last(self):
        return self._inner.egress_blocked_since_last() if self._inner else []

    async def reset(self) -> None:
        await self._get().reset()

    async def destroy(self) -> None:
        if self._inner is not None:
            await self._inner.destroy()


def build_tools(cfg: Config) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ShellExecTool())
    for cls in (*WORKSPACE_TOOLS, *SCRATCHPAD_TOOLS):
        reg.register(cls())
    reg.register(ReadRefTool())
    return reg


async def catalog(cfg: Config, profile_name: str) -> list[dict]:
    """Catálogo de tools disponibles para un perfil (RF-11): nativas + MCP conectados, filtrado
    por el perfil. Conecta los MCP solo para enumerarlos y los cierra; no ejecuta nada."""
    profile = cfg.profile(profile_name)
    reg = build_tools(cfg)
    # Tools que una sesión registra dinámicamente (memoria y, si el perfil delega, subagentes).
    state = StateStore(cfg.data_path / "state.db")
    reg.register(MemorySave(state, ttl_days=profile.retention_days))
    reg.register(MemorySearch(state))
    reg.register(MemoryUpdate(state))
    if cfg.subagents.max_depth > 0:
        async def _noop(_task: str, _budget: int, _target: str | None = None):
            return ("", "completed", "", 0, 0)
        targets = {n: cfg.profile(n).description for n in profile.delegate_profiles
                   if n in cfg.profiles and cfg.allows_profile(n)}
        reg.register(DelegateTool(_noop, cfg.subagents.budget_tokens, profile.name, targets))
    mcp = McpConnections()
    try:
        if any(profile.allows_tool(f"reminders.{n}") for n in ("add", "list")):
            for tool in await mcp.connect(reminders_server(str(cfg.data_path / "reminders.db"))):
                reg.register(tool)
        if profile.allows_tool("kali.nmap"):
            for tool in await mcp.connect(kali_server(
                    cfg.kali.url, None, profile.scope, profile.authorization_ref, True)):
                reg.register(tool)
        inv = load_inventory(cfg.root)
        if profile.allows_tool("infra.inventory"):
            reg.register(InventoryTool(inv))
        if profile.allows_tool("portainer.list_containers"):
            pt = inv.get("portainer")
            for tool in await mcp.connect(portainer_server(
                    pt.get("url", ""), inv.secret("portainer", "api_key") or "",
                    int(pt.get("endpoint", 1)), True)):
                reg.register(tool)
        if profile.allows_tool("homeassistant.list_entities"):
            ha = inv.get("homeassistant")
            ha_token = (inv.secret("homeassistant", "token")
                        or inv.secret("homeassistant", "api_key") or "")
            for tool in await mcp.connect(homeassistant_server(
                    ha.get("url", ""), ha_token, True)):
                reg.register(tool)
        if profile.allows_tool("media.search"):
            jk, tr = inv.get("jackett"), inv.get("transmission")
            for tool in await mcp.connect(media_server(
                    jk.get("url", ""), inv.secret("jackett", "api_key") or "",
                    tr.get("url", ""), tr.get("username", ""),
                    inv.secret("transmission", "password") or "", True)):
                reg.register(tool)
        if profile.allows_tool("nas.list"):
            nas = inv.get("nas")
            for tool in await mcp.connect(nas_server(
                    nas.get("url", ""), nas.get("user", "") or nas.get("username", ""),
                    inv.secret("nas", "password") or "", inv.secret("nas", "community") or "",
                    True)):
                reg.register(tool)
        if profile.allows_tool("cloudflare.zones"):
            cf_token = inv.secret("cloudflare", "api_key") or inv.secret("cloudflare", "token")
            for tool in await mcp.connect(cloudflare_server(cf_token or "", True)):
                reg.register(tool)
        reg = reg.for_profile(profile)
        skills = SkillRegistry(cfg.root / "skills").for_profile(profile.skills)
        out = [{"name": t.name, "version": t.version, "risk": t.risk_class.value,
                "idempotent": t.idempotent, "mcp_server": t.mcp_server,
                "description": t.description} for t in reg]
        out += [{"name": f"skill:{sk.name}", "version": sk.full_version, "risk": "read",
                 "idempotent": True, "mcp_server": "skill", "description": sk.description}
                for sk in skills.values()]
        return sorted(out, key=lambda d: d["name"])
    finally:
        await mcp.aclose()


def load_system_prompt(cfg: Config) -> tuple[str, str]:
    """Devuelve el prompt de sistema y una versión que cubre también el prompt del motor."""
    text = (cfg.root / "prompts" / "system.md").read_text(encoding="utf-8")
    engine = engine_instructions(cfg) if cfg.model.provider == "codex" else None
    return text, sha256_text(text + "\x00" + (engine or ""))[:16]


async def run_session(
    opts: SessionOptions,
    cfg: Config,
    provider: ModelProvider,
    *,
    store: AuditStore | None = None,
    approver: Approver | None = None,
    sandbox_factory=None,
    on_progress=None,
) -> SessionResult:
    global _SLOTS
    _SLOTS = _SLOTS or asyncio.Semaphore(cfg.concurrency.max_sessions)
    store = store or AuditStore(cfg.data_path, Redactor(cfg.audit.redact_pii))
    if reason := KillSwitch(store).active():
        raise SessionRefused(f"kill switch activo ({reason}); usa `argos rearm`")
    if not cfg.allows_profile(opts.profile):
        # RF-SEC-02: un perfil solo se ejecuta en el núcleo de su segmento.
        raise SessionRefused(
            f"el perfil {opts.profile!r} no pertenece al segmento {cfg.segment!r}; ejecútalo con "
            f"ARGOS_SEGMENT={cfg.segment_of(opts.profile) or '?'}")
    if opts.depth > 0:
        # Los subagentes no ocupan hueco: los acota max_depth y el presupuesto del padre.
        return await _run(opts, cfg, provider, store, approver, sandbox_factory, on_progress)
    if _SLOTS.locked():
        raise SessionRefused(f"límite de sesiones concurrentes ({cfg.concurrency.max_sessions})")
    async with _SLOTS:
        return await _run(opts, cfg, provider, store, approver, sandbox_factory, on_progress)


async def _run(opts: SessionOptions, cfg: Config, provider: ModelProvider, store: AuditStore,
               approver: Approver | None, sandbox_factory, on_progress) -> SessionResult:
    profile = cfg.profile(opts.profile)
    killswitch = KillSwitch(store)
    sid = opts.session_id or uuid.uuid4().hex
    dry_run = profile.dry_run if opts.dry_run is None else opts.dry_run
    workspace = opts.workspace or cfg.data_path / "workspaces" / sid
    (workspace / "in").mkdir(parents=True, exist_ok=True)
    (workspace / "out").mkdir(parents=True, exist_ok=True)
    for name, content in opts.input_files.items():
        (workspace / "in" / name).write_text(content, encoding="utf-8")

    secrets = load_profile_secrets(cfg.root, profile, store.redactor)
    tools = build_tools(cfg)
    mcp = McpConnections()
    result: LoopResult | None = None
    try:
        state_dir = opts.state_dir or cfg.data_path
        state_dir.mkdir(parents=True, exist_ok=True)
        state = StateStore(state_dir / "state.db")
        tools.register(MemorySave(state, ttl_days=profile.retention_days))
        tools.register(MemorySearch(state))
        tools.register(MemoryUpdate(state))
        if any(profile.allows_tool(f"reminders.{n}") for n in ("add", "list")):
            for tool in await mcp.connect(reminders_server(str(state_dir / "reminders.db"))):
                tools.register(tool)
        if profile.allows_tool("kali.nmap"):
            # Auditoría de servicios propios (UC-2). Alcance y autorización del perfil (RF-SEC-06,
            # RF-LEG-01); el token de Kali, si lo hay, es un secreto scoped (nunca al modelo).
            for tool in await mcp.connect(kali_server(
                    cfg.kali.url, secrets.get(cfg.kali.token_env), profile.scope,
                    profile.authorization_ref, dry_run)):
                tools.register(tool)
        inventory = load_inventory(cfg.root, store.redactor)
        if profile.allows_tool("infra.inventory"):
            tools.register(InventoryTool(inventory))
        if profile.allows_tool("portainer.list_containers"):
            # Gestión de infra propia (UC-3). URL y api key del inventario (nunca al modelo).
            pt = inventory.get("portainer")
            for tool in await mcp.connect(portainer_server(
                    pt.get("url", ""), inventory.secret("portainer", "api_key") or "",
                    int(pt.get("endpoint", 1)), dry_run)):
                tools.register(tool)
        if profile.allows_tool("homeassistant.list_entities"):
            ha = inventory.get("homeassistant")
            ha_token = (inventory.secret("homeassistant", "token")
                        or inventory.secret("homeassistant", "api_key") or "")
            for tool in await mcp.connect(homeassistant_server(
                    ha.get("url", ""), ha_token, dry_run)):
                tools.register(tool)
        if profile.allows_tool("media.search"):
            jk, tr = inventory.get("jackett"), inventory.get("transmission")
            for tool in await mcp.connect(media_server(
                    jk.get("url", ""), inventory.secret("jackett", "api_key") or "",
                    tr.get("url", ""), tr.get("username", ""),
                    inventory.secret("transmission", "password") or "", dry_run)):
                tools.register(tool)
        if profile.allows_tool("nas.list"):
            nas = inventory.get("nas")
            for tool in await mcp.connect(nas_server(
                    nas.get("url", ""), nas.get("user", "") or nas.get("username", ""),
                    inventory.secret("nas", "password") or "",
                    inventory.secret("nas", "community") or "", dry_run)):
                tools.register(tool)
        if profile.allows_tool("cloudflare.zones"):
            cf_token = (inventory.secret("cloudflare", "api_key")
                        or inventory.secret("cloudflare", "token"))
            for tool in await mcp.connect(cloudflare_server(cf_token or "", dry_run)):
                tools.register(tool)

        session_limit = opts.session_budget_tokens or cfg.budget.session_tokens
        budget = BudgetTracker(sid, session_limit, cfg.budget.day_tokens, cfg.budget.warn_ratio,
                               tokens_spent_today(store), lambda e: store.emit(e))

        if opts.depth < cfg.subagents.max_depth:
            async def spawn(task: str, limit: int, target: str | None = None
                            ) -> tuple[str, str, str, int, int]:
                # El perfil destino debe estar autorizado por el perfil actual y pertenecer a
                # este segmento (RF-SEC-02): no se cruza el aislamiento por delegación.
                sub_profile = target or profile.name
                if sub_profile != profile.name and (
                        sub_profile not in profile.delegate_profiles
                        or not cfg.allows_profile(sub_profile)):
                    raise ValueError(f"delegación a {sub_profile!r} no permitida")
                child_dry = cfg.profile(sub_profile).dry_run if target else dry_run
                remaining = max(1, budget.session_limit - budget.spent)
                child = await run_session(
                    SessionOptions(task=task, profile=sub_profile, channel=opts.channel,
                                   dry_run=child_dry, parent_session_id=sid,
                                   allow_domains=opts.allow_domains,
                                   session_budget_tokens=min(limit, remaining),
                                   state_dir=opts.state_dir, workspace=workspace,
                                   depth=opts.depth + 1),
                    cfg, provider, store=store, approver=approver,
                    sandbox_factory=sandbox_factory, on_progress=on_progress)
                return (child.session_id, child.status, child.message, child.steps,
                        child.tokens)
            targets = {n: cfg.profile(n).description for n in profile.delegate_profiles
                       if n in cfg.profiles and cfg.allows_profile(n)}
            tools.register(DelegateTool(spawn, cfg.subagents.budget_tokens, profile.name, targets))

        skills = SkillRegistry(cfg.root / "skills").for_profile(profile.skills)
        context_ref: list[ContextManager] = []
        if skills:
            tools.register(LoadSkillTool(
                skills, lambda sk: context_ref[0].add_skill(sk.name, sk.body)))

        tools = tools.for_profile(profile)
        system, prompt_version = load_system_prompt(cfg)
        context = ContextManager(
            system=system + skills_index(skills), tools=[], task=opts.task, store=store,
            max_chars=cfg.loop.observation_max_chars,
            prune_failed_after=cfg.loop.prune_failed_after,
            lazy_tools=len(tools) > cfg.loop.lazy_tools_over)
        context_ref.append(context)
        if context.lazy_tools:
            tools.register(LoadToolsTool(context))
        context.tools = tools.specs()

        extra = [*profile.egress_extra, *opts.allow_domains]
        if sandbox_factory is not None:
            factory = sandbox_factory
        elif cfg.sandbox.backend == "broker":
            # El núcleo no toca Docker: pide el sandbox de su sesión al broker (ADR-0008).
            def factory() -> BrokerSandbox:
                return BrokerSandbox(cfg.broker_socket(), sid, extra, secrets)
        else:
            policy = EgressPolicy(cfg.egress_path())
            policy.set_default([*cfg.egress.allowlist, *cfg.segments[cfg.segment].egress_extra])

            def factory() -> DockerSandbox:
                return DockerSandbox(
                    session_id=sid, workspace=workspace, cfg=cfg.sandbox, policy=policy,
                    network=cfg.segment_network(), proxy_url=cfg.segment_proxy(),
                    segment=cfg.segment, allowlist_extra=extra, env=secrets)
        sandbox: Sandbox = LazySandbox(factory)

        store.emit(SessionStarted(
            session_id=sid, trace_id=opts.trace_id or sid, agent_profile=profile.name,
            channel=opts.channel, parent_session_id=opts.parent_session_id, task=opts.task,
            model=provider.name, prompt_version=prompt_version,
            skills_versions={n: s.full_version for n, s in skills.items()},
            tools_versions=tools.versions(), config_hash=cfg.config_hash(),
            harness_commit=harness_commit(cfg.root),
            budget={"session_tokens": session_limit, "day_tokens": cfg.budget.day_tokens},
            authorization_ref=profile.authorization_ref, dry_run=dry_run))

        loop = AgentLoop(
            session_id=sid, cfg=cfg, profile=profile, store=store, provider=provider,
            tools=tools, context=context, budget=budget, killswitch=killswitch,
            approver=approver or NoApprover(), workspace=workspace, sandbox=sandbox,
            dry_run=dry_run, on_progress=on_progress)

        # Memoria relevante (RF-17) y contexto de la conversación. Los subagentes no: su
        # contexto limpio es justo lo que se busca (RF-02).
        if opts.depth == 0:
            memories = state.relevant(profile.name, opts.task, cfg.memory.inject_limit)
            conversation = (await conversation_block(state, opts.thread_id, loop,
                                                     cfg.memory.keep_recent_exchanges)
                            if opts.thread_id else "")
            context.task = compose_task(opts.task, render_memories(memories), conversation)
            if memories:
                store.emit(MemoryEvent(session_id=sid, trace_id=sid, op="inject",
                                       memory_ids=[m.id for m in memories]))
        try:
            result = await loop.run()
        except asyncio.CancelledError:
            result = LoopResult("aborted", "cancelada por el usuario", 0, 0)
            raise
        finally:
            await sandbox.destroy()
            # Si el bucle reventó o fue cancelado, la sesión se cierra igualmente en auditoría.
            end = result or LoopResult("failed", "sesión interrumpida", 0, 0)
            store.emit(SessionEnded(session_id=sid, trace_id=opts.trace_id or sid,
                                    status=end.status, result=end.message, steps=end.steps))
            if opts.thread_id and opts.depth == 0:
                state.append_exchange(opts.thread_id, sid, opts.task, end.message, end.status)
    finally:
        await mcp.aclose()

    return SessionResult(sid, result.status, result.message, result.steps, result.tokens,
                         workspace)
