"""Служебный навык: помощь, пинг, статус."""

from __future__ import annotations

from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.module import Module


class SystemModule(Module):
    name = "system"
    description = "Помощь, состояние ядра, проверка связи"
    intents = (Intent.HELP, Intent.PING, Intent.STATUS)
    priority = 10

    async def handle(self, request: Request, intent: str) -> Response:
        if intent == Intent.PING:
            return self.reply("Я здесь.", intent)
        if intent == Intent.STATUS:
            return self.reply(await self._status(), intent)
        return self.reply(self._help(), intent)

    def _help(self) -> str:
        lines = ["Что я умею:"]
        for name, description in self.kernel.modules.catalog().items():
            lines.append(f"  • {name} — {description}")
        tools = self.kernel.agent.toolbox()
        if tools:
            lines.append("\nИнструменты, которыми пользуюсь сама:")
            lines.append("  " + ", ".join(tools))
        lines.append(
            "\nКлючевые слова: помощь · статус · запомни … · вспомни … · напомни … · "
            "напоминания · научись … · навыки · забудь разговор"
        )
        return "\n".join(lines)

    def _language_line(self) -> str:
        stats = getattr(self.kernel.llm, "stats", {})
        detected = stats.get("foreign_detected", 0)
        if not detected:
            return f"Язык: {self.kernel.config.response_language}, иероглифов не было"
        fixed = stats.get("foreign_fixed_by_retry", 0)
        stripped = stats.get("foreign_stripped", 0)
        return (f"Язык: {self.kernel.config.response_language}, "
                f"иероглифы ловились {detected} раз (переписано {fixed}, вырезано {stripped})")

    async def _status(self) -> str:
        kernel = self.kernel
        config = kernel.config
        online = "доступна" if await kernel.llm.available(recheck=True) else "недоступна"
        sessions, turns = await kernel.sessions.stats()
        agent = f"вкл, до {config.agent_max_steps} шагов" if config.agent_enabled else "выкл"
        scheduler = "вкл" if config.scheduler_enabled else "выкл"
        return (
            f"Модель: {config.llm_model} ({online})\n"
            f"Модулей: {len(kernel.modules)}, инструментов: {len(kernel.agent.toolbox())}\n"
            f"Память: {await kernel.memory.count()} записей\n"
            f"Сессии: {sessions}, реплик в окне: {turns}\n"
            f"Агент: {agent}\n"
            f"Планировщик: {scheduler}, напоминаний: {await kernel.scheduler.count_pending()}\n"
            f"{self._language_line()}\n"
            f"{kernel.tracer.summary()}\n"
            f"База: {config.db_path}"
        )
