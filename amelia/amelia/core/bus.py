"""Событийная шина. Модули общаются через неё, а не напрямую друг с другом."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, handler: Callable) -> None:
        self._handlers[event].append(handler)

    async def emit(self, event: str, payload: Any = None) -> None:
        for handler in self._handlers.get(event, []):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("Обработчик события %r упал", event)

    async def gather(self, event: str, payload: Any = None) -> list[Any]:
        """Как emit, но собирает результаты (для запросов вида «кто что знает»)."""
        coros = []
        results: list[Any] = []
        for handler in self._handlers.get(event, []):
            result = handler(payload)
            if inspect.isawaitable(result):
                coros.append(result)
            else:
                results.append(result)
        if coros:
            results.extend(await asyncio.gather(*coros, return_exceptions=True))
        return results
