"""Единые объекты обмена: что клиент отдаёт ядру и что получает обратно."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Request:
    text: str
    user_id: str = "local"
    source: str = "cli"           # какой клиент прислал: cli, telegram, ...
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    # Клиент может подставить приёмник токенов — тогда ответ пойдёт потоком.
    on_token: Callable[[str], Awaitable[None]] | None = None


@dataclass(slots=True)
class Response:
    text: str
    module: str = "core"
    intent: str = ""
    data: dict[str, Any] = field(default_factory=dict)
