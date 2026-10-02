"""Publicar el informe de un perfil aislado (pentest/osint) en Notion (ADR-0027).

El perfil aislado no ve `secrets/` ni tiene la tool de Notion (otro segmento). `report.publish`
no crea nada: lee el informe del workspace y emite un evento `publish_request` que el canal reenvía
al daemon principal, que crea la subpágina LITERAL por la API (sin que ningún modelo lea el texto
no confiable). El resultado —la URL— lo ve el usuario.
"""

from __future__ import annotations

from typing import Any

from argos.audit.events import ErrorKind, PublishRequested, RiskClass
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_REPORT_CHARS = 100_000


class PublishReportTool(Tool):
    name = "report.publish"
    version = "1.0.0"
    risk_class = RiskClass.WRITE
    idempotent = False
    description = (
        "Publica un informe en Notion como subpágina. Solo tras terminar la auditoría o "
        "investigación y con el informe ya escrito en el workspace. No esperes el resultado: la "
        "URL le llega al usuario. parent = la URL o id de la página de Notion que indicó."
    )
    parameters = {
        "type": "object",
        "required": ["title", "parent"],
        "properties": {
            "title": {"type": "string", "description": "Título de la subpágina"},
            "parent": {"type": "string", "description": "URL o id de la página padre en Notion"},
            "path": {
                "type": "string",
                "description": "Ruta del informe en el workspace (por defecto out/informe.md)",
            },
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = str(args.get("title", "")).strip()
        parent = str(args.get("parent", "")).strip()
        rel = str(args.get("path") or "out/informe.md").strip().lstrip("/")
        if not title or not parent:
            raise ToolError("faltan title o parent", ErrorKind.VALIDATION_ERROR)
        if ".." in rel.split("/"):
            raise ToolError("ruta inválida", ErrorKind.VALIDATION_ERROR)
        path = ctx.workspace / rel
        if not path.is_file():
            raise ToolError(
                f"no existe el informe {rel}: escríbelo primero en el workspace",
                ErrorKind.VALIDATION_ERROR,
            )
        markdown = path.read_text(encoding="utf-8", errors="replace")[:MAX_REPORT_CHARS]
        ctx.emit(
            PublishRequested(
                session_id=ctx.session_id, title=title, markdown=markdown, parent=parent
            )
        )
        return ToolResult(
            f"Publicación de «{title}» solicitada; el enlace de Notion le llegará al usuario. "
            "Termina con un aviso breve.",
            data={"publish_title": title},
        )
