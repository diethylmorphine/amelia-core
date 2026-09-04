"""Провайдер Ollama на голом stdlib — чтобы ядро не тянуло зависимости."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections import Counter

from .. import lang

log = logging.getLogger(__name__)


async def _noop(_: str) -> None:
    """Заглушка, когда обработчик потока сломался."""


class OllamaProvider:
    def __init__(self, base_url: str, model: str, embed_model: str, timeout: float = 120.0,
                 probe_ttl: float = 30.0, options: dict | None = None,
                 strip_foreign: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.timeout = timeout
        self.probe_ttl = probe_ttl
        # Параметры сэмплирования. Без них Ollama берёт свои дефолты
        # (temperature 0.8), а для маленького Qwen это и есть главная
        # причина иероглифов посреди русской фразы.
        self.options = options or {}
        self.strip_foreign = strip_foreign
        self.stats: Counter[str] = Counter()
        self._available: bool | None = None
        self._checked_at: float = 0.0

    # --- транспорт -------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    async def _apost(self, path: str, payload: dict) -> dict:
        return await asyncio.to_thread(self._post, path, payload)

    # --- API -------------------------------------------------------------
    async def available(self, recheck: bool = False) -> bool:
        """Доступность с коротким TTL.

        Раньше флаг залипал навсегда: одна сетевая икота — и Амелия до конца
        своих дней считала модель мёртвой. Для процесса, который живёт неделями,
        это фатально, поэтому проверка протухает и повторяется сама.
        """
        age = time.monotonic() - self._checked_at
        if self._available is not None and not recheck and age < self.probe_ttl:
            return self._available

        was = self._available
        try:
            await asyncio.to_thread(
                urllib.request.urlopen, f"{self.base_url}/api/tags", None, 3.0
            )
            self._available = True
        except Exception as exc:
            log.debug("Ollama недоступна: %s", exc)
            self._available = False
        self._checked_at = time.monotonic()

        if was is not None and was != self._available:
            log.info("Модель %s", "вернулась" if self._available else "пропала")
        return self._available

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        """Ответ модели. С on_token — потоком, иначе одним куском.

        language прибивает язык ответа: директива уходит в системный промпт,
        а результат проверяется на иероглифы. Нашлись — одна перегенерация
        с более жёсткой формулировкой, и лишь потом вырезание.
        """
        if on_token is not None:
            return await self._stream_guarded(prompt, system, on_token, language)

        answer = await self._once(prompt, lang.pin(system, language) if language else system)
        if not language or not lang.has_foreign(answer):
            return lang.strip_thinking(answer)

        # Вырезание ломает смысл: 因为 — это «потому что», без него фраза
        # разваливается. Поэтому сначала честная попытка переписать.
        self.stats["foreign_detected"] += 1
        log.info("Модель подмешала иероглифы (%d симв.) — перегенерирую",
                 lang.foreign_count(answer))
        retry = await self._once(prompt, lang.pin(system, language, insist=True))

        if retry and not lang.has_foreign(retry):
            self.stats["foreign_fixed_by_retry"] += 1
            return lang.strip_thinking(retry)

        self.stats["foreign_stripped"] += 1
        best = retry if retry and lang.foreign_count(retry) < lang.foreign_count(answer) else answer
        return lang.clean(best, strip=self.strip_foreign)

    async def _once(self, prompt: str, system: str | None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        try:
            data = await self._apost(
                "/api/chat",
                {"model": self.model, "messages": messages, "stream": False,
                 "options": self.options},
            )
            return data.get("message", {}).get("content", "").strip()
        except Exception:
            log.exception("Ошибка генерации")
            self._checked_at = 0.0     # следующая проверка сходит на сервер по-настоящему
            return ""

    async def _stream_guarded(self, prompt, system, on_token, language) -> str:
        """Поток чистится на лету.

        Перегенерировать нечего: отправленное уже у пользователя на экране.
        Поэтому здесь только фильтр — иероглифы вырезаются из каждого куска,
        не доходя до клиента.
        """
        messages = ([{"role": "system", "content": lang.pin(system, language) if language else system}]
                    if (system or language) else []) + [{"role": "user", "content": prompt}]

        dirty = 0

        async def guarded(piece: str) -> None:
            nonlocal dirty
            if language and lang.has_foreign(piece):
                dirty += lang.foreign_count(piece)
                piece = lang.strip_foreign(piece)
                if not piece:
                    return
            await on_token(piece)

        raw = await self._stream(messages, guarded)
        if dirty:
            self.stats["foreign_detected"] += 1
            self.stats["foreign_stripped"] += 1
            log.info("Поток содержал %d иероглифов — вырезаны на лету", dirty)
        return lang.clean(raw, strip=self.strip_foreign and bool(language))

    async def _stream(self, messages: list[dict], on_token) -> str:
        """Читаем NDJSON в отдельном потоке, куски отдаём в цикл событий."""
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def reader() -> None:
            try:
                request = urllib.request.Request(
                    f"{self.base_url}/api/chat",
                    data=json.dumps({"model": self.model, "messages": messages,
                                     "stream": True, "options": self.options}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    for line in response:
                        if not line.strip():
                            continue
                        chunk = json.loads(line.decode("utf-8"))
                        piece = chunk.get("message", {}).get("content", "")
                        if piece:
                            loop.call_soon_threadsafe(queue.put_nowait, piece)
                        if chunk.get("done"):
                            break
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(reader))
        parts: list[str] = []
        try:
            while True:
                piece = await queue.get()
                if piece is None:
                    break
                if isinstance(piece, Exception):
                    log.exception("Ошибка потоковой генерации", exc_info=piece)
                    self._checked_at = 0.0
                    break
                parts.append(piece)
                try:
                    await on_token(piece)
                except Exception:
                    log.exception("Обработчик потока упал — досмотрю ответ молча")
                    on_token = _noop
        finally:
            await task
        return "".join(parts).strip()

    async def embed(self, text: str) -> list[float]:
        try:
            data = await self._apost(
                "/api/embeddings", {"model": self.embed_model, "prompt": text}
            )
            return data.get("embedding", [])
        except Exception:
            log.exception("Ошибка эмбеддинга")
            self._checked_at = 0.0
            return []
