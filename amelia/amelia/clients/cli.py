"""Консольный клиент. Нужен только stdlib — удобно для отладки ядра."""

from __future__ import annotations

import asyncio

from ..core.context import Request
from .base import Client

BANNER = "АМЕЛИЯ · автономное модульное единое логико-интеллектуальное ядро\nПиши «помощь». Выход — «выход» или Ctrl+C.\n"


class CLIClient(Client):
    name = "cli"

    async def run(self) -> None:
        print(BANNER)
        self.kernel.bus.on("job.fired", self._deliver)
        while True:
            try:
                text = await asyncio.to_thread(input, "› ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            text = text.strip()
            if not text:
                continue
            if text.lower() in {"выход", "exit", "quit", ":q"}:
                return
            printed = False

            async def on_token(piece: str) -> None:
                nonlocal printed
                printed = True
                print(piece, end="", flush=True)

            response = await self.kernel.ask(
                Request(text=text, user_id="local", source=self.name, on_token=on_token)
            )
            # Модули без потока (память, напоминания) печатают ответ целиком.
            print("\n" if printed else f"{response.text}\n")

    def _deliver(self, job) -> None:
        """Сработавшее напоминание печатаем прямо в консоль."""
        if job.source == self.name or job.source == "cli":
            print(f"\n\u23f0 {job.payload}\n\u203a ", end="", flush=True)
