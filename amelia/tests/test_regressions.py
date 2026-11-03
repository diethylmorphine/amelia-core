"""Регрессии: баги, найденные аудитом. Каждый тест падал до правки."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from amelia.clients.telegram import TelegramClient
from amelia.config import Config
from amelia.core.context import Request, Response
from amelia.core.kernel import Kernel
from amelia.core.module import Module
from amelia.core.trace import Tracer
from amelia.modules.remind.timeparse import parse

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class OfflineLLM:
    async def available(self, recheck: bool = False) -> bool:
        return False

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        return ""

    async def embed(self, text: str) -> list[float]:
        return []


@pytest.fixture
async def kernel(tmp_path):
    config = Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "v",
                    health_port=0, upkeep_enabled=False, scheduler_tick=0.1)
    kernel = Kernel(config)
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = OfflineLLM()
    await kernel.start()
    yield kernel
    await kernel.stop()


# --- 1. мусор в переменных окружения не роняет службу ---------------------

def test_garbage_env_does_not_kill_startup(monkeypatch):
    """Под systemd падение на старте — это бесконечная рестарт-петля."""
    monkeypatch.setenv("AMELIA_AGENT_MAX_STEPS", "не число")
    monkeypatch.setenv("AMELIA_SCHEDULER_TICK", "")
    monkeypatch.setenv("AMELIA_HEALTH_PORT", "-5")
    config = Config.load(env_file=Path("/nonexistent"))
    assert config.agent_max_steps == 4
    assert config.scheduler_tick == 5.0
    assert config.health_port == 0


def test_out_of_range_values_are_clamped(monkeypatch):
    monkeypatch.setenv("AMELIA_UPKEEP_HOUR", "99")
    monkeypatch.setenv("AMELIA_AGENT_MAX_STEPS", "9999")
    config = Config.load(env_file=Path("/nonexistent"))
    assert config.upkeep_hour == 23
    assert config.agent_max_steps == 20


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_float_env_uses_defaults(monkeypatch, value):
    monkeypatch.setenv("AMELIA_SCHEDULER_TICK", value)
    monkeypatch.setenv("AMELIA_LLM_PROBE_TTL", value)
    monkeypatch.setenv("AMELIA_TEMPERATURE", value)

    config = Config.load(env_file=Path("/nonexistent"))

    assert config.scheduler_tick == 5.0
    assert config.llm_probe_ttl == 30.0
    assert config.llm_temperature == 0.3


async def test_telegram_requires_owner_id_before_starting():
    client = TelegramClient(Kernel(Config(bot_token="test-token")))

    with pytest.raises(SystemExit, match="AMELIA_OWNER_ID"):
        await client.run()


def test_installer_environment_template_exists():
    template = Path(__file__).resolve().parents[1] / ".env.example"

    assert template.is_file()
    contents = template.read_text(encoding="utf-8")
    assert "AMELIA_BOT_TOKEN=" in contents
    assert "AMELIA_OWNER_ID=" in contents


# --- 2. сломанный модуль не превращает ассистента в кирпич ---------------

class ExplodingModule(Module):
    name = "exploding"
    description = "падает на setup"
    intents = ("exploding",)

    async def setup(self, kernel) -> None:
        raise RuntimeError("я сломан")

    async def handle(self, request, intent):
        return self.reply("никогда", intent)


async def test_broken_module_is_dropped_not_fatal(tmp_path):
    config = Config(db_path=tmp_path / "t.db", health_port=0,
                    upkeep_enabled=False, scheduler_enabled=False)
    kernel = Kernel(config)
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = OfflineLLM()
    await kernel.memory.init()
    kernel.modules.discover()
    kernel.modules._modules.append(ExplodingModule())

    await kernel._setup_modules()          # раньше здесь падало всё ядро

    assert kernel.modules.by_name("exploding") is None
    assert kernel.modules.by_name("system") is not None
    await kernel.memory.close()


# --- 3. разбор времени не переполняется и не даёт нулевой период ---------

def test_absurd_interval_does_not_crash():
    when, every, _ = parse("напомни через 999999999999 дней", now=NOW)
    assert when is None and every is None          # раньше OverflowError


def test_zero_period_is_rejected():
    """«каждые 0 минут» молча становилось разовой задачей."""
    _, every, _ = parse("напоминай каждые 0 минут пить воду", now=NOW)
    assert every is None


def test_too_frequent_period_is_rejected():
    _, every, _ = parse("напоминай каждую секунду", now=NOW)
    assert every is None


def test_sane_intervals_still_work():
    when, _, _ = parse("напомни через 20 минут кофе", now=NOW)
    assert when == NOW + timedelta(minutes=20)
    _, every, _ = parse("напоминай каждый день в 8 зарядка", now=NOW)
    assert every == 86400


# --- 4. планировщик догоняет арифметикой, а не циклом --------------------

async def test_scheduler_catches_up_instantly_after_long_downtime(kernel):
    """Машина стояла год, период минута: цикл сделал бы полмиллиона итераций."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=365)
    await kernel.scheduler.schedule("зарядка", long_ago, every=60)

    start = time.perf_counter()
    for job in await kernel.scheduler.due():
        await kernel.scheduler._fire(job)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f"догон занял {elapsed:.1f} с"
    upcoming = (await kernel.scheduler.pending())[0]
    assert upcoming.when > datetime.now(timezone.utc)


