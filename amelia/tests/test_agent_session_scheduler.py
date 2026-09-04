"""Тесты трёх новых способностей: контекст, инструменты, время."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from amelia.config import Config
from amelia.core.context import Request
from amelia.core.kernel import Kernel
from amelia.modules.remind.timeparse import humanize_every, parse

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
WARSAW = ZoneInfo("Europe/Warsaw")


class ScriptedLLM:
    def __init__(self) -> None:
        self.agent_script: list[str] = []
        self.classify = "chat"
        self.plain = "прямой ответ"

    async def available(self, recheck: bool = False) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        return []

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        system = system or ""
        if "классификатор" in system:
            return self.classify
        if "верни РОВНО один JSON" in system:
            return self.agent_script.pop(0) if self.agent_script else '{"done": true}'
        if "Сожми диалог" in system:
            return "выжимка разговора"
        if on_token is not None:
            await on_token(self.plain)
        return self.plain


@pytest.fixture
async def kernel(tmp_path):
    config = Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "vault",
                    scheduler_tick=0.1, timezone_name="Europe/Warsaw")
    kernel = Kernel(config)
    llm = ScriptedLLM()
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = llm
    kernel.scripted = llm
    await kernel.start()
    yield kernel
    await kernel.stop()


# --- разбор времени ------------------------------------------------------

@pytest.mark.parametrize("text, delta_min, every, payload", [
    ("напомни через 20 минут купить кофе", 20, None, "купить кофе"),
    ("напомни через 2 часа", 120, None, ""),
    ("напоминай каждый час пить воду", 60, 3600, "пить воду"),
])
def test_relative_time(text, delta_min, every, payload):
    when, repeat, rest = parse(text, now=NOW, tz=WARSAW)
    assert when == NOW + timedelta(minutes=delta_min)
    assert repeat == every
    assert payload in rest


def test_absolute_time_rolls_to_tomorrow():
    # 09:00 по Варшаве уже прошло в 14:00 — переносим на завтра
    when, _, _ = parse("напомни в 9 позвонить", now=NOW, tz=WARSAW)
    assert when == datetime(2026, 8, 2, 7, 0, tzinfo=timezone.utc)


def test_tomorrow_defaults_to_morning():
    when, _, rest = parse("напомни завтра забрать посылку", now=NOW, tz=WARSAW)
    assert when == datetime(2026, 8, 2, 7, 0, tzinfo=timezone.utc)
    assert "забрать посылку" in rest


def test_unparseable_time_returns_none():
    when, _, _ = parse("напомни когда-нибудь потом", now=NOW, tz=WARSAW)
    assert when is None


def test_plurals():
    assert humanize_every(86400) == "каждый день"
    assert humanize_every(172800) == "каждые 2 дня"
    assert humanize_every(432000) == "каждые 5 дней"


# --- сессии --------------------------------------------------------------

async def test_session_survives_between_requests(kernel):
    kernel.scripted.agent_script = ['{"answer":"приятно познакомиться"}']
    await kernel.ask(Request(text="меня зовут Дмитрий"))
    block = await kernel.sessions.as_prompt_block("cli:local")
    assert "Дмитрий" in block


async def test_forget_clears_context_completely(kernel):
    kernel.scripted.agent_script = ['{"answer":"ок"}']
    await kernel.ask(Request(text="запомни это в разговоре"))
    kernel.scripted.classify = "forget"
    response = await kernel.ask(Request(text="забудь разговор"))
    assert response.intent == "forget"
    assert await kernel.sessions.as_prompt_block("cli:local") == ""


async def test_sessions_are_isolated_per_user(kernel):
    kernel.scripted.agent_script = ['{"answer":"ок"}', '{"answer":"ок"}']
    await kernel.ask(Request(text="секрет первого", user_id="one"))
    await kernel.ask(Request(text="секрет второго", user_id="two"))
    assert "первого" not in await kernel.sessions.as_prompt_block("cli:two")


# --- агент ---------------------------------------------------------------

async def test_agent_chains_tool_then_answers(kernel):
    await kernel.ask(Request(text="запомни пароль morphine2024"))
    kernel.scripted.classify = "chat"
    kernel.scripted.agent_script = [
        '{"tool":"search_memory","args":{"query":"пароль"}}',
        '{"done": true}',
    ]
    response = await kernel.ask(Request(text="какой у меня пароль?"))
    assert response.data["tools"] == "search_memory"
    assert response.data["steps"] == 1   # один вызов инструмента, ответ пишется отдельно


async def test_agent_survives_unknown_tool(kernel):
    kernel.scripted.classify = "chat"
    kernel.scripted.agent_script = [
        '{"tool":"launch_missiles","args":{}}',
        '{"done": true}',
    ]
    response = await kernel.ask(Request(text="сделай что-нибудь"))
    # Несуществующий инструмент не вызван — значит, обычный путь с потоком
    assert response.data.get("tools", "") == ""
    assert response.text


async def test_agent_respects_step_ceiling(kernel):
    kernel.config.agent_max_steps = 2
    kernel.scripted.classify = "chat"
    kernel.scripted.agent_script = ['{"tool":"list_reminders","args":{}}'] * 5
    response = await kernel.ask(Request(text="крутись бесконечно"))
    assert response.data["steps"] == 2   # потолок соблюдён, цикл не бесконечный
    assert response.text


async def test_toolbox_has_no_duplicates(kernel):
    tools = kernel.agent.toolbox()
    assert len(tools) == len(set(tools))
    assert {"save_note", "search_memory", "schedule_reminder"} <= set(tools)


# --- планировщик ---------------------------------------------------------

async def test_reminder_fires(kernel):
    fired: list[str] = []
    kernel.bus.on("job.fired", lambda job: fired.append(job.payload))
    await kernel.scheduler.schedule("выпить воды", datetime.now(timezone.utc))
    import asyncio
    await asyncio.sleep(0.35)
    assert fired == ["выпить воды"]


async def test_recurring_job_reschedules_itself(kernel):
    job_id = await kernel.scheduler.schedule(
        "зарядка", datetime.now(timezone.utc) - timedelta(seconds=1), every=3600
    )
    import asyncio
    await asyncio.sleep(0.35)
    pending = {job.id for job in await kernel.scheduler.pending()}
    assert job_id in pending  # повторяющаяся не исчезает, а уезжает вперёд


async def test_cancel_reminder(kernel):
    kernel.scripted.classify = "remind"
    created = await kernel.ask(Request(text="напомни через 5 часов проверить сборку"))
    job_id = created.data["job_id"]
    kernel.scripted.classify = "remind_cancel"
    cancelled = await kernel.ask(Request(text=f"отмени напоминание {job_id}"))
    assert cancelled.data["ok"] is True
    assert await kernel.scheduler.count_pending() == 0   # служебные задачи сюда не попадают
