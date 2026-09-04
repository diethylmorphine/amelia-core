"""Маршрутизатор интентов: сначала правила, потом модель.

Это и есть «логико-интеллектуальный» слой. Дешёвое и предсказуемое
решается регулярками; LLM подключается только когда правила промолчали.
"""

from __future__ import annotations

import logging

from .context import Request
from .logic.intents import FALLBACK_INTENT
from .logic.rules import match_rules

log = logging.getLogger(__name__)

_CLASSIFIER_SYSTEM = (
    "Ты классификатор намерений. Верни РОВНО одно слово — идентификатор интента "
    "из предложенного списка, без пояснений и знаков препинания."
)


class Router:
    def __init__(self, registry, llm) -> None:
        self.registry = registry
        self.llm = llm

    async def resolve(self, request: Request) -> str:
        intent = match_rules(request.text)
        if intent:
            log.debug("Интент по правилу: %s", intent)
            return intent

        known = self._known_intents()
        if known and await self.llm.available():
            guess = await self._classify(request, known)
            if guess in known:
                log.debug("Интент по модели: %s", guess)
                return guess

        return FALLBACK_INTENT

    def _known_intents(self) -> list[str]:
        seen: list[str] = []
        for module in self.registry:
            for intent in module.intents:
                if intent not in seen:
                    seen.append(intent)
        return seen

    async def _classify(self, request: Request, known: list[str]) -> str:
        prompt = f"Список интентов: {', '.join(known)}\n\nСообщение: {request.text}\n\nИнтент:"
        raw = await self.llm.complete(
            prompt, system=_CLASSIFIER_SYSTEM, language=request.meta.get("lang")
        )
        return raw.strip().split()[0].strip(".,:;\"'").lower() if raw.strip() else FALLBACK_INTENT
