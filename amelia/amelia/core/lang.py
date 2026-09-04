"""Единственная точка фиксации языка.

Раньше её не было: три промпта независимо просили «на языке собеседника»,
два молчали, а параметры сэмплирования не задавались вовсе — значит,
работали дефолты Ollama с temperature 0.8. Для трёхмиллиардного Qwen,
у которого в обучении огромная доля китайского, это прямое приглашение
иероглифам посреди русской фразы.

Здесь три уровня защиты:
  1. Явная директива в системном промпте — «отвечай только по-русски».
  2. Сэмплирование потише: низкая температура и min_p вместо неё.
  3. Проверка результата: нашлись иероглифы — перегенерировать, а если и
     это не помогло, вырезать и посчитать инцидент.
"""

from __future__ import annotations

import re

# Иероглифика, кана, хангыль и полноширинные формы. Для русско-английского
# ассистента любой символ отсюда — мусор. Эмодзи (U+1F300+) сюда не попадают.
FOREIGN_SCRIPTS = re.compile(
    "["
    "\u3000-\u303f"      # китайская пунктуация
    "\u3040-\u30ff"      # хирагана и катакана
    "\u3400-\u4dbf"      # иероглифы, расширение A
    "\u4e00-\u9fff"      # основные иероглифы
    "\uf900-\ufaff"      # совместимость
    "\uff01-\uffee"      # полноширинные формы
    "\uac00-\ud7af"      # хангыль
    "]+"
)

# Qwen3 в режиме рассуждений оборачивает мысли в <think>…</think>.
# Пользователю они не нужны, а в память попадать не должны тем более.
THINKING = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
LATIN = re.compile(r"[a-zA-Z]")

LANGUAGE_NAMES = {"ru": "РУССКОМ", "en": "ENGLISH"}

DIRECTIVES = {
    "ru": (
        "ОТВЕЧАЙ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. Не вставляй китайские, японские или "
        "корейские иероглифы — ни одного символа, даже в скобках или как пояснение."
    ),
    "en": (
        "RESPOND ONLY IN ENGLISH. Do not insert Chinese, Japanese or Korean "
        "characters anywhere — not a single one, not even in parentheses."
    ),
}

# Вторая попытка, если модель всё-таки подмешала иероглифы.
INSISTENT = {
    "ru": (
        "КРИТИЧЕСКИ ВАЖНО: предыдущий ответ содержал иероглифы и был отклонён. "
        "Напиши ответ заново, используя ИСКЛЮЧИТЕЛЬНО кириллицу, латиницу, "
        "цифры и обычную пунктуацию. Любой иероглиф недопустим."
    ),
    "en": (
        "CRITICAL: the previous answer contained CJK characters and was rejected. "
        "Rewrite it using ONLY Latin letters, digits and ordinary punctuation."
    ),
}


def detect(text: str, default: str = "ru") -> str:
    """Язык по алфавиту. Грубо, но для пары ru/en надёжнее любой эвристики."""
    cyrillic = len(CYRILLIC.findall(text))
    latin = len(LATIN.findall(text))
    if not cyrillic and not latin:
        return default
    return "ru" if cyrillic >= latin else "en"


def resolve(configured: str, user_text: str) -> str:
    """Настройка «auto» означает: подстроиться под собеседника."""
    if configured and configured != "auto":
        return configured if configured in DIRECTIVES else "ru"
    return detect(user_text)


def pin(system: str | None, language: str, insist: bool = False) -> str:
    """Прибить язык к системному промпту."""
    directive = (INSISTENT if insist else DIRECTIVES).get(language, DIRECTIVES["ru"])
    return f"{system}\n\n{directive}" if system else directive


def has_foreign(text: str) -> bool:
    return bool(FOREIGN_SCRIPTS.search(text))


def foreign_count(text: str) -> int:
    return sum(len(match) for match in FOREIGN_SCRIPTS.findall(text))


def strip_foreign(text: str) -> str:
    """Вырезать иероглифы, не оставив двойных пробелов на их месте."""
    cleaned = FOREIGN_SCRIPTS.sub("", text)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return re.sub(r"\s+([,.!?;:])", r"\1", cleaned)


def strip_thinking(text: str) -> str:
    return THINKING.sub("", text).lstrip()


def clean(text: str, strip: bool = True) -> str:
    """Полная очистка ответа перед выдачей наружу."""
    text = strip_thinking(text)
    return strip_foreign(text) if strip else text
