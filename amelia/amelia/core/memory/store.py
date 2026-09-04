"""Память ассистента: SQLite, приватность по владельцу, гибридный поиск.

Три вещи, которые здесь важнее всего:
  1. Записи приватны по умолчанию — чужое в выдачу не попадает.
  2. Поиск гибридный: FTS5 отбирает кандидатов, косинус их ранжирует.
  3. Повторное «запомни то же самое» обновляет запись, а не плодит копии.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .embeddings import cosine, pack, rank, unpack

log = logging.getLogger(__name__)
SCHEMA = Path(__file__).with_name("schema.sql")

PRIVATE = "private"
SHARED = "shared"

# Явный маркер намеренно неограниченного поиска. Раньше ту же роль играл
# user_id=None по умолчанию — и именно поэтому утечка между пользователями
# случилась молча: забыть аргумент было проще, чем передать.
ALL_USERS = "*"

# Сколько кандидатов отбирает полнотекстовый индекс перед ранжированием.
CANDIDATES = 60
# Похожесть, выше которой запись считается той же самой. Порог высокий
# намеренно: слияние молча выбрасывает новый текст, а «встреча в 10» и
# «встреча в 11» семантически почти неразличимы.
DEDUP_THRESHOLD = 0.97
# И даже при высокой похожести тексты должны быть сопоставимой длины —
# защита от вырожденной модели эмбеддингов, выдающей всем один вектор.
DEDUP_LENGTH_RATIO = 0.8


@dataclass(slots=True)
class Memory:
    id: int
    text: str
    source: str
    user_id: str
    tags: list[str]
    created_at: str
    scope: str = PRIVATE
    score: float = 0.0


def _tokens(text: str) -> list[str]:
    return [word for word in re.findall(r"\w+", text, re.UNICODE) if len(word) > 1]


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


class MemoryStore:
    def __init__(self, db_path: Path, llm) -> None:
        self.db_path = Path(db_path)
        self.llm = llm
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
        self._db.row_factory = sqlite3.Row
        # Режимы под процесс, который живёт неделями: WAL переживает внезапное
        # выключение питания, busy_timeout не даёт упасть на конкурентной записи.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=15000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA.read_text(encoding="utf-8"))
        self._migrate()
        self._db.commit()
        log.info("Память подключена: %s (WAL)", self.db_path)

    def _migrate(self) -> None:
        """Досоздать колонки, появившиеся позже базы."""
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(memories)")}
        if "scope" not in existing:
            self.db.execute(f"ALTER TABLE memories ADD COLUMN scope TEXT NOT NULL DEFAULT '{PRIVATE}'")
            log.info("Миграция: добавлена колонка scope")

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Память не инициализирована")
        return self._db

    # ------------------------------------------------------------------ #
    async def remember(
        self, text: str, *, source: str = "unknown", user_id: str = "local",
        tags: list[str] | None = None, scope: str = PRIVATE,
    ) -> int:
        """Сохранить запись. Повтор того же самого обновляет существующую."""
        embedding = await self.llm.embed(text) if await self.llm.available() else []

        duplicate = await asyncio.to_thread(self._find_duplicate, text, user_id, embedding)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with self._lock:
            if duplicate is not None:
                self.db.execute(
                    "UPDATE memories SET created_at = ?, source = ? WHERE id = ?",
                    (now, source, duplicate),
                )
                self.db.commit()
                log.debug("Дубль записи #%d — обновил дату вместо вставки", duplicate)
                return duplicate

            cursor = self.db.execute(
                "INSERT INTO memories (text, source, user_id, tags, embedding, created_at, scope)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (text, source, user_id, json.dumps(tags or [], ensure_ascii=False),
                 pack(embedding) if embedding else None, now, scope),
            )
            self.db.commit()
            return int(cursor.lastrowid)

    def _find_duplicate(self, text: str, user_id: str, embedding: list[float]) -> int | None:
        row = self.db.execute(
            "SELECT id FROM memories WHERE user_id = ? AND lower(trim(text)) = ? LIMIT 1",
            (user_id, _normalize(text)),
        ).fetchone()
        if row:
            return int(row["id"])
        if not embedding:
            return None
        length = len(text.strip())
        for candidate in self._candidates(text, user_id, include_shared=False, limit=20):
            other = len(candidate["text"].strip())
            shorter, longer = sorted((length, other))
            if not longer or shorter / longer < DEDUP_LENGTH_RATIO:
                continue
            if cosine(embedding, unpack(candidate["embedding"])) >= DEDUP_THRESHOLD:
                return int(candidate["id"])
        return None

    async def forget(self, memory_id: int, user_id: str | None = None) -> bool:
        sql = "DELETE FROM memories WHERE id = ?"
        params: list = [memory_id]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        async with self._lock:
            cursor = self.db.execute(sql, params)
            self.db.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    async def recall(
        self, query: str, user_id: str, limit: int = 5, include_shared: bool = True
    ) -> list[Memory]:
        """Гибридный поиск: FTS5 отбирает, косинус ранжирует.

        user_id — обязательный позиционный аргумент, не забываемый по умолчанию.
        Для намеренно общего поиска передаётся ALL_USERS.
        """
        query_vector = await self.llm.embed(query) if await self.llm.available() else []
        return await asyncio.to_thread(
            self._recall_sync, query, query_vector, limit, user_id, include_shared
        )

    def _recall_sync(self, query, query_vector, limit, user_id, include_shared) -> list[Memory]:
        rows = self._candidates(query, user_id, include_shared, CANDIDATES)
        if not rows:
            return []

        if query_vector:
            scored = []
            for row in rows:
                similarity = cosine(query_vector, unpack(row["embedding"]))
                if similarity > 0:
                    scored.append(self._to_memory(row, rank(similarity, row["created_at"])))
            scored.sort(key=lambda memory: memory.score, reverse=True)
            return scored[:limit]

        # Без модели порядок задаёт FTS5, свежесть лишь подталкивает.
        ranked = [self._to_memory(row, rank(1.0, row["created_at"])) for row in rows]
        ranked.sort(key=lambda memory: memory.score, reverse=True)
        return ranked[:limit]

    def _candidates(self, query: str, user_id: str | None, include_shared: bool, limit: int):
        """Короткий список кандидатов вместо скана всей таблицы."""
        scope_sql, scope_params = self._scope_clause(user_id, include_shared)
        terms = " OR ".join(_tokens(query))

        if terms:
            sql = (
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid"
                f" WHERE memories_fts MATCH ?{scope_sql} ORDER BY rank LIMIT ?"
            )
            try:
                rows = self.db.execute(sql, [terms, *scope_params, limit]).fetchall()
                if rows:
                    return rows
            except sqlite3.OperationalError:
                log.debug("FTS-запрос не выполнен, откатываюсь на свежие записи")

        # Ничего не нашлось лексически — берём свежие, чтобы семантика имела шанс.
        sql = f"SELECT * FROM memories m WHERE 1=1{scope_sql} ORDER BY id DESC LIMIT ?"
        return self.db.execute(sql, [*scope_params, limit]).fetchall()

    @staticmethod
    def _scope_clause(user_id: str | None, include_shared: bool) -> tuple[str, list]:
        if user_id is None or user_id == ALL_USERS:
            return "", []
        if include_shared:
            return " AND (m.user_id = ? OR m.scope = ?)", [user_id, SHARED]
        return " AND m.user_id = ?", [user_id]

    @staticmethod
    def _to_memory(row: sqlite3.Row, score: float = 0.0) -> Memory:
        keys = row.keys()
        return Memory(
            id=row["id"], text=row["text"], source=row["source"], user_id=row["user_id"],
            tags=json.loads(row["tags"] or "[]"), created_at=row["created_at"],
            scope=row["scope"] if "scope" in keys else PRIVATE, score=score,
        )

    # ------------------------------------------------------------------ #
    async def count(self, user_id: str | None = None) -> int:
        if user_id and user_id != ALL_USERS:
            return int(self.db.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,)
            ).fetchone()[0])
        return int(self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    async def backup(self, target: Path) -> Path:
        """Онлайн-копия через нативный SQLite backup: не блокирует запись."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        def _run() -> None:
            with sqlite3.connect(target) as copy:
                self.db.backup(copy)
        await asyncio.to_thread(_run)
        return target

    async def maintain(self, vacuum: bool = False) -> str:
        """Подрезать WAL и при необходимости сжать базу."""
        def _run() -> str:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if vacuum:
                self.db.execute("VACUUM")
            self.db.execute("PRAGMA optimize")
            self.db.commit()
            return f"{self.db_path.stat().st_size / 1024 / 1024:.1f} МБ"
        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        if self._db is not None:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._db.close()
            self._db = None
