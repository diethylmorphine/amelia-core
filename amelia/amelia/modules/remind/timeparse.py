"""Разбор времени по-русски, без внешних зависимостей.

Понимает: «через 20 минут», «завтра в 9», «в 18:30», «каждый день в 8»,
«каждые 2 часа», «в понедельник в 10».
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

UNITS = {
    "секунд": 1, "сек": 1,
    "минут": 60, "мин": 60,
    "час": 3600, "часа": 3600, "часов": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400,
    "недел": 604800,
}
WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресень": 6,
}

# Пять лет вперёд — потолок здравого смысла. Больше — переполнение timedelta.
MAX_SECONDS = 5 * 365 * 86400
MIN_EVERY = 60          # чаще раза в минуту напоминать бессмысленно

RE_IN = re.compile(r"через\s+(\d+)?\s*([а-яё]+)", re.I)
RE_EVERY_UNIT = re.compile(r"кажд(?:ый|ые|ую)\s+(\d+)?\s*([а-яё]+)", re.I)
RE_AT = re.compile(r"\bв\s+(\d{1,2})(?:[:.](\d{2}))?\b", re.I)
RE_TOMORROW = re.compile(r"\bзавтра\b", re.I)
RE_AFTER_TOMORROW = re.compile(r"\bпослезавтра\b", re.I)


def _amount(raw: str | None) -> int:
    """Число из текста. Бесконечно большие сроки отсекаем до разбора:
    timedelta на них падает с OverflowError."""
    if not raw:
        return 1
    try:
        return min(int(raw), MAX_SECONDS)
    except (TypeError, ValueError):
        return 1


def _unit_seconds(word: str) -> int | None:
    word = word.lower()
    for stem, seconds in UNITS.items():
        if word.startswith(stem):
            return seconds
    return None


def parse(text: str, *, now: datetime | None = None, tz: tzinfo | None = None) -> tuple[datetime | None, int | None, str]:
    """Возвращает (когда в UTC, период в секундах или None, очищенный текст).

    tz — реальный часовой пояс, а не число. Фиксированное смещение ломается
    дважды в год: 25 октября Варшава уходит с +2 на +1, и все «завтра в 9»
    уезжают на час. zoneinfo про переходы знает.
    """
    zone = tz or timezone.utc
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    local = now.astimezone(zone)
    rest = text
    when: datetime | None = None
    every: int | None = None

    # --- повторяющиеся ---
    match = RE_EVERY_UNIT.search(text)
    if match:
        seconds = _unit_seconds(match.group(2))
        if seconds:
            candidate = _amount(match.group(1)) * seconds
            # «каждые 0 минут» раньше молча превращалось в разовую задачу
            if MIN_EVERY <= candidate <= MAX_SECONDS:
                every = candidate
                rest = rest[: match.start()] + rest[match.end():]

    # --- через N единиц ---
    match = RE_IN.search(rest)
    if match:
        seconds = _unit_seconds(match.group(2))
        if seconds:
            delay = _amount(match.group(1)) * seconds
            if 0 < delay <= MAX_SECONDS:
                when = now + timedelta(seconds=delay)
                rest = rest[: match.start()] + rest[match.end():]

    # --- день + время суток ---
    if when is None:
        day = local
        shifted = False
        if RE_AFTER_TOMORROW.search(rest):
            day, shifted = local + timedelta(days=2), True
            rest = RE_AFTER_TOMORROW.sub("", rest, count=1)
        elif RE_TOMORROW.search(rest):
            day, shifted = local + timedelta(days=1), True
            rest = RE_TOMORROW.sub("", rest, count=1)
        else:
            for name, index in WEEKDAYS.items():
                if re.search(rf"\bв?о?\s*{name}", rest, re.I):
                    ahead = (index - local.weekday()) % 7 or 7
                    day, shifted = local + timedelta(days=ahead), True
                    rest = re.sub(rf"\bв?о?\s*{name}\w*", "", rest, count=1, flags=re.I)
                    break

        match = RE_AT.search(rest)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= local and not shifted:
                    candidate += timedelta(days=1)
                when = candidate
                rest = rest[: match.start()] + rest[match.end():]
        elif shifted:
            when = day.replace(hour=9, minute=0, second=0, microsecond=0)

    if when is None and every:
        when = now + timedelta(seconds=every)

    cleaned = re.sub(r"\s{2,}", " ", rest).strip(" ,:-—«»\"")
    return (when.astimezone(timezone.utc) if when else None), every, cleaned


def humanize(when: datetime, tz: tzinfo | None = None) -> str:
    return when.astimezone(tz or timezone.utc).strftime("%d.%m в %H:%M")


PERIODS = (
    ("неделю", "недели", "недель", 604800, "каждую"),
    ("день", "дня", "дней", 86400, "каждый"),
    ("час", "часа", "часов", 3600, "каждый"),
    ("минуту", "минуты", "минут", 60, "каждую"),
)


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def humanize_every(seconds: int) -> str:
    for one, few, many, size, article in PERIODS:
        if seconds % size == 0:
            count = seconds // size
            if count == 1:
                return f"{article} {one}"
            return f"каждые {count} {_plural(count, one, few, many)}"
    return f"каждые {seconds} с"
