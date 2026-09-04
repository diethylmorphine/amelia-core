"""Планировщик: Амелия существует во времени.

До этого она умела только отвечать. Теперь может действовать сама —
напомнить, повторить, разбудить модуль по расписанию. Задачи лежат в той же
SQLite, поэтому переживают рестарт; сработавшие уходят в шину событий,
а клиенты доставляют их пользователю.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Задачи, которые Амелия ставит себе сама: в «напоминаниях» им не место.
INTERNAL_KINDS = ("maintenance",)


@dataclass(slots=True)
class Job:
    id: int
    user_id: str
    source: str
    kind: str
    payload: str
    run_at: str
    every: int | None
    status: str
    meta: dict

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.run_at)


class Scheduler:
    def __init__(self, kernel, tick: float = 5.0) -> None:
        self.kernel = kernel
        self.tick = tick
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def db(self):
        return self.kernel.memory.db

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info("Планировщик запущен, тик %.0f с", self.tick)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                for job in await self.due():
                    await self._fire(job)
            except Exception:
                log.exception("Тик планировщика упал")
            await asyncio.sleep(self.tick)

    # ------------------------------------------------------------------ #
    async def schedule(
        self, payload: str, run_at: datetime, *, user_id: str = "local",
        source: str = "cli", kind: str = "remind", every: int | None = None, meta: dict | None = None,
    ) -> int:
        async with self._lock:
            cursor = self.db.execute(
                "INSERT INTO jobs (user_id, source, kind, payload, run_at, every, meta, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, source, kind, payload,
                 run_at.astimezone(timezone.utc).isoformat(timespec="seconds"), every,
                 json.dumps(meta or {}, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self.db.commit()
            return int(cursor.lastrowid)

    async def ensure_recurring(
        self, payload: str, run_at: datetime, *, kind: str, every: int, user_id: str = "local",
        source: str = "service",
    ) -> int | None:
        """Создать повторяющуюся задачу, если такой ещё нет.

        Без этого каждый рестарт сервиса добавлял бы ещё один ежедневный бэкап.
        """
        row = self.db.execute(
            "SELECT id FROM jobs WHERE kind = ? AND status = 'pending' LIMIT 1", (kind,)
        ).fetchone()
        if row:
            return None
        return await self.schedule(
            payload, run_at, user_id=user_id, source=source, kind=kind, every=every
        )

    async def due(self, now: datetime | None = None) -> list[Job]:
        stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status = 'pending' AND run_at <= ? ORDER BY run_at", (stamp,)
        ).fetchall()
        return [self._row(row) for row in rows]

    async def pending(
        self, user_id: str | None = None, limit: int = 20, include_internal: bool = False
    ) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE status = 'pending'"
        params: list = []
        if not include_internal:
            sql += f" AND kind NOT IN ({','.join('?' * len(INTERNAL_KINDS))})"
            params.extend(INTERNAL_KINDS)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY run_at LIMIT ?"
        params.append(limit)
        return [self._row(row) for row in self.db.execute(sql, params)]

    async def cancel(self, job_id: int, user_id: str | None = None) -> bool:
        sql = "UPDATE jobs SET status = 'cancelled' WHERE id = ? AND status = 'pending'"
        params: list = [job_id]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        async with self._lock:
            cursor = self.db.execute(sql, params)
            self.db.commit()
            return cursor.rowcount > 0

    async def _fire(self, job: Job) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            if job.every and job.every > 0:
                # Повторяющаяся: догоняем арифметикой, а не циклом. При секундном
                # периоде и неделе простоя цикл сделал бы 600 тысяч итераций.
                behind = (now - job.when).total_seconds()
                skipped = int(behind // job.every) + 1
                nxt = job.when + timedelta(seconds=job.every * skipped)
                if skipped > 1:
                    log.info("Задача #%d пропустила %d срабатываний — беру ближайшее",
                             job.id, skipped - 1)
                self.db.execute(
                    "UPDATE jobs SET run_at = ?, fired_at = ? WHERE id = ?",
                    (nxt.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), job.id),
                )
            else:
                self.db.execute(
                    "UPDATE jobs SET status = 'done', fired_at = ? WHERE id = ?",
                    (now.isoformat(timespec="seconds"), job.id),
                )
            self.db.commit()
        log.info("Задача #%d сработала", job.id)
        await self.kernel.bus.emit("job.fired", job)

    @staticmethod
    def _row(row) -> Job:
        try:
            meta = json.loads(row["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return Job(
            id=row["id"], user_id=row["user_id"], source=row["source"], kind=row["kind"],
            payload=row["payload"], run_at=row["run_at"], every=row["every"],
            status=row["status"], meta=meta,
        )

    async def count_pending(self, include_internal: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
        params: tuple = ()
        if not include_internal:
            sql += f" AND kind NOT IN ({','.join('?' * len(INTERNAL_KINDS))})"
            params = INTERNAL_KINDS
        return int(self.db.execute(sql, params).fetchone()[0])
