"""Диалоговый контекст.

До этого каждый запрос был独立ным: спросить «а сколько это в евро?» было
невозможно — Амелия не знала, о чём «это». Сессия хранит историю реплик,
переживает рестарт процесса и сжимает старое в выжимку, когда окно растёт.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SUMMARY_SYSTEM = (
    "Сожми диалог в короткую выжимку: только факты, решения и открытые вопросы, "
    "которые понадобятся дальше. Без вступлений, 3-5 строк."
)


@dataclass(slots=True)
class Turn:
    role: str
    content: str
    created_at: str = ""


def session_key(user_id: str, source: str) -> str:
    return f"{source}:{user_id}"


class SessionStore:
    """История реплик в той же SQLite, что и память."""

    def __init__(self, memory, llm, window: int = 12, summarize_after: int = 24) -> None:
        self.memory = memory
        self.llm = llm
        self.window = window
        self.summarize_after = summarize_after
        self._lock = asyncio.Lock()

    @property
    def db(self):
        return self.memory.db

    async def append(self, key: str, role: str, content: str) -> None:
        if not content.strip():
            return
        async with self._lock:
            self.db.execute(
                "INSERT INTO turns (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (key, role, content[:8000], datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self.db.commit()

    async def history(self, key: str) -> list[Turn]:
        """Последние реплики окна, в хронологическом порядке."""
        rows = self.db.execute(
            "SELECT role, content, created_at FROM turns WHERE session_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (key, self.window),
        ).fetchall()
        return [Turn(row["role"], row["content"], row["created_at"]) for row in reversed(rows)]

    async def summary(self, key: str) -> str:
        row = self.db.execute(
            "SELECT summary FROM session_summaries WHERE session_id = ?", (key,)
        ).fetchone()
        return row["summary"] if row else ""

    async def as_prompt_block(self, key: str) -> str:
        """Готовый текстовый блок контекста для промпта."""
        parts: list[str] = []
        summary = await self.summary(key)
        if summary:
            parts.append(f"Ранее в разговоре:\n{summary}")
        turns = await self.history(key)
        if turns:
            lines = "\n".join(
                f"{'Пользователь' if turn.role == 'user' else 'Амелия'}: {turn.content}"
                for turn in turns
            )
            parts.append(f"Последние реплики:\n{lines}")
        return "\n\n".join(parts)

    async def maybe_compact(self, key: str, language: str | None = None) -> bool:
        """Сжать хвост в выжимку, если реплик накопилось слишком много."""
        total = self.db.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?", (key,)
        ).fetchone()[0]
        if total < self.summarize_after or not await self.llm.available():
            return False

        cutoff = total - self.window
        rows = self.db.execute(
            "SELECT id, role, content FROM turns WHERE session_id = ? ORDER BY id LIMIT ?",
            (key, cutoff),
        ).fetchall()
        if not rows:
            return False

        previous = await self.summary(key)
        dialogue = "\n".join(
            f"{'Пользователь' if row['role'] == 'user' else 'Амелия'}: {row['content']}"
            for row in rows
        )
        prompt = (f"Предыдущая выжимка:\n{previous}\n\n" if previous else "") + f"Новые реплики:\n{dialogue}"
        compact = await self.llm.complete(prompt, system=SUMMARY_SYSTEM, language=language)
        if not compact.strip():
            return False

        async with self._lock:
            self.db.execute(
                "INSERT INTO session_summaries (session_id, summary, upto_turn, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary,"
                " upto_turn=excluded.upto_turn, updated_at=excluded.updated_at",
                (key, compact.strip(), rows[-1]["id"],
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self.db.execute(
                "DELETE FROM turns WHERE session_id = ? AND id <= ?", (key, rows[-1]["id"])
            )
            self.db.commit()
        log.info("Сессия %s сжата: %d реплик в выжимку", key, len(rows))
        return True

    async def clear(self, key: str) -> int:
        async with self._lock:
            cursor = self.db.execute("DELETE FROM turns WHERE session_id = ?", (key,))
            self.db.execute("DELETE FROM session_summaries WHERE session_id = ?", (key,))
            self.db.commit()
            return cursor.rowcount

    async def stats(self) -> tuple[int, int]:
        turns = self.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        sessions = self.db.execute("SELECT COUNT(DISTINCT session_id) FROM turns").fetchone()[0]
        return int(sessions), int(turns)
