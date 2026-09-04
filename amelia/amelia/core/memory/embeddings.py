"""Векторы и ранжирование.

Косинус на чистом питоне линеен по числу записей: на 3000 заметок поиск
занимает полсекунды, на 10000 — почти две. Поэтому косинусом ранжируется не
вся база, а короткий список кандидатов, отобранный полнотекстовым индексом.
"""

from __future__ import annotations

import math
from array import array
from datetime import datetime, timezone

# Насколько свежесть двигает выдачу. 0 — не двигает вовсе.
RECENCY_WEIGHT = 0.3
# Через столько дней прибавка за свежесть падает вдвое.
RECENCY_HALF_LIFE = 90.0


def pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    vector = array("f")
    vector.frombytes(blob)
    return list(vector)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def recency(created_at: str, now: datetime | None = None) -> float:
    """1.0 для сегодняшней записи, 0.5 через период полураспада."""
    try:
        stamp = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_days = max((now or datetime.now(timezone.utc)) - stamp, _ZERO).total_seconds() / 86400
    return 1.0 / (1.0 + age_days / RECENCY_HALF_LIFE)


def rank(similarity: float, created_at: str, now: datetime | None = None) -> float:
    """Свежесть — мягкий множитель, а не отдельный критерий.

    Заметка двухлетней давности не должна лежать вровень со вчерашней, но и
    вытеснять точное совпадение свежим мусором тоже нельзя.
    """
    return similarity * (1.0 + RECENCY_WEIGHT * recency(created_at, now))


from datetime import timedelta as _td
_ZERO = _td(0)