# --- 5. трассировка не путает параллельные запросы -----------------------

def test_tracer_measures_each_request_separately(tmp_path):
    """Раньше ключом был id(request): переиспользование id ломало замеры."""
    tracer = Tracer(tmp_path / "trace.jsonl", enabled=False)

    slow, fast = Request(text="медленный"), Request(text="быстрый")
    tracer._on_request(slow)
    time.sleep(0.05)
    tracer._on_request(fast)

    slow_response = Response(text="a", data={"_started": slow.meta["_started"]})
    fast_response = Response(text="b", data={"_started": fast.meta["_started"]})
    records = []
    tracer._write = lambda record: records.append(record)

    tracer._on_response(fast_response)
    tracer._on_response(slow_response)

    assert records[1]["ms"] > records[0]["ms"], "замеры перепутаны между запросами"


async def test_tracer_survives_response_without_request(kernel):
    kernel.tracer._on_response(Response(text="сирота"))
    assert kernel.tracer.counters["module.core"] >= 1


# --- 6. user_id у поиска нельзя забыть -----------------------------------

def test_recall_requires_user_id_explicitly():
    """Утечка случилась именно потому, что аргумент был необязательным."""
    import inspect

    from amelia.core.memory.store import MemoryStore

    parameter = inspect.signature(MemoryStore.recall).parameters["user_id"]
    assert parameter.default is inspect.Parameter.empty

    with pytest.raises(TypeError):
        MemoryStore.recall(None, "запрос")          # без user_id вызвать нельзя


async def test_all_users_sentinel_is_explicit(kernel):
    from amelia.core.memory.store import ALL_USERS
    await kernel.memory.remember("общая заметка", user_id="alice")
    assert await kernel.memory.recall("общая", ALL_USERS)
    assert not await kernel.memory.recall("общая", "bob")


# --- 7. дедуп не съедает разные записи -----------------------------------

class BagOfWordsLLM(OfflineLLM):
    """Правдоподобные векторы: близкие тексты — близкие направления.

    Хеш обязан быть детерминированным: встроенный hash() рандомизируется
    на каждый процесс, и тест на его основе врёт примерно раз в шестьдесят
    прогонов из-за случайных коллизий. Флейковый тест хуже отсутствующего.
    """

    DIMS = 512

    async def available(self, recheck: bool = False) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        import re
        import zlib
        vector = [0.0] * self.DIMS
        for word in re.findall(r"\w+", text.lower()):
            vector[zlib.crc32(word.encode("utf-8")) % self.DIMS] += 1.0
        return vector


