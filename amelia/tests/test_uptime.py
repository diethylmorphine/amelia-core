"""Тесты круглосуточного режима: то, что ломается не сразу, а через недели."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from amelia.config import Config
from amelia.core.kernel import Kernel
from amelia.core.llm.ollama import OllamaProvider
from amelia.modules.remind.timeparse import parse

WARSAW = ZoneInfo("Europe/Warsaw")


class FlakyLLM:
    """Модель, которую можно уронить и поднять — как настоящую."""

    def __init__(self) -> None:
        self.up = True
        self.probes = 0

    async def available(self, recheck: bool = False) -> bool:
        self.probes += 1
        return self.up

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        return "ответ" if self.up else ""

    async def embed(self, text: str) -> list[float]:
        return []


@pytest.fixture
async def kernel(tmp_path):
    config = Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "vault",
                    scheduler_tick=0.1, health_port=0, upkeep_enabled=False)
    kernel = Kernel(config)
    llm = FlakyLLM()
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = llm
    kernel.flaky = llm
    await kernel.start()
    yield kernel
    await kernel.stop()


# --- переход на зимнее время ---------------------------------------------

def test_dst_transition_keeps_local_hour():
    """«Завтра в 9» должно остаться девятью утра по обе стороны перевода часов."""
    before = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)   # ещё +2
    after = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)    # уже +1

    first, _, _ = parse("напомни завтра в 9 позвонить", now=before, tz=WARSAW)
    second, _, _ = parse("напомни завтра в 9 позвонить", now=after, tz=WARSAW)

    assert first.astimezone(WARSAW).hour == 9
    assert second.astimezone(WARSAW).hour == 9
    assert first.hour != second.hour  # в UTC часы разные — значит переход учтён


# --- восстановление после сбоя модели ------------------------------------

async def test_llm_availability_recovers(tmp_path):
    """Кэш доступности обязан протухать, иначе рестарт Ollama = вечная смерть."""
    provider = OllamaProvider("http://127.0.0.1:59999", "m", "e", probe_ttl=0.2)
    assert await provider.available() is False
    assert await provider.available() is False   # взято из кэша
    checked = provider._checked_at
    await asyncio.sleep(0.25)
    await provider.available()
    assert provider._checked_at > checked        # TTL истёк, сходили заново


async def test_kernel_survives_llm_going_down_and_up(kernel):
    kernel.flaky.up = False
    down = await kernel.ask(_req("поболтаем"))
    assert "недоступна" in down.text

    kernel.flaky.up = True
    up = await kernel.ask(_req("поболтаем"))
    assert "недоступна" not in up.text


# --- обслуживание ---------------------------------------------------------

async def test_backup_is_a_real_openable_database(kernel, tmp_path):
    await kernel.memory.remember("пароль morphine2024")
    target = await kernel.memory.backup(tmp_path / "backups" / "copy.db")
    assert target.exists()

    import sqlite3
    with sqlite3.connect(target) as copy:
        rows = copy.execute("SELECT text FROM memories").fetchall()
    assert any("morphine2024" in row[0] for row in rows)


async def test_wal_mode_is_on(kernel):
    mode = kernel.memory.db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


async def test_upkeep_trims_logs_and_rotates_backups(tmp_path):
    config = Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "v",
                    scheduler_tick=0.1, health_port=0, log_keep_lines=10, backup_keep=2)
    kernel = Kernel(config)
    llm = FlakyLLM()
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = llm
    await kernel.start()
    try:
        trace = tmp_path / "trace.jsonl"
        trace.write_text("\n".join(json.dumps({"i": i}) for i in range(500)) + "\n", encoding="utf-8")

        upkeep = kernel.modules.by_name("upkeep")
        for _ in range(4):
            await upkeep.run()
            await asyncio.sleep(1.05)   # метки различаются по секундам

        assert len(trace.read_text(encoding="utf-8").splitlines()) == 10
        copies = list((tmp_path / "backups").glob("amelia-*.db"))
        assert len(copies) == 2, f"ротация не сработала: {len(copies)} копий"
    finally:
        await kernel.stop()


async def test_recurring_job_is_not_duplicated_on_restart(kernel):
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    first = await kernel.scheduler.ensure_recurring("обслуживание", when, kind="maintenance", every=86400)
    second = await kernel.scheduler.ensure_recurring("обслуживание", when, kind="maintenance", every=86400)
    assert first is not None and second is None
    assert await kernel.scheduler.count_pending(include_internal=True) == 1
    assert await kernel.scheduler.count_pending() == 0   # служебная в напоминания не течёт


# --- здоровье -------------------------------------------------------------

async def test_health_snapshot_reports_state(kernel):
    snapshot = await kernel.health.snapshot()
    assert snapshot["status"] == "ok"
    assert snapshot["modules"] >= 6
    assert "jobs_pending" in snapshot


def test_sd_notify_is_silent_outside_systemd(monkeypatch):
    from amelia.core.health import sd_notify
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("READY=1") is False


def _req(text: str):
    from amelia.core.context import Request
    return Request(text=text)
