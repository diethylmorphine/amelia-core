"""Каталог интентов. Одно место, где живут строковые идентификаторы."""

from __future__ import annotations

from enum import StrEnum


class Intent(StrEnum):
    HELP = "help"
    PING = "ping"
    STATUS = "status"
    NOTE = "note"
    RECALL = "recall"
    # --- время ---
    REMIND = "remind"
    REMIND_LIST = "remind_list"
    REMIND_CANCEL = "remind_cancel"
    # --- сессия ---
    FORGET = "forget"
    UPKEEP = "upkeep"
    ACTIONS = "actions"
    UNDO = "undo"
    # --- самораcширение ---
    SKILL_NEW = "skill_new"
    SKILL_LIST = "skill_list"
    SKILL_SHOW = "skill_show"
    SKILL_ON = "skill_on"
    SKILL_OFF = "skill_off"
    SKILL_DROP = "skill_drop"
    # --- фолбэк ---
    CHAT = "chat"


FALLBACK_INTENT = Intent.CHAT
