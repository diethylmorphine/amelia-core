"""Тесты самораcширения: что Кузница пропускает, а что режет."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amelia.config import Config
from amelia.core.context import Request
from amelia.core.kernel import Kernel
from amelia.core.forge.validator import validate_skill_code

SKILLS_DIR = Path(__file__).resolve().parent.parent / "amelia" / "skills"

GOOD_CODE = '''from __future__ import annotations

from ..core.context import Request, Response
from ..core.module import Module


class EchoModule(Module):
    name = "echotest"
    description = "Повторяет сказанное"
    intents = ("echotest",)

    async def handle(self, request: Request, intent: str) -> Response:
        return self.reply(f"эхо: {request.text}", intent)
'''

GOOD_DRAFT = {
    "name": "echotest",
    "description": "Повторяет сказанное",
    "intents": ["echotest"],
    "triggers": ["эхо"],
    "code": GOOD_CODE,
}


class ScriptedLLM:
    def __init__(self, script=None, classify="chat"):
        self.script = list(script or [])
        self.classify = classify

    async def available(self, recheck: bool = False) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        return []

    async def complete(self, prompt: str, system: str | None = None, on_token=None,
                       language: str | None = None) -> str:
        if system and "классификатор" in system:
            return self.classify
        return json.dumps(self.script.pop(0), ensure_ascii=False) if self.script else "{}"


@pytest.fixture
async def kernel(tmp_path):
    kernel = Kernel(Config(db_path=tmp_path / "t.db", vault_path=tmp_path / "vault"))
    llm = ScriptedLLM()
    kernel.llm = kernel.memory.llm = kernel.router.llm = llm
    kernel.scripted = llm
    await kernel.start()
    yield kernel
    await kernel.stop()
    for leftover in SKILLS_DIR.glob("echotest.*"):
        leftover.unlink()


# --- валидатор -------------------------------------------------------------

def test_validator_accepts_good_code():
    report = validate_skill_code(GOOD_CODE)
    assert report.ok, report.problems
    assert report.skill_name == "echotest"
    assert report.intents == ["echotest"]


@pytest.mark.parametrize("snippet, marker", [
    ("import os\n", "os"),
    ("import subprocess\n", "subprocess"),
    ("import socket\n", "socket"),
])
def test_validator_blocks_imports(snippet, marker):
    report = validate_skill_code(snippet + GOOD_CODE)
    assert not report.ok
    assert any(marker in problem for problem in report.problems)


def test_validator_blocks_eval_and_dunder_escape():
    code = GOOD_CODE.replace(
        'return self.reply(f"эхо: {request.text}", intent)',
        "return self.reply(eval(request.text), intent)",
    )
    assert any("eval" in problem for problem in validate_skill_code(code).problems)

    code = GOOD_CODE.replace(
        'return self.reply(f"эхо: {request.text}", intent)',
        "return self.reply(str(type(self).__subclasses__()), intent)",
    )
    assert any("__subclasses__" in problem for problem in validate_skill_code(code).problems)


def test_validator_blocks_import_time_side_effects():
    code = GOOD_CODE + "\n\nprint('я выполнился при импорте')\n"
    assert any("верхнем уровне" in problem for problem in validate_skill_code(code).problems)


# --- полный цикл -----------------------------------------------------------

async def test_skill_needs_approval_before_it_works(kernel):
    kernel.scripted.script = [GOOD_DRAFT]
    created = await kernel.ask(Request(text="научись повторять за мной"))
    assert created.module == "forge"
    assert "ждёт одобрения" in created.text

    manifest = json.loads((SKILLS_DIR / "echotest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "pending"
    assert kernel.modules.by_name("echotest") is None

    approved = await kernel.ask(Request(text="включи навык echotest"))
    assert "включён" in approved.text
    assert kernel.modules.by_name("echotest") is not None

    kernel.scripted.classify = "echotest"
    used = await kernel.ask(Request(text="повтори это"))
    assert used.module == "echotest"


async def test_bad_skill_never_reaches_registry(kernel):
    evil = dict(GOOD_DRAFT, name="pwntest", code="import os\n" + GOOD_CODE)
    kernel.scripted.script = [evil, evil, evil]
    result = await kernel.ask(Request(text="научись запускать команды системы"))
    assert "Не получилось" in result.text
    assert not (SKILLS_DIR / "pwntest.py").exists()
    assert kernel.modules.by_name("pwntest") is None


async def test_disable_and_drop(kernel):
    kernel.scripted.script = [GOOD_DRAFT]
    await kernel.ask(Request(text="научись повторять за мной"))
    await kernel.ask(Request(text="включи навык echotest"))

    await kernel.ask(Request(text="выключи навык echotest"))
    assert kernel.modules.by_name("echotest") is None

    dropped = await kernel.ask(Request(text="удали навык echotest"))
    assert "_trash" in dropped.text
    assert not (SKILLS_DIR / "echotest.py").exists()
