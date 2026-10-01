"""Tool `infra.inventory`: el agente consulta la documentación de infraestructura sin secretos."""

from __future__ import annotations

import json
from typing import Any

from argos.audit.events import RiskClass
from argos.inventory import Inventory
from argos.tools.base import Tool, ToolContext, ToolResult


class InventoryTool(Tool):
    name = "infra.inventory"
    description = (
        "Consulta el inventario de infraestructura (servicios, URLs, usuarios, notas). "
        "No muestra secretos; solo indica qué credenciales hay configuradas."
    )
    parameters = {"type": "object", "properties": {}}
    risk_class = RiskClass.READ
    idempotent = True

    def __init__(self, inventory: Inventory) -> None:
        self._inv = inventory

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        view = self._inv.public_view()
        if not view:
            return ToolResult("(inventario vacío: secrets/inventory.yaml sin servicios)")
        return ToolResult(json.dumps(view, ensure_ascii=False, indent=2))
