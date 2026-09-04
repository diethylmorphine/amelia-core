"""Контракт клиента. Клиент — это транспорт, вся логика живёт в ядре."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.kernel import Kernel


class Client(ABC):
    name: str = "client"

    def __init__(self, kernel: "Kernel") -> None:
        self.kernel = kernel

    @abstractmethod
    async def run(self) -> None:
        ...
