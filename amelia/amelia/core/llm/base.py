"""Контракт провайдера модели. Меняется на любой другой без правок ядра."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    async def available(self) -> bool:
        """Живой ли бэкенд прямо сейчас."""
        ...

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        ...

    async def embed(self, text: str) -> list[float]:
        ...