class DegenerateLLM(BagOfWordsLLM):
    """Сломанная модель: всем один и тот же вектор."""

    async def embed(self, text: str) -> list[float]:
        return [0.5] * self.DIMS


@pytest.mark.parametrize("first, second", [
    ("напомнить про встречу в 10", "напомнить про встречу в 11"),
    ("пароль от вайфая abc", "пароль от вайфая xyz"),
])
async def test_similar_but_different_notes_are_kept(tmp_path, first, second):
    config = Config(db_path=tmp_path / "t.db", health_port=0,
                    upkeep_enabled=False, scheduler_enabled=False)
    kernel = Kernel(config)
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = BagOfWordsLLM()
    await kernel.start()
    try:
        one = await kernel.memory.remember(first, user_id="dave")
        two = await kernel.memory.remember(second, user_id="dave")
        assert one != two, "разные заметки слились — второй текст потерян"
    finally:
        await kernel.stop()


async def test_degenerate_embedder_cannot_eat_the_database(tmp_path):
    """Если модель эмбеддингов сломалась и выдаёт всем один вектор,
    защита по длине не даёт дедупу схлопнуть всю память в одну запись."""
    config = Config(db_path=tmp_path / "t.db", health_port=0,
                    upkeep_enabled=False, scheduler_enabled=False)
    kernel = Kernel(config)
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = DegenerateLLM()
    await kernel.start()
    try:
        await kernel.memory.remember("короткая", user_id="dave")
        await kernel.memory.remember("совсем другая заметка гораздо длиннее первой", user_id="dave")
        assert await kernel.memory.count(user_id="dave") == 2
    finally:
        await kernel.stop()


async def test_exact_duplicates_still_merge(tmp_path):
    config = Config(db_path=tmp_path / "t.db", health_port=0,
                    upkeep_enabled=False, scheduler_enabled=False)
    kernel = Kernel(config)
    kernel.llm = kernel.memory.llm = kernel.router.llm = kernel.sessions.llm = BagOfWordsLLM()
    await kernel.start()
    try:
        one = await kernel.memory.remember("пароль morphine2024", user_id="dave")
        two = await kernel.memory.remember("Пароль  MORPHINE2024 ".lower(), user_id="dave")
        assert one == two
    finally:
        await kernel.stop()


async def test_concurrent_identical_notes_are_deduplicated(kernel):
    ids = await asyncio.gather(*[
        kernel.memory.remember("same concurrent note", user_id="dave")
        for _ in range(30)
    ])

    assert len(set(ids)) == 1
    assert await kernel.memory.count(user_id="dave") == 1


# --- устойчивость к странному вводу --------------------------------------

@pytest.mark.parametrize("text", [
    "", "   ", "\n\n", "🙂" * 50, "'; DROP TABLE memories;--", "%s %d {0}", "я" * 9000,
])
async def test_weird_input_does_not_crash(kernel, text):
    response = await kernel.ask(Request(text=text, user_id="u0"))
    assert isinstance(response.text, str)
    assert kernel.memory.db.execute("SELECT COUNT(*) FROM memories").fetchone() is not None


@pytest.mark.parametrize("query", ["", "!!!", "OR AND NOT", "*", '"""""'])
async def test_weird_search_does_not_crash(kernel, query):
    assert isinstance(await kernel.memory.recall(query, "u0"), list)


async def test_concurrent_requests_all_land(kernel):
    """Телеграм — это параллельные обращения от разных людей."""
    async def one(index: int):
        return await kernel.ask(
            Request(text=f"запомни маркер{index}", user_id=f"u{index % 5}", source="telegram")
        )

    results = await asyncio.gather(*[one(i) for i in range(30)], return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception)]
    assert await kernel.memory.count() == 30


# --- фиксация языка: китайские вставки у Qwen ----------------------------

