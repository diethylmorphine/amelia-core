"""Детерминированный слой: дешёвые и предсказуемые правила до всякой модели.

Правила управления навыками стоят выше остальных намеренно: команды
включения и удаления не должны случайно уехать в модель.
"""

from __future__ import annotations

import re

from .intents import Intent

# Порядок важен: первое совпадение побеждает.
RULES: list[tuple[re.Pattern[str], Intent]] = [
    (re.compile(r"^\s*/?(отмени|удали)\s+(напоминание|напоминалку)\b", re.I), Intent.REMIND_CANCEL),
    (re.compile(r"^\s*/?(напоминания|мои напоминания|что запланировано|расписание)\b", re.I), Intent.REMIND_LIST),
    (re.compile(r"^\s*/?(напомни|напоминай|разбуди)\b", re.I), Intent.REMIND),
    (re.compile(r"^\s*/?(забудь разговор|очисти контекст|новая тема|начнём заново|сброс)\b", re.I), Intent.FORGET),
    (re.compile(r"^\s*/?(включи|активируй|одобри)\s+навык\b", re.I), Intent.SKILL_ON),
    (re.compile(r"^\s*/?(выключи|отключи)\s+навык\b", re.I), Intent.SKILL_OFF),
    (re.compile(r"^\s*/?(удали|снеси|убери)\s+навык\b", re.I), Intent.SKILL_DROP),
    (re.compile(r"^\s*/?(покажи|код)\s+навык[аи]?\b", re.I), Intent.SKILL_SHOW),
    (re.compile(r"^\s*/?(навыки|список навыков|мои навыки)\b", re.I), Intent.SKILL_LIST),
    (re.compile(r"^\s*/?(научись|добавь навык|создай навык|новый навык|напиши навык|сделай навык)\b", re.I), Intent.SKILL_NEW),
    (re.compile(r"^\s*/?(отмени|откати)\s+(последнее|действие|последнее действие)\b", re.I), Intent.UNDO),
    (re.compile(r"^\s*/?(действия|что ты сделала|журнал действий)\b", re.I), Intent.ACTIONS),
    (re.compile(r"^\s*/?(обслуживание|бэкап|бекап|резервная копия)\b", re.I), Intent.UPKEEP),
    (re.compile(r"^\s*/?(помощь|help|хелп|команды)\b", re.I), Intent.HELP),
    (re.compile(r"^\s*/?(пинг|ping)\b", re.I), Intent.PING),
    (re.compile(r"^\s*/?(статус|status|состояние)\b", re.I), Intent.STATUS),
    (re.compile(r"^\s*/?(запомни|заметка|note|запиши)\b", re.I), Intent.NOTE),
    (re.compile(r"^\s*/?(вспомни|найди|recall|искать|поиск)\b", re.I), Intent.RECALL),
    (re.compile(r"\bчто ты знаешь (о|про)\b", re.I), Intent.RECALL),
]


def match_rules(text: str) -> Intent | None:
    for pattern, intent in RULES:
        if pattern.search(text):
            return intent
    return None


def strip_trigger(text: str) -> str:
    """Убирает командное слово, оставляя полезную нагрузку."""
    for pattern, _ in RULES:
        match = pattern.search(text)
        if match:
            return text[match.end():].strip(" :,-—«»\"") or text.strip()
    return text.strip()
