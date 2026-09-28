"""Carga de configuración y perfiles, con hash para reproducibilidad (RF-OB-10, P8)."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from argos.model.base import Route

ROOT = Path(__file__).resolve().parents[2]


class CodexCfg(BaseModel):
    instructions_file: str | None = None
    overrides: dict[str, str] = Field(default_factory=dict)
    disable_features: list[str] = Field(default_factory=list)


class RouteCfg(BaseModel):
    model: str | None = None
    effort: str | None = None


class ModelCfg(BaseModel):
    provider: str = "codex"
    name: str | None = None
    timeout_s: int = 300
    cost_per_mtok: dict[str, float] = Field(
        default_factory=lambda: {"input": 0.0, "cached_input": 0.0, "output": 0.0})
    codex: CodexCfg = CodexCfg()
    # RF-CTX-05: decide = bucle normal; hard = escalado tras fallos; internal = trabajo interno.
    routes: dict[str, RouteCfg] = Field(default_factory=dict)
    escalate_after_failures: int = 2
    summarize_observations_over: int = 0   # chars; 0 = desactivado

    def route(self, name: str) -> Route:
        r = self.routes.get(name) or self.routes.get("decide") or RouteCfg()
        return Route(name, r.model, r.effort)


class LoopCfg(BaseModel):
    max_steps: int = 25
    repeat_limit: int = 3
    observation_max_chars: int = 4000
    prune_failed_after: int = 2
    lazy_tools_over: int = 40


class MemoryCfg(BaseModel):
    inject_limit: int = 5              # RF-17: memorias relevantes por sesión
    keep_recent_exchanges: int = 3     # intercambios literales de la conversación


class SubagentsCfg(BaseModel):
    max_depth: int = 1
    budget_tokens: int = 60_000


class BudgetCfg(BaseModel):
    session_tokens: int = 300_000
    day_tokens: int = 3_000_000
    warn_ratio: float = 0.8


class ApprovalCfg(BaseModel):
    require_for: list[str] = Field(default_factory=lambda: ["destructive", "offensive"])
    timeout_s: int = 120


class ConcurrencyCfg(BaseModel):
    max_sessions: int = 4


class SandboxCfg(BaseModel):
    # broker = vía el servicio que posee el socket Docker (ADR-0008); docker = CLI directa (dev).
    backend: str = "broker"
    image: str = "argos-sandbox:latest"
    cpus: str = "1.0"
    memory: str = "1g"
    pids: int = 256
    command_timeout_s: int = 120


class EgressCfg(BaseModel):
    allowlist: list[str] = Field(default_factory=list)


class SegmentCfg(BaseModel):
    """Segmento de seguridad (RF-SEC-02): núcleo, red, proxy y datos propios."""

    profiles: list[str]
    egress_extra: list[str] = Field(default_factory=list)
    network: str | None = None      # por defecto argos_sandbox_<segmento>
    proxy_url: str | None = None    # por defecto http://egress-<segmento>:3128


class KaliCfg(BaseModel):
    """MCP de Kali (UC-2). El alcance y la autorización son del perfil, no de aquí."""

    url: str = "http://kali:8000"
    token_env: str = "KALI_API_TOKEN"     # secreto opcional del perfil pentest


class MatrixCfg(BaseModel):
    """Canal Matrix (RF-07). El token del bot nunca va aquí: variable `token_env`."""

    homeserver: str | None = None
    user_id: str | None = None
    allowed_users: list[str] = Field(default_factory=list)
    profile: str = "personal"
    notify_room: str | None = None      # sala de control para aprobaciones de otros canales
    token_env: str = "ARGOS_MATRIX_TOKEN"
    progress_interval_s: float = 3.0


class AuditCfg(BaseModel):
    redact_pii: bool = True
    retention_days: int = 90


class SecretRef(BaseModel):
    name: str
    powerful: bool = False


class Profile(BaseModel):
    name: str
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=lambda: ["*"])
    reads_untrusted: bool = False
    secrets: list[SecretRef] = Field(default_factory=list)
    egress_extra: list[str] = Field(default_factory=list)
    dry_run: bool = False
    retention_days: int | None = None
    scope: list[str] = Field(default_factory=list)
    authorization_ref: str | None = None
    # Perfil con capacidad potente (credenciales de servicios, gestión de infra vía inventario…),
    # aunque sus secretos no vivan en `secrets:` sino en el inventario. Gobierna P2 (RF-SEC-03).
    powerful: bool = False
    # Perfiles a los que este perfil puede delegar subtareas (orquestador → especialistas).
    delegate_profiles: list[str] = Field(default_factory=list)

    @property
    def is_powerful(self) -> bool:
        return self.powerful or any(s.powerful for s in self.secrets)

    @model_validator(mode="after")
    def _separate_untrusted_from_power(self) -> Profile:
        # P2 / RF-SEC-03: quien lee datos no confiables no puede tener credenciales potentes.
        if self.reads_untrusted and self.is_powerful:
            raise ValueError(
                f"perfil {self.name!r}: reads_untrusted=true es incompatible con capacidad"
                " potente (secretos 'powerful' o powerful=true) (P2, RF-SEC-03)")
        return self

    def allows_tool(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatchcase(tool_name, pat) for pat in self.tools)


class Config(BaseModel):
    data_dir: Path = Path("var")
    model: ModelCfg = ModelCfg()
    loop: LoopCfg = LoopCfg()
    budget: BudgetCfg = BudgetCfg()
    approval: ApprovalCfg = ApprovalCfg()
    concurrency: ConcurrencyCfg = ConcurrencyCfg()
    subagents: SubagentsCfg = SubagentsCfg()
    memory: MemoryCfg = MemoryCfg()
    sandbox: SandboxCfg = SandboxCfg()
    egress: EgressCfg = EgressCfg()
    audit: AuditCfg = AuditCfg()
    matrix: MatrixCfg = MatrixCfg()
    kali: KaliCfg = KaliCfg()
    segments: dict[str, SegmentCfg] = Field(
        default_factory=lambda: {"main": SegmentCfg(profiles=["*"])})
    segment: str = "main"            # activo: $ARGOS_SEGMENT

    root: Path = ROOT
    profiles: dict[str, Profile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_segments(self) -> Config:
        if self.segment not in self.segments:
            raise ValueError(f"segmento desconocido: {self.segment!r} ({list(self.segments)})")
        owners: dict[str, str] = {}
        for seg, sc in self.segments.items():
            for prof in sc.profiles:
                if prof != "*" and prof in owners:
                    raise ValueError(f"perfil {prof!r} en dos segmentos: {owners[prof]}, {seg}")
                owners[prof] = seg
        return self

    @property
    def base_path(self) -> Path:
        return self.data_dir if self.data_dir.is_absolute() else self.root / self.data_dir

    @property
    def data_path(self) -> Path:
        """Datos del segmento activo (auditoría, workspaces, blobs): aislados por segmento."""
        p = self.base_path / "segments" / self.segment
        p.mkdir(parents=True, exist_ok=True)
        return p

    def egress_path(self, segment: str | None = None) -> Path:
        """Política y eventos del proxy: fuera del directorio de datos del núcleo."""
        return self.base_path / "egress" / (segment or self.segment)

    @property
    def api_socket(self) -> Path:
        """Socket de la API del núcleo persistente (RF-04), dentro de un directorio 700."""
        return self.data_path / "run" / "argos.sock"

    def broker_socket(self, segment: str | None = None) -> Path:
        return self.base_path / "broker" / (segment or self.segment) / "broker.sock"

    def segment_of(self, profile: str) -> str | None:
        for seg, sc in self.segments.items():
            if profile in sc.profiles or "*" in sc.profiles:
                return seg
        return None

    def allows_profile(self, profile: str) -> bool:
        sc = self.segments[self.segment]
        return profile in sc.profiles or "*" in sc.profiles

    def segment_network(self, segment: str | None = None) -> str:
        seg = segment or self.segment
        return self.segments[seg].network or f"argos_sandbox_{seg}"

    def segment_proxy(self, segment: str | None = None) -> str:
        seg = segment or self.segment
        return self.segments[seg].proxy_url or f"http://egress-{seg}:3128"

    def profile(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            raise KeyError(
                f"perfil desconocido: {name!r} (disponibles: {list(self.profiles)})") from None

    def config_hash(self) -> str:
        """Hash estable de la configuración efectiva (sin rutas locales)."""
        data = self.model_dump(mode="json", exclude={"root", "segment"})
        return sha256_text(json.dumps(data, sort_keys=True))[:16]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_config(root: Path = ROOT, overrides: dict[str, Any] | None = None) -> Config:
    raw = yaml.safe_load((root / "config" / "argos.yaml").read_text()) or {}
    for dotted, value in (overrides or {}).items():
        node = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    raw.setdefault("segment", os.environ.get("ARGOS_SEGMENT", "main"))
    profiles = {}
    for path in sorted((root / "config" / "profiles").glob("*.yaml")):
        prof = Profile.model_validate(yaml.safe_load(path.read_text()))
        profiles[prof.name] = prof
    return Config.model_validate({**raw, "root": root, "profiles": profiles})


def harness_commit(root: Path = ROOT) -> str:
    """Commit del arnés + marca `-dirty` si hay cambios sin confirmar (RF-OB-10)."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=root,
            capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
            capture_output=True, text=True).stdout.strip()
        return commit + ("-dirty" if dirty else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
