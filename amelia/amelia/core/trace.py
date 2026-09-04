"""Наблюдаемость: что происходило и сколько это стоило.

Подписывается на шину и пишет одну строку JSON на запрос. Без этого отладка
агентных цепочек превращается в гадание — модель вызвала три инструмента,
а какие и с чем, узнать неоткуда.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class Tracer:
    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.counters: Counter[str] = Counter()
        self.started = time.monotonic()

    def attach(self, bus) -> None:
        bus.on("request", self._on_request)
        bus.on("response", self._on_response)
        bus.on("job.fired", lambda job: self._bump("jobs.fired"))
        bus.on("modules.reloaded", lambda catalog: self._bump("modules.reloaded"))

    # ------------------------------------------------------------------ #
    def _on_request(self, request) -> None:
        # Время живёт на самом запросе, а не в словаре по id(): id
        # переиспользуется сборщиком мусора, а параллельные запросы
        # от разных людей перепутали бы замеры между собой.
        request.meta["_started"] = time.monotonic()
        self.counters["requests"] += 1
        self.counters[f"source.{request.source}"] += 1

    def _on_response(self, response) -> None:
        started = response.data.pop("_started", None)
        elapsed = time.monotonic() - started if started else 0.0
        self.counters[f"module.{response.module}"] += 1
        self.counters[f"intent.{response.intent}"] += 1
        self._write({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "module": response.module,
            "intent": response.intent,
            "ms": round(elapsed * 1000),
            "chars": len(response.text),
            "data": {k: v for k, v in response.data.items() if isinstance(v, (int, float, bool, str))},
        })

    def _bump(self, key: str) -> None:
        self.counters[key] += 1

    def _write(self, record: dict) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.debug("Не удалось записать трассировку")

    # ------------------------------------------------------------------ #
    def summary(self, top: int = 4) -> str:
        uptime = time.monotonic() - self.started
        hours, rest = divmod(int(uptime), 3600)
        minutes, seconds = divmod(rest, 60)
        stamp = f"{hours}ч {minutes}м" if hours else (f"{minutes}м {seconds}с" if minutes else f"{seconds}с")
        modules = [(k.split(".", 1)[1], v) for k, v in self.counters.items() if k.startswith("module.")]
        modules.sort(key=lambda item: item[1], reverse=True)
        busiest = ", ".join(f"{name} ×{count}" for name, count in modules[:top]) or "—"
        return f"Запросов: {self.counters['requests']} за {stamp}\nЧаще всего: {busiest}"
