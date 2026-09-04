"""Свободный диалог: сессия, память и агентный цикл.

Три уровня, каждый подстраховывает следующий:
  агент с инструментами -> прямой ответ модели с контекстом -> честное «модель лежит».
"""

from __future__ import annotations

from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.module import Module
from ...core.session import session_key

SYSTEM = (
    "Ты Амелия — личный локальный ассистент. Отвечай кратко и по делу. "
    "Опирайся на контекст разговора и на записи из памяти, "
    "не выдумывай фактов, которых там нет."
)


class ChatModule(Module):
    name = "chat"
    description = "Свободный диалог с памятью разговора и инструментами"
    intents = (Intent.CHAT, Intent.FORGET)
    priority = 900  # последний в очереди: забирает всё, что не разобрали остальные

    async def handle(self, request: Request, intent: str) -> Response:
        if intent == Intent.FORGET:
            return await self._forget(request, intent)

        if not await self.kernel.llm.available():
            return self.reply(
                "Модель сейчас недоступна — запусти Ollama. Команды по ключевым словам "
                "при этом работают: помощь, запомни, вспомни, напомни.",
                intent,
            )

        context = await self._context(request)

        if self.kernel.config.agent_enabled:
            result = await self.kernel.agent.run(request, context)
            if not result.fell_back and result.text:
                return self.reply(result.text, intent, steps=result.steps, tools=",".join(result.calls))

        prompt = f"{context}\n\nВопрос: {request.text}" if context else request.text
        answer = await self.kernel.llm.complete(
            prompt, system=SYSTEM, on_token=request.on_token,
            language=request.meta.get("lang"),
        )
        return self.reply(answer or "Модель промолчала.", intent, steps=0)

    async def _context(self, request: Request) -> str:
        """Контекст = история разговора + релевантные записи памяти."""
        parts: list[str] = []
        key = request.meta.get("session") or session_key(request.user_id, request.source)

        dialogue = await self.kernel.sessions.as_prompt_block(key)
        if dialogue:
            parts.append(dialogue)

        found = await self.kernel.memory.recall(request.text, request.user_id, limit=4)
        if found:
            parts.append("Из памяти:\n" + "\n".join(f"- {memory.text}" for memory in found))
        return "\n\n".join(parts)

    async def _forget(self, request: Request, intent: str) -> Response:
        key = request.meta.get("session") or session_key(request.user_id, request.source)
        removed = await self.kernel.sessions.clear(key)
        return self.reply(
            f"Контекст разговора очищен ({removed} реплик). Долговременная память цела.",
            intent, removed=removed, skip_session=True,
        )
