"""Навык поиска по памяти."""

from __future__ import annotations

from ...core.agent import Tool
from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.logic.rules import strip_trigger
from ...core.module import Module


class RecallModule(Module):
    name = "recall"
    description = "Найти что-то в сохранённой памяти"
    intents = (Intent.RECALL,)
    priority = 20

    async def handle(self, request: Request, intent: str) -> Response:
        query = strip_trigger(request.text)
        if not query:
            return self.reply("Что искать?", intent)

        found = await self.kernel.memory.recall(query, request.user_id, limit=5)
        if not found:
            return self.reply("Ничего не нашлось.", intent)

        lines = [f"Нашёл {len(found)}:"]
        for memory in found:
            score = f" [{memory.score:.2f}]" if memory.score else ""
            lines.append(f"  • {memory.text[:200]}{score}  ({memory.created_at[:10]})")
        return self.reply("\n".join(lines), intent, hits=len(found))

    # ------------------------------------------------------------------ #
    def tools(self) -> list[Tool]:
        return [Tool(
            name="search_memory",
            description="Найти в долговременной памяти всё по теме запроса",
            args={"query": "что искать", "limit": "сколько записей, по умолчанию 5"},
            run=self._tool_search,
        )]

    async def _tool_search(self, query: str = "", limit: int = 5, user_id: str = "local") -> str:
        if not query.strip():
            return "пустой запрос"
        try:
            limit = max(1, min(int(limit), 10))
        except (TypeError, ValueError):
            limit = 5
        found = await self.kernel.memory.recall(query, user_id, limit=limit)
        if not found:
            return "ничего не найдено"
        return " | ".join(f"{m.text[:200]} ({m.created_at[:10]})" for m in found)
