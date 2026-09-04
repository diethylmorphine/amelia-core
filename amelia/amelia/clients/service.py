"""Безголовый режим для systemd.

CLI требует stdin, которого под systemd нет. Этот клиент ничего не читает:
поднимает Telegram, если задан токен, и просто живёт — планировщик,
обслуживание и проверка здоровья работают сами.
"""

from __future__ import annotations

import asyncio
import logging

from .base import Client

log = logging.getLogger(__name__)


class ServiceClient(Client):
    name = "service"

    async def run(self) -> None:
        config = self.kernel.config
        tasks: list[asyncio.Task] = []

        if config.bot_token:
            from .telegram import TelegramClient
            telegram = TelegramClient(self.kernel)
            tasks.append(asyncio.create_task(self._guard(telegram), name="telegram"))
        else:
            log.warning("AMELIA_BOT_TOKEN не задан — работаю без Telegram: "
                        "планировщик, обслуживание и здоровье")

        log.info("Служба запущена. Остановка — SIGTERM.")
        try:
            await asyncio.Event().wait()          # живём, пока не остановят
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _guard(self, client: Client) -> None:
        """Клиент упал — не роняем службу, ждём и поднимаем заново."""
        delay = 5.0
        while True:
            try:
                await client.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Клиент %s упал, перезапуск через %.0f с", client.name, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300.0)
