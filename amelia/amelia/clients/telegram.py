"""Телеграм-клиент на aiogram. Зависимость импортируется лениво.

Установка: pip install -e ".[telegram]"
"""

from __future__ import annotations

import asyncio
import logging

from ..core.context import Request
from .base import Client

log = logging.getLogger(__name__)

# Как часто дописывать сообщение при потоковой генерации.
STREAM_EDIT_INTERVAL = 1.0


class TelegramClient(Client):
    name = "telegram"

    async def run(self) -> None:
        token = self.kernel.config.bot_token
        if not token:
            raise SystemExit("Не задан AMELIA_BOT_TOKEN в .env")

        owner = str(self.kernel.config.owner_id or "")
        if not owner:
            raise SystemExit("Не задан AMELIA_OWNER_ID — Telegram-клиент закрыт для всех")

        try:
            from aiogram import Bot, Dispatcher
            from aiogram.types import Message
        except ImportError as exc:
            raise SystemExit('Нужен aiogram: pip install -e ".[telegram]"') from exc

        bot = Bot(token)
        dispatcher = Dispatcher()

        @dispatcher.message()
        async def on_message(message: Message) -> None:
            user_id = str(message.from_user.id)
            if owner and user_id != owner:
                return  # приватный ассистент: чужих не слушаем

            # Поток: одно сообщение, которое дописывается по мере генерации.
            # Telegram ограничивает частоту правок, поэтому раз в секунду.
            state = {"sent": None, "buffer": "", "at": 0.0}

            async def on_token(piece: str) -> None:
                state["buffer"] += piece
                now = asyncio.get_running_loop().time()
                if now - state["at"] < STREAM_EDIT_INTERVAL:
                    return
                state["at"] = now
                try:
                    if state["sent"] is None:
                        state["sent"] = await message.answer(state["buffer"] + " ▍")
                    else:
                        await state["sent"].edit_text(state["buffer"] + " ▍")
                except Exception:
                    pass  # правка не прошла — не роняем генерацию

            response = await self.kernel.ask(
                Request(
                    text=message.text or "",
                    user_id=user_id,
                    source=self.name,
                    meta={"chat_id": message.chat.id},
                    on_token=on_token,
                )
            )
            if state["sent"] is not None:
                try:
                    await state["sent"].edit_text(response.text)
                    return
                except Exception:
                    return  # текст уже совпадает с последней правкой
            await message.answer(response.text)

        async def deliver(job) -> None:
            """Планировщик разбудил задачу — доставляем её в чат."""
            if job.source != self.name:
                return
            chat_id = job.meta.get("chat_id") or job.user_id
            try:
                await bot.send_message(chat_id, f"\u23f0 {job.payload}")
            except Exception:
                log.exception("Не удалось доставить задачу #%s", job.id)

        self.kernel.bus.on("job.fired", deliver)
        log.info("Телеграм-клиент запущен")
        try:
            await dispatcher.start_polling(bot)
        finally:
            await bot.session.close()
