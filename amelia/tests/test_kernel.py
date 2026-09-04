"""Дымовые тесты ядра. Работают без Ollama — LLM подменяется заглушкой."""

from __future__ import annotations

import pytest

from amelia.config import Config
from amelia.core.context import Request
from amelia.core.kernel import Kernel
from amelia.core.logic.intents import Intent
from amelia.core.logic.rules import match_rules, strip_trigger


class OfflineLLM:
    """Модель, которой нет: проверяем, что ядро выживает без неё."""

    async def available(self, recheck: bool = False) -> bool:
        return False

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        return ""

    async def embed(self, text: str) -> list[float]:
        return []


@pytest.fixture
async def kernel(tmp_path):
    config = Config(db_path=tmp_path / "test.db", vault_path=tmp_path / "vault")
    kernel = Kernel(config)
    kernel.llm = OfflineLLM()
    kernel.memory.llm = OfflineLLM()
    kernel.router.llm = OfflineLLM()
    await kernel.start()
    yield kernel
    await kernel.stop()


def test_rules_match():
    assert match_rules("помощь") == Intent.HELP
    assert match_rules("запомни купить кофе") == Intent.NOTE
    assert match_rules("расскажи анекдот") is None
    assert strip_trigger("запомни: купить кофе") == "купить кофе"


async def test_modules_discovered(kernel):
    assert {"system", "notes", "recall", "chat"} <= set(kernel.modules.catalog())


async def test_help_intent(kernel):
    response = await kernel.ask(Request(text="помощь"))
    assert response.module == "system"
    assert "notes" in response.text


async def test_note_then_recall(kernel):
    saved = await kernel.ask(Request(text="запомни пароль от вайфая morphine2024"))
    assert saved.module == "notes"

    found = await kernel.ask(Request(text="вспомни morphine2024"))
    assert found.module == "recall"
    assert "morphine2024" in found.text


async def test_chat_degrades_gracefully(kernel):
    response = await kernel.ask(Request(text="просто поболтаем о жизни"))
    assert response.module == "chat"
    assert response.text
