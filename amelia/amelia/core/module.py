"""Контракт модуля. Всё, что должен знать навык, чтобы жить в ядре."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from .agent import Tool
from .context import Request, Response

if TYPE_CHECKING:
    from .kernel import Kernel


class Module(ABC):
    name: ClassVar[str] = "unnamed"
    description: ClassVar[str] = ""
    intents: ClassVar[tuple[str, ...]] = ()
    priority: ClassVar[int] = 100          # меньше — раньше в очереди на обработку

    def __init__(self) -> None:
        self.kernel: "Kernel | None" = None
        self._origin: str = ""

    async def setup(self, kernel: "Kernel") -> None:
        """Вызывается один раз при старте ядра."""
        self.kernel = kernel

    async def teardown(self) -> None:
        """Закрыть соединения, снять хуки."""

    def can_handle(self, request: Request, intent: str) -> bool:
        return intent in self.intents

    def tools(self) -> list[Tool]:
        """Инструменты, которые модуль отдаёт агенту. По умолчанию — никаких."""
        return []

    @abstractmethod
    async def handle(self, request: Request, intent: str) -> Response:
        ...

    def reply(self, text: str, intent: str = "", **data) -> Response:
        return Response(text=text, module=self.name, intent=intent, data=data)
