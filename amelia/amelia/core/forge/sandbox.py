"""Урезанное ядро для сгенерированных навыков.

Валидатор ловит `import os`, но не ловит «прочитал всю базу и записал в
заметку» — потому что для этого хватает обычного self.kernel. А в config
лежит токен бота: любой прошедший проверку навык мог его прочитать.

Здесь навык получает не ядро, а фасад: только своя область памяти, модель
на чтение и безобидная часть настроек. Ни шины, ни планировщика, ни кузницы,
ни реестра других модулей.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Что из настроек навыку видеть можно. Токенов и путей здесь нет намеренно.
SAFE_CONFIG_FIELDS = ("llm_model", "embed_model", "timezone_name", "session_window")


class ScopedMemory:
    """Память навыка: только записи этого пользователя, помеченные навыком."""

    def __init__(self, memory, skill: str) -> None:
        self._memory = memory
        self._skill = skill

    async def remember(self, text: str, *, user_id: str = "local", tags=None, **_) -> int:
        marks = list(tags or []) + [f"skill:{self._skill}"]
        return await self._memory.remember(
            text, source=f"skill:{self._skill}", user_id=user_id, tags=marks
        )

    async def recall(self, query: str, user_id: str = "local", limit: int = 5, **_):
        # include_shared=False: навык не видит даже общие записи, только свои.
        return await self._memory.recall(query, user_id, limit=limit, include_shared=False)

    async def count(self, user_id: str | None = None) -> int:
        return await self._memory.count(user_id=user_id)


class ReadOnlyLLM:
    def __init__(self, llm) -> None:
        self._llm = llm

    async def available(self, recheck: bool = False) -> bool:
        return await self._llm.available(recheck)

    async def complete(self, prompt: str, system: str | None = None, on_token=None) -> str:
        return await self._llm.complete(prompt, system=system, on_token=on_token)

    async def embed(self, text: str) -> list[float]:
        return await self._llm.embed(text)


@dataclass(slots=True)
class SafeConfig:
    llm_model: str = ""
    embed_model: str = ""
    timezone_name: str = "UTC"
    session_window: int = 12

    @property
    def tz(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        from datetime import timezone
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.utc


class SkillKernel:
    """Фасад ядра для навыка. Всё, чего здесь нет, навыку недоступно."""

    __slots__ = ("memory", "llm", "config", "skill")

    def __init__(self, kernel, skill: str) -> None:
        self.skill = skill
        self.memory = ScopedMemory(kernel.memory, skill)
        self.llm = ReadOnlyLLM(kernel.llm)
        self.config = SafeConfig(**{
            field: getattr(kernel.config, field, None) for field in SAFE_CONFIG_FIELDS
        })

    def __getattr__(self, name: str):
        raise AttributeError(
            f"Навык «{self.skill}» обратился к kernel.{name} — этого ему не положено. "
            f"Доступны: memory, llm, config."
        )
