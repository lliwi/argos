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
from argos.model.base import ModelProvider
from argos.model.factory import engine_instructions
from argos.sandbox.docker_sandbox import DockerSandbox, EgressPolicy, Sandbox
from argos.secrets import load_profile_secrets
from argos.skills import LoadSkillTool, SkillRegistry, skills_index
from argos.state import StateStore, render_memories
from argos.tools.mcp_client import McpConnections, reminders_server
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

        session_limit = opts.session_budget_tokens or cfg.budget.session_tokens
        budget = BudgetTracker(sid, session_limit, cfg.budget.day_tokens, cfg.budget.warn_ratio,
                               tokens_spent_today(store), lambda e: store.emit(e))

        if opts.depth < cfg.subagents.max_depth:
            async def spawn(task: str, limit: int) -> tuple[str, str, str, int, int]:
                remaining = max(1, budget.session_limit - budget.spent)
                child = await run_session(
                    SessionOptions(task=task, profile=profile.name, channel=opts.channel,
                                   dry_run=dry_run, parent_session_id=sid,
                                   allow_domains=opts.allow_domains,
                                   session_budget_tokens=min(limit, remaining),
                                   state_dir=opts.state_dir, workspace=workspace,
                                   depth=opts.depth + 1),
                    cfg, provider, store=store, approver=approver,
                    sandbox_factory=sandbox_factory, on_progress=on_progress)
                return (child.session_id, child.status, child.message, child.steps,
                        child.tokens)
            tools.register(DelegateTool(spawn, cfg.subagents.budget_tokens))

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

        policy = EgressPolicy(cfg.egress_path())
        policy.set_default([*cfg.egress.allowlist, *cfg.segments[cfg.segment].egress_extra])
        factory = sandbox_factory or (lambda: DockerSandbox(
            session_id=sid, workspace=workspace, cfg=cfg.sandbox, policy=policy,
            network=cfg.segment_network(), proxy_url=cfg.segment_proxy(), segment=cfg.segment,
            allowlist_extra=[*profile.egress_extra, *opts.allow_domains], env=secrets))
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
