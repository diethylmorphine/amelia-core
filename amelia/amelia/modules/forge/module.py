"""Навык управления навыками: Амелия дописывает себе функции по запросу.

Сознательно не автоматизировано до конца: новый навык появляется в статусе
pending и начинает работать только после явного «включи навык <имя>».
"""

from __future__ import annotations

from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.logic.rules import strip_trigger
from ...core.module import Module

STATUS_MARK = {
    "enabled": "●",
    "pending": "○",
    "disabled": "·",
    "broken": "×",
}


class ForgeModule(Module):
    name = "forge"
    description = "Написать себе новый навык, включить, выключить, убрать"
    intents = (
        Intent.SKILL_NEW,
        Intent.SKILL_LIST,
        Intent.SKILL_SHOW,
        Intent.SKILL_ON,
        Intent.SKILL_OFF,
        Intent.SKILL_DROP,
    )
    priority = 5  # раньше всех: управление собой важнее любых навыков

    async def handle(self, request: Request, intent: str) -> Response:
        payload = strip_trigger(request.text)

        if intent == Intent.SKILL_LIST:
            return self.reply(self._list(), intent)
        if intent == Intent.SKILL_NEW:
            return await self._create(payload, intent)
        if intent == Intent.SKILL_SHOW:
            return self._show(payload, intent)

        if not payload:
            return self.reply("Уточни имя навыка.", intent)

        forge = self.kernel.forge
        if intent == Intent.SKILL_ON:
            result = await forge.approve(payload)
        elif intent == Intent.SKILL_OFF:
            result = await forge.disable(payload)
        else:
            result = await forge.drop(payload)
        return self.reply(result.message, intent, ok=result.ok)

    async def _create(self, payload: str, intent: str) -> Response:
        if not payload:
            return self.reply("Опиши, что новый навык должен делать.", intent)
        result = await self.kernel.forge.create(payload)
        return self.reply(result.message, intent, ok=result.ok, skill=result.name)

    def _show(self, payload: str, intent: str) -> Response:
        if not payload:
            return self.reply("Какой навык показать?", intent)
        source = self.kernel.forge.source_of(payload)
        if source is None:
            return self.reply(f"Исходника навыка «{payload}» нет.", intent)
        return self.reply(source, intent)

    def _list(self) -> str:
        lines = ["Встроенные модули:"]
        generated = {item["name"] for item in self.kernel.forge.catalog()}
        for name, description in self.kernel.modules.catalog().items():
            if name not in generated:
                lines.append(f"  ● {name} — {description}")

        catalog = self.kernel.forge.catalog()
        lines.append("\nНаписанные Амелией:")
        if not catalog:
            lines.append("  пока ни одного — скажи «научись <что делать>»")
        for item in catalog:
            mark = STATUS_MARK.get(item.get("status", ""), "?")
            lines.append(f"  {mark} {item['name']} [{item.get('status')}] — {item.get('description', '')}")
            if item.get("status") == "pending":
                lines.append(f"      ждёт: «включи навык {item['name']}»")
            if item.get("status") == "broken":
                lines.append(f"      ошибка: {str(item.get('error', ''))[:120]}")
        return "\n".join(lines)
