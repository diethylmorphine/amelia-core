"""Напоминания и повторяющиеся задачи."""

from __future__ import annotations

import re

from ...core.agent import Tool, ToolResult
from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.logic.rules import strip_trigger
from ...core.module import Module
from .timeparse import humanize, humanize_every, parse


class RemindModule(Module):
    name = "remind"
    description = "Напомнить о чём-то в срок или по расписанию"
    intents = (Intent.REMIND, Intent.REMIND_LIST, Intent.REMIND_CANCEL)
    priority = 15

    async def setup(self, kernel) -> None:
        await super().setup(kernel)
        kernel.bus.on("job.fired", self._on_fired)

    async def _on_fired(self, job) -> None:
        """Задача сработала — кладём в память, чтобы след остался даже без клиента."""
        if job.kind == "remind":
            await self.kernel.memory.remember(
                f"сработало напоминание: {job.payload}",
                source="scheduler", user_id=job.user_id, tags=["напоминание"],
            )

    # ------------------------------------------------------------------ #
    async def handle(self, request: Request, intent: str) -> Response:
        if intent == Intent.REMIND_LIST:
            return self.reply(await self._list(request.user_id), intent)
        if intent == Intent.REMIND_CANCEL:
            return await self._cancel(request, intent)
        return await self._create(request, intent)

    async def _create(self, request: Request, intent: str) -> Response:
        payload = strip_trigger(request.text)
        when, every, text = parse(payload, tz=self.kernel.config.tz)

        if when is None:
            return self.reply(
                "Не разобрала время. Попробуй «напомни через 20 минут …», "
                "«напомни завтра в 9 …» или «напоминай каждый день в 8 …».",
                intent,
            )
        text = re.sub(r"^(мне|нам)\b", "", text, flags=re.I).strip() or "напоминание"

        job_id = await self.kernel.scheduler.schedule(
            text, when, user_id=request.user_id, source=request.source,
            every=every, meta={"chat_id": request.meta.get("chat_id")},
        )
        stamp = humanize(when, self.kernel.config.tz)
        repeat = f", {humanize_every(every)}" if every else ""
        return self.reply(f"Напомню {stamp}{repeat}: {text}  (#{job_id})", intent, job_id=job_id)

    async def _list(self, user_id: str) -> str:
        jobs = await self.kernel.scheduler.pending(user_id)
        if not jobs:
            return "Ничего не запланировано."
        zone = self.kernel.config.tz
        lines = [f"Запланировано ({len(jobs)}):"]
        for job in jobs:
            repeat = f" · {humanize_every(job.every)}" if job.every else ""
            lines.append(f"  #{job.id} — {humanize(job.when, zone)}{repeat} — {job.payload}")
        lines.append("\nОтменить: «отмени напоминание N»")
        return "\n".join(lines)

    async def _cancel(self, request: Request, intent: str) -> Response:
        numbers = re.findall(r"\d+", request.text)
        if not numbers:
            return self.reply("Какое отменить? Скажи «отмени напоминание N».", intent)
        job_id = int(numbers[0])
        ok = await self.kernel.scheduler.cancel(job_id, request.user_id)
        return self.reply(
            f"Напоминание #{job_id} отменено." if ok else f"Напоминания #{job_id} в очереди нет.",
            intent, ok=ok,
        )

    # ------------------------------------------------------------------ #
    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="schedule_reminder",
                description="Запланировать напоминание. Время в свободной форме: «через 20 минут», «завтра в 9»",
                args={"text": "о чём напомнить", "when": "когда, по-русски"},
                run=self._tool_schedule,
                mutates=True,
                undo=self._undo_schedule,
            ),
            Tool(
                name="list_reminders",
                description="Показать запланированные напоминания",
                args={},
                run=self._tool_list,
            ),
        ]

    async def _tool_schedule(self, text: str = "", when: str = "", user_id: str = "local") -> ToolResult:
        moment, every, _ = parse(when or text, tz=self.kernel.config.tz)
        if moment is None:
            return ToolResult("не разобрала время")
        job_id = await self.kernel.scheduler.schedule(
            text or when, moment, user_id=user_id, every=every
        )
        stamp = humanize(moment, self.kernel.config.tz)
        return ToolResult(f"запланировано #{job_id} на {stamp}", undo=str(job_id))

    async def _undo_schedule(self, token: str) -> bool:
        try:
            return await self.kernel.scheduler.cancel(int(token))
        except (TypeError, ValueError):
            return False

    async def _tool_list(self, user_id: str = "local") -> str:
        jobs = await self.kernel.scheduler.pending(user_id)
        if not jobs:
            return "очередь пуста"
        zone = self.kernel.config.tz
        return "; ".join(f"#{job.id} {humanize(job.when, zone)} {job.payload}" for job in jobs)