class ChineseProneLLM(OfflineLLM):
    """Модель, которая подмешивает иероглифы, пока её не одёрнешь построже."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def available(self, recheck: bool = False) -> bool:
        return True

    async def complete(self, prompt, system=None, on_token=None, language=None):
        self.calls.append(system)
        insisted = system and "КРИТИЧЕСКИ ВАЖНО" in system
        text = "Сборка падает, не хватает памяти." if insisted else "Сборка падает 因为 мало памяти。"
        if on_token is not None:
            for piece in text.split(" "):
                await on_token(piece + " ")
        return text


def test_language_is_detected_by_alphabet():
    from amelia.core import lang
    assert lang.detect("Привет, как дела") == "ru"
    assert lang.detect("Hello there friend") == "en"
    assert lang.detect("12345 !!!") == "ru"          # запасной вариант


def test_emoji_survive_but_hieroglyphs_do_not():
    from amelia.core import lang
    assert lang.strip_foreign("Отлично! 🙂") == "Отлично! 🙂"
    assert lang.strip_foreign("Пароль 密码 morphine") == "Пароль morphine"
    assert not lang.has_foreign("Обычный русский текст")


def test_thinking_block_is_removed():
    """Qwen3 в режиме рассуждений оборачивает мысли в <think>."""
    from amelia.core import lang
    assert lang.clean("<think>надо подумать</think>Ответ.") == "Ответ."


def test_directive_is_pinned_to_system_prompt():
    """Раньше промпты просили «на языке собеседника» — модель это игнорировала."""
    from amelia.core import lang
    pinned = lang.pin("Ты ассистент.", "ru")
    assert "ТОЛЬКО НА РУССКОМ" in pinned
    assert "Ты ассистент." in pinned


async def test_hieroglyphs_trigger_regeneration_not_just_stripping():
    """Вырезать нельзя: 因为 — это «потому что», без него фраза разваливается."""
    from amelia.core.llm.ollama import OllamaProvider

    provider = OllamaProvider("http://x", "qwen", "bge")
    fake = ChineseProneLLM()
    provider._once = lambda prompt, system: fake.complete(prompt, system)

    answer = await provider.complete("почему падает сборка", system="Ты Амелия.", language="ru")

    assert "因为" not in answer
    assert "не хватает памяти" in answer, "должна была сработать перегенерация"
    assert len(fake.calls) == 2, "перегенерации не было — просто вырезали"
    assert provider.stats["foreign_fixed_by_retry"] == 1


async def test_stream_filters_hieroglyphs_inline():
    """В потоке перегенерировать нечего — отправленное уже на экране."""
    from amelia.core.llm.ollama import OllamaProvider

    provider = OllamaProvider("http://x", "qwen", "bge")
    fake = ChineseProneLLM()

    async def fake_stream(messages, on_token):
        return await fake.complete("", messages[0]["content"], on_token=on_token)

    provider._stream = fake_stream
    received: list[str] = []

    await provider.complete("вопрос", system="Ты Амелия.",
                            on_token=lambda p: received.append(p) or asyncio.sleep(0),
                            language="ru")

    assert not any("因为" in piece for piece in received)
    assert provider.stats["foreign_stripped"] == 1


def test_sampling_options_are_actually_sent():
    """Без options Ollama берёт temperature 0.8 — главный источник иероглифов."""
    config = Config(llm_temperature=0.3, llm_min_p=0.05)
    assert config.sampling["temperature"] == 0.3
    assert config.sampling["min_p"] == 0.05

    kernel_config = Config(db_path=Path("/tmp/x.db"))
    kernel = Kernel(kernel_config)
    assert kernel.llm.options["temperature"] == 0.3
    assert kernel.llm.options["repeat_penalty"] >= 1.0


async def test_language_is_resolved_once_per_request(kernel):
    response = await kernel.ask(Request(text="Привет, как дела?"))
    assert response.intent
    request = Request(text="Hello, how are you?")
    await kernel.ask(request)
    assert request.meta["lang"] == "en"
