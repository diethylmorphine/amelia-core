"""Ядро. Единая точка входа: клиент отдаёт Request — получает Response."""

from __future__ import annotations

import logging

from ..config import Config
from . import lang
from .agent import Agent
from .bus import EventBus
from .context import Request, Response
from .forge import Forge
from .forge.sandbox import SkillKernel
from .health import Health
from .llm.ollama import OllamaProvider
from .memory.store import MemoryStore
from .registry import ModuleRegistry
from .router import Router
from .scheduler import Scheduler
from .session import SessionStore, session_key
from .trace import Tracer

log = logging.getLogger(__name__)


class Kernel:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.bus = EventBus()
        self.llm = OllamaProvider(
            config.ollama_url, config.llm_model, config.embed_model,
            probe_ttl=config.llm_probe_ttl,
            options=config.sampling,
            strip_foreign=config.strip_foreign_scripts,
        )
        self.memory = MemoryStore(config.db_path, self.llm)
        self.sessions = SessionStore(
            self.memory, self.llm, config.session_window, config.session_compact_after
        )
        self.modules = ModuleRegistry()
        self.router = Router(self.modules, self.llm)
        self.agent = Agent(self)
        self.scheduler = Scheduler(self, config.scheduler_tick)
        self.forge = Forge(self)
        self.tracer = Tracer(config.db_path.parent / "trace.jsonl", config.trace_enabled)
        self.health = Health(self, config.health_host, config.health_port)
        self._started = False

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        await self.memory.init()
        self.tracer.attach(self.bus)
        self.modules.discover()
        await self._setup_modules()
        if self.config.scheduler_enabled:
            await self.scheduler.start()
        self._started = True
        online = "онлайн" if await self.llm.available() else "офлайн (только правила)"
        log.info("Ядро запущено: модулей %d, модель %s", len(self.modules), online)
        await self.health.start()
        await self.bus.emit("kernel.started", self)

    async def reload_modules(self) -> None:
        """Горячая перезагрузка реестра — после того как Кузница изменила навыки."""
        for module in self.modules:
            await module.teardown()
        self.modules.discover(reload=True)
        await self._setup_modules()
        await self.bus.emit("modules.reloaded", self.modules.catalog())
        log.info("Реестр перезагружен: модулей %d", len(self.modules))

    # ------------------------------------------------------------------ #
    async def _setup_modules(self) -> None:
        """Инициализация модулей.

        Один сломанный навык не должен превращать ассистента в кирпич:
        упавший на setup исключается из реестра, остальные работают.
        """
        broken = []
        for module in self.modules:
            try:
                await module.setup(self._kernel_for(module))
            except Exception:
                log.exception("Модуль %s упал на setup — исключаю его", module.name)
                broken.append(module)
        for module in broken:
            self.modules.drop(module)

    def _kernel_for(self, module):
        """Встроенным модулям — всё ядро, сгенерированным навыкам — фасад."""
        if self.modules.is_generated(module):
            return SkillKernel(self, module.name)
        return self

    async def ask(self, request: Request) -> Response:
        if not self._started:
            raise RuntimeError("Ядро не запущено: сначала await kernel.start()")

        key = session_key(request.user_id, request.source)
        request.meta.setdefault("session", key)
        # Язык определяется один раз на запрос и дальше не переспрашивается.
        request.meta.setdefault(
            "lang", lang.resolve(self.config.response_language, request.text)
        )
        await self.bus.emit("request", request)
        await self.sessions.append(key, "user", request.text)

        intent = await self.router.resolve(request)
        module = self.modules.pick(request, intent)

        if module is None:
            response = Response(
                text="Пока не умею такое. Напиши «помощь», чтобы увидеть список навыков.",
                intent=intent,
            )
        else:
            try:
                response = await module.handle(request, intent)
            except Exception:
                log.exception("Модуль %s упал на интенте %s", module.name, intent)
                response = Response(text="Модуль споткнулся. Подробности в логах.",
                                    module=module.name, intent=intent)

        if not response.data.get("skip_session"):
            await self.sessions.append(key, "assistant", response.text)
            await self.sessions.maybe_compact(key, request.meta.get("lang"))
        # Замер начат на запросе — переносим на ответ, чтобы трассировка
        # верно сопоставила пару даже при параллельных обращениях.
        if "_started" in request.meta:
            response.data["_started"] = request.meta["_started"]
        await self.bus.emit("response", response)
        response.data.pop("_started", None)
        return response

    async def stop(self) -> None:
        await self.health.stop()
        await self.scheduler.stop()
        for module in self.modules:
            await module.teardown()
        await self.memory.close()
        await self.bus.emit("kernel.stopped", self)
        log.info("Ядро остановлено")
