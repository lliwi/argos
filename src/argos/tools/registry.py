"""Catálogo de tools (RF-11) filtrado por perfil (RF-05, RF-SEC-01)."""

from __future__ import annotations

from argos.config import Profile
from argos.model.base import ToolSpec
from argos.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool duplicada: {tool.name}")
        self._tools[tool.name] = tool

    def for_profile(self, profile: Profile) -> ToolRegistry:
        sub = ToolRegistry()
        for tool in self._tools.values():
            if profile.allows_tool(tool.name):
                sub.register(tool)
        return sub

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(t.name, t.description, t.parameters) for t in self._tools.values()]

    def versions(self) -> dict[str, str]:
        return {t.name: t.version for t in self._tools.values()}

    async def aclose(self) -> None:
        for tool in self._tools.values():
            await tool.aclose()
