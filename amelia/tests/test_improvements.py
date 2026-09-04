"""Тесты семи доработок: приватность, скорость, поток, шлюзы, песочница."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from amelia.config import Config
from amelia.core.context import Request
from amelia.core.forge.sandbox import SkillKernel
from amelia.core.kernel import Kernel
from amelia.core.memory.store import SHARED


class ScriptedLLM:
    def __init__(self, vectors: bool = False) -> None:
        self.agent_script: list[str] = []
        self.classify = "chat"
        self.answer = "готовый ответ"
        self.vectors = vectors

    async def available(self, recheck: bool = False) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        if not self.vectors:
            return []
        # Детерминированный «эмбеддинг»: одинаковый текст -> одинаковый вектор.
        base = [(hash((text, i)) % 1000) / 1000 for i in range(32)]
        return base

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        system = system or ""
        if "классификатор" in system:
            return self.classify
        if "верни РОВНО один JSON" in system or "Ты Амелия — личный локальный ассистент. У тебя есть" in system:
            return self.agent_script.pop(0) if self.agent_script else '{"done": true}'
        if on_token is not None:
            for piece in self.answer.split(" "):
                await on_token(piece + " ")
        return self.answer


@pytest.fixture
async def kernel(tmp_path):
    config = Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "v",
                    scheduler_tick=0.1, health_port=0, upkeep_enabled=False)
    kernel = Kernel(config)
    llm = ScriptedLLM()
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = llm
    kernel.scripted = llm
    await kernel.start()
    yield kernel
    await kernel.stop()


# === 1. приватность памяти ==============================================

async def test_memory_does_not_leak_between_users(kernel):
    """Тот самый баг: пароль Алисы не должен находиться Бобом."""
    await kernel.ask(Request(text="запомни мой пароль от банка hunter2", user_id="alice"))
    kernel.scripted.classify = "recall"
    bob = await kernel.ask(Request(text="вспомни пароль", user_id="bob"))
    assert "hunter2" not in bob.text

    alice = await kernel.ask(Request(text="вспомни пароль", user_id="alice"))
    assert "hunter2" in alice.text


async def test_chat_context_is_also_scoped(kernel):
    """Утечка в контекст опаснее: происходит молча, без всякого «вспомни»."""
    await kernel.ask(Request(text="запомни код от сейфа 4815", user_id="alice"))
    found = await kernel.memory.recall("код от сейфа", user_id="bob")
    assert all("4815" not in memory.text for memory in found)


async def test_shared_scope_is_visible_to_everyone(kernel):
    await kernel.memory.remember("вайфай гостевой guest2026", user_id="alice", scope=SHARED)
    found = await kernel.memory.recall("вайфай", user_id="bob")
    assert any("guest2026" in memory.text for memory in found)


# === 2. поиск не сканирует всю базу =====================================

async def test_recall_stays_fast_as_base_grows(tmp_path):
    config = Config(db_path=tmp_path / "big.db", vault_path=tmp_path / "v",
                    health_port=0, upkeep_enabled=False, scheduler_enabled=False)
    kernel = Kernel(config)
    llm = ScriptedLLM(vectors=True)
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = llm
    await kernel.start()
    try:
        for i in range(1500):
            await kernel.memory.remember(f"заметка {i} про проект и сборку", user_id="local")

        start = time.perf_counter()
        for _ in range(5):
            await kernel.memory.recall("проект сборка", user_id="local")
        elapsed_ms = (time.perf_counter() - start) / 5 * 1000

        # Раньше ранжировалась вся таблица; теперь — короткий список кандидатов.
        assert elapsed_ms < 120, f"поиск деградировал: {elapsed_ms:.0f} мс"
    finally:
        await kernel.stop()


# === 3. дедупликация и свежесть =========================================

async def test_repeating_the_same_note_does_not_duplicate(kernel):
    first = await kernel.memory.remember("пароль от вайфая morphine2024", user_id="local")
    second = await kernel.memory.remember("Пароль от вайфая morphine2024  ", user_id="local")
    assert first == second
    assert await kernel.memory.count(user_id="local") == 1


async def test_fresh_note_outranks_stale_one(kernel):
    old_id = await kernel.memory.remember("сборка проекта падает", user_id="local")
    kernel.memory.db.execute(
        "UPDATE memories SET created_at = '2023-01-01T00:00:00+00:00' WHERE id = ?", (old_id,)
    )
    kernel.memory.db.commit()
    await kernel.memory.remember("сборка проекта чинится так", user_id="local")

    found = await kernel.memory.recall("сборка проекта", user_id="local", limit=2)
    assert "чинится" in found[0].text


# === 4. поток ============================================================

async def test_tokens_reach_the_client(kernel):
    pieces: list[str] = []

    async def sink(piece: str) -> None:
        pieces.append(piece)

    kernel.scripted.classify = "chat"
    kernel.scripted.answer = "первое второе третье"
    response = await kernel.ask(Request(text="расскажи что-нибудь", on_token=sink))

    assert len(pieces) >= 3, "ответ пришёл одним куском"
    assert "".join(pieces).strip() == response.text


# === 5. шлюзы у пишущих инструментов ====================================

async def test_mutating_tools_can_be_switched_off(kernel):
    assert "save_note" in kernel.agent.toolbox()
    kernel.config.agent_allow_mutations = False
    toolbox = kernel.agent.toolbox()
    assert "save_note" not in toolbox and "schedule_reminder" not in toolbox
    assert "search_memory" in toolbox, "читающие должны остаться"


async def test_agent_write_is_journalled_and_undoable(kernel):
    kernel.scripted.classify = "chat"
    kernel.scripted.agent_script = [
        '{"tool":"save_note","args":{"text":"агент записал это сам"}}',
        '{"done": true}',
    ]
    await kernel.ask(Request(text="что там у меня по проекту", user_id="alice"))

    actions = await kernel.agent.recent_actions("alice")
    assert actions and actions[0]["tool"] == "save_note"
    assert await kernel.memory.count(user_id="alice") == 1

    message = await kernel.agent.undo_last("alice")
    assert "Отменила" in message
    assert await kernel.memory.count(user_id="alice") == 0


async def test_read_only_tool_is_not_journalled(kernel):
    kernel.scripted.classify = "chat"
    kernel.scripted.agent_script = [
        '{"tool":"search_memory","args":{"query":"что-нибудь"}}',
        '{"done": true}',
    ]
    await kernel.ask(Request(text="что там по проекту", user_id="bob"))
    assert await kernel.agent.recent_actions("bob") == []


# === 6. песочница возможностей навыка ===================================

def test_skill_kernel_hides_everything_dangerous(tmp_path):
    class FakeKernel:
        config = Config(db_path=tmp_path / "x.db", bot_token="СЕКРЕТНЫЙ-ТОКЕН")
        memory = object()
        llm = object()
        bus = object()
        scheduler = object()
        forge = object()
        modules = object()

    scoped = SkillKernel(FakeKernel(), "tips")

    assert not hasattr(scoped.config, "bot_token"), "токен бота виден навыку"
    for forbidden in ("bus", "scheduler", "forge", "modules", "sessions", "health"):
        with pytest.raises(AttributeError):
            getattr(scoped, forbidden)
    assert scoped.config.llm_model  # безобидное — на месте


async def test_builtin_modules_still_get_full_kernel(kernel):
    system = kernel.modules.by_name("system")
    assert system.kernel is kernel
    assert not kernel.modules.is_generated(system)


# === 7. выгрузка копии наружу ===========================================

async def test_offsite_backup_runs_the_configured_command(tmp_path):
    marker = tmp_path / "uploaded.txt"
    config = Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "v", health_port=0,
                    scheduler_enabled=False,
                    backup_command=f"/bin/sh -c echo_placeholder")
    config.backup_command = f"/usr/bin/env cp {{path}} {marker}"
    kernel = Kernel(config)
    llm = ScriptedLLM()
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = llm
    await kernel.start()
    try:
        report = await kernel.modules.by_name("upkeep").run()
        assert "Наружу" in report
        assert marker.exists(), "копия наружу не уехала"
    finally:
        await kernel.stop()


async def test_offsite_failure_does_not_break_upkeep(tmp_path):
    config = Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "v", health_port=0,
                    scheduler_enabled=False, backup_command="/nonexistent/binary {path}")
    kernel = Kernel(config)
    llm = ScriptedLLM()
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = llm
    await kernel.start()
    try:
        report = await kernel.modules.by_name("upkeep").run()
        assert "Копия базы" in report      # локальный бэкап всё равно снят
    finally:
        await kernel.stop()
