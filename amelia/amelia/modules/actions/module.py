"""Журнал самостоятельных действий и откат последнего.

У кузницы пять шлюзов между моделью и выполнением кода. У агента их не было
вовсе: трёхмиллиардная модель молча писала в память и ставила задачи.
Полностью запрещать — потерять половину пользы, поэтому здесь другой подход:
каждое изменение видно и обратимо.
"""

from __future__ import annotations

from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.module import Module


class ActionsModule(Module):
    name = "actions"
    description = "Что Амелия сделала сама и как это отменить"
    intents = (Intent.ACTIONS, Intent.UNDO)
    priority = 12

    async def handle(self, request: Request, intent: str) -> Response:
        if intent == Intent.UNDO:
            return self.reply(await self.kernel.agent.undo_last(request.user_id), intent)

        records = await self.kernel.agent.recent_actions(request.user_id)
        if not records:
            return self.reply("Сама я пока ничего не делала.", intent)

        lines = [f"Мои действия ({len(records)}):"]
        for record in records:
            mark = "×" if record["undone"] else "•"
            lines.append(f"  {mark} {record['created_at'][:16]} {record['tool']} — {record['result'][:80]}")
        lines.append("\nОткатить последнее: «отмени последнее действие»")
        return self.reply("\n".join(lines), intent)
