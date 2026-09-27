"""Skills: instrucciones Markdown cargadas bajo demanda (§9).

- Formato: `skills/<nombre>/SKILL.md` con front-matter YAML (`name`, `description`, `version`).
- Divulgación progresiva (RF-SK-05): al modelo solo llega el índice (nombre + descripción); el
  cuerpo entra al activarse con `skills.load`.
- Versión efectiva = `version@hash8` del fichero (RF-SK-07, P8).
- Las skills son contenido de confianza (instaladas por el usuario, RF-SK-04): entran como
  instrucciones, no como <untrusted>, y no se podan.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from argos.audit.events import ErrorKind, RiskClass, SkillActivation
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult

_FRONT = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    body: str
    path: Path
    digest: str

    @property
    def full_version(self) -> str:
        return f"{self.version}@{self.digest[:8]}"


def parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    match = _FRONT.match(text)
    if not match:
        raise SkillError(f"{path}: falta front-matter YAML (--- ... ---)")
    meta = yaml.safe_load(match.group(1)) or {}
    name = str(meta.get("name", ""))
    if not _NAME.match(name):
        raise SkillError(f"{path}: 'name' inválido ({name!r}); usa [a-z0-9-]")
    if not meta.get("description"):
        raise SkillError(f"{path}: falta 'description'")
    body = match.group(2).strip()
    if not body:
        raise SkillError(f"{path}: cuerpo vacío")
    return Skill(name, str(meta["description"]).strip(), str(meta.get("version", "0.0.0")), body,
                 path, hashlib.sha256(text.encode()).hexdigest())


class SkillRegistry:
    def __init__(self, skills_dir: Path) -> None:
        self.dir = skills_dir
        self.skills: dict[str, Skill] = {}
        self.errors: list[str] = []
        for path in sorted(skills_dir.glob("*/SKILL.md")):
            try:
                skill = parse_skill(path)
                self.skills[skill.name] = skill
            except SkillError as exc:
                self.errors.append(str(exc))

    def for_profile(self, patterns: list[str]) -> dict[str, Skill]:
        return {n: s for n, s in self.skills.items()
                if any(fnmatch.fnmatchcase(n, p) for p in patterns)}

    def install(self, source: str, force: bool = False) -> Skill:
        """Instala desde un directorio local o un repo git (`https://…`, `git@…`)."""
        with tempfile.TemporaryDirectory(prefix="argos-skill-") as tmp:
            if re.match(r"^(https://|git@)", source):
                proc = subprocess.run(["git", "clone", "--depth", "1", source, f"{tmp}/repo"],
                                      capture_output=True, text=True)
                if proc.returncode != 0:
                    raise SkillError(f"git clone falló: {proc.stderr.strip()[:300]}")
                src = Path(tmp) / "repo"
                shutil.rmtree(src / ".git", ignore_errors=True)
            else:
                src = Path(source).expanduser().resolve()
            skill = parse_skill(src / "SKILL.md")
            dest = self.dir / skill.name
            if dest.exists() and not force:
                raise SkillError(f"la skill {skill.name} ya existe (usa --force para actualizar)")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        installed = parse_skill(dest / "SKILL.md")
        self.skills[installed.name] = installed
        return installed


def skills_index(skills: dict[str, Skill]) -> str:
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
    return ("\n\n## Skills disponibles\nInstrucciones especializadas. Si una encaja con la tarea, "
            f"cárgala con skills.load antes de empezar.\n{lines}")


class LoadSkillTool(Tool):
    name = "skills.load"
    description = "Carga las instrucciones completas de una skill del índice."
    parameters = {"type": "object", "required": ["name"],
                  "properties": {"name": {"type": "string"}}}
    risk_class = RiskClass.READ
    idempotent = True

    def __init__(self, skills: dict[str, Skill], on_load) -> None:
        self._skills = skills
        self._on_load = on_load   # callback(skill) -> None: la inyecta en el contexto

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", ""))
        skill = self._skills.get(name)
        if skill is None:
            raise ToolError(f"skill desconocida: {name!r} (disponibles: {list(self._skills)})",
                            ErrorKind.VALIDATION_ERROR)
        self._on_load(skill)
        ctx.emit(SkillActivation(session_id=ctx.session_id, turn_id=ctx.turn_id,
                                 skill=skill.name, version=skill.full_version))
        return ToolResult(f"skill {skill.name} ({skill.full_version}) cargada: sus instrucciones "
                          "están ahora en tu contexto de sistema.")
