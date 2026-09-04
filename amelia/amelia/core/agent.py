"""Агентный цикл: модули становятся инструментами модели.

Управление и вывод разделены намеренно: JSON решает, что вызвать, а финальный
ответ пишется прозой отдельным потоковым вызовом. Так пользователь не ждёт,
пока модель до конца соберёт экранированную строку внутри JSON.

Пишущие инструменты помечены mutates=True: их можно отключить целиком
(agent_allow_mutations) и в любом случае каждый вызов попадает в журнал,
откуда его можно откатить.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 30.0

CONTROL_SYSTEM = """Ты Амелия — личный локальный ассистент. У тебя есть инструменты.

На каждом шаге верни РОВНО один JSON-объект, без markdown и пояснений:
  {"tool": "имя", "args": {...}}   — вызвать инструмент
  {"done": true}                   — данных достаточно, пора отвечать

Правила:
- Вызывай инструмент только если без него не ответить. Знаешь ответ — сразу {"done": true}.
- Никогда не выдумывай результаты инструментов: дождись их на следующем шаге.
- Сам ответ здесь не пиши, его напишешь отдельно."""

ANSWER_SYSTEM = (
    "Ты Амелия — личный локальный ассистент. Ответь кратко и по делу, "
    "опираясь на контекст разговора и на результаты инструментов. "
    "Не выдумывай того, чего в них нет. Без преамбул и без JSON."
)


@dataclass(slots=True)
class ToolResult:
    """Результат вызова. undo — то, чем действие можно отменить."""
    text: str
    undo: str | None = None


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    args: dict[str, str]
    run: Callable[..., Awaitable[Any]]
    mutates: bool = False                       # меняет ли состояние
    undo: Callable[[str], Awaitable[bool]] | None = None
    module: str = ""

    def signature(self) -> str:
        args = ", ".join(f"{key}: {value}" for key, value in self.args.items()) or "без аргументов"
        mark = " [изменяет данные]" if self.mutates else ""
        return f"{self.name}({args}) — {self.description}{mark}"


@dataclass(slots=True)
class AgentResult:
    text: str
    steps: int = 0
    calls: list[str] = field(default_factory=list)
    fell_back: bool = False


class Agent:
    def __init__(self, kernel) -> None:
        self.kernel = kernel

    # ------------------------------------------------------------------ #
    def toolbox(self, mutating: bool | None = None) -> dict[str, Tool]:
        """Инструменты всех модулей. mutating=False — только читающие."""
        allow_writes = self.kernel.config.agent_allow_mutations
        tools: dict[str, Tool] = {}
        for module in self.kernel.modules:
            for tool in module.tools():
                if tool.name in tools:
                    log.warning("Инструмент %s объявлен дважды — беру первый", tool.name)
                    continue
                if tool.mutates and not allow_writes:
                    continue
                if mutating is not None and tool.mutates is not mutating:
                    continue
                tool.module = module.name
                tools[tool.name] = tool
        return tools

    async def run(self, request, context: str = "") -> AgentResult:
        tools = self.toolbox()
        if not tools or not await self.kernel.llm.available():
            return AgentResult(text="", fell_back=True)

        catalog = "\n".join(f"- {tool.signature()}" for tool in tools.values())
        transcript: list[str] = []
        calls: list[str] = []
        max_steps = self.kernel.config.agent_max_steps

        for step in range(1, max_steps + 1):
            prompt = self._control_prompt(request.text, context, catalog, transcript, step, max_steps)
            raw = await self.kernel.llm.complete(
                prompt, system=CONTROL_SYSTEM, language=request.meta.get("lang")
            )
            action = self._parse(raw)

            if action is None or action.get("done") or "answer" in action:
                if not calls:
                    # Инструменты не понадобились — пусть отвечает обычный путь,
                    # он умеет в поток и не платит за лишний вызов.
                    return AgentResult(text="", steps=step, fell_back=True)
                break

            name = str(action.get("tool", "")).strip()
            args = action.get("args") or {}
            if name not in tools:
                transcript.append(f"[шаг {step}] Инструмента «{name}» нет. Доступны: {', '.join(tools)}")
                continue
            if not isinstance(args, dict):
                transcript.append(f"[шаг {step}] args должен быть объектом, пришло: {type(args).__name__}")
                continue

            result = await self._call(tools[name], args, request)
            calls.append(name)
            transcript.append(f"[шаг {step}] {name}({json.dumps(args, ensure_ascii=False)}) -> {result}")

        if not calls:
            return AgentResult(text="", steps=max_steps, fell_back=True)

        answer = await self.kernel.llm.complete(
            self._answer_prompt(request.text, context, transcript),
            system=ANSWER_SYSTEM,
            on_token=request.on_token,
            language=request.meta.get("lang"),
        )
        return AgentResult(
            text=answer or "Инструменты отработали, но сформулировать ответ не вышло.",
            steps=len(transcript), calls=calls,
        )

    # ------------------------------------------------------------------ #
    def _control_prompt(self, question, context, catalog, transcript, step, max_steps) -> str:
        parts = [f"Инструменты:\n{catalog}"]
        if context:
            parts.append(f"Контекст:\n{context}")
        if transcript:
            parts.append("Что уже сделано:\n" + "\n".join(transcript))
        parts.append(f"Вопрос пользователя: {question}")
        remaining = max_steps - step
        parts.append(
            'Шагов не осталось — верни {"done": true}.' if remaining <= 0
            else f"Осталось шагов: {remaining}. Твой ход — верни JSON."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _answer_prompt(question, context, transcript) -> str:
        parts = []
        if context:
            parts.append(f"Контекст:\n{context}")
        if transcript:
            parts.append("Результаты инструментов:\n" + "\n".join(transcript))
        parts.append(f"Вопрос: {question}")
        return "\n\n".join(parts)

    async def _call(self, tool: Tool, args: dict, request) -> str:
        """Вызов под таймаутом. Пишущий — с записью в журнал."""
        try:
            accepted = inspect.signature(tool.run).parameters
            filtered = {key: value for key, value in args.items() if key in accepted}
            # Инструменты, которым важно, кто спрашивает, объявляют user_id.
            if "user_id" in accepted:
                filtered["user_id"] = request.user_id
            result = await asyncio.wait_for(tool.run(**filtered), timeout=TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            return f"ошибка: инструмент не ответил за {TOOL_TIMEOUT:.0f} с"
        except TypeError as exc:
            return f"ошибка аргументов: {exc}"
        except Exception as exc:
            log.exception("Инструмент %s упал", tool.name)
            return f"ошибка: {type(exc).__name__}: {exc}"

        if isinstance(result, ToolResult):
            text, undo_token = result.text, result.undo
        elif isinstance(result, str):
            text, undo_token = result, None
        else:
            text, undo_token = json.dumps(result, ensure_ascii=False, default=str), None

        if tool.mutates:
            await self._journal(tool, args, text, undo_token, request.user_id)
        return (text or "пусто")[:2000]

    async def _journal(self, tool: Tool, args: dict, result: str, undo_token, user_id: str) -> None:
        """Каждое самостоятельное изменение оставляет след."""
        try:
            self.kernel.memory.db.execute(
                "INSERT INTO tool_journal (user_id, tool, args, result, undo_token, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, tool.name, json.dumps(args, ensure_ascii=False), result[:500],
                 undo_token, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self.kernel.memory.db.commit()
        except Exception:
            log.exception("Не удалось записать действие в журнал")

    @staticmethod
    def _parse(raw: str) -> dict | None:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text.rstrip())
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data if {"tool", "done", "answer"} & data.keys() else None

    # ------------------------------------------------------------------ #
    async def recent_actions(self, user_id: str, limit: int = 10) -> list[dict]:
        rows = self.kernel.memory.db.execute(
            "SELECT * FROM tool_journal WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    async def undo_last(self, user_id: str) -> str:
        row = self.kernel.memory.db.execute(
            "SELECT * FROM tool_journal WHERE user_id = ? AND undone = 0 AND undo_token IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return "Отменять нечего — самостоятельных действий не было."

        tool = self.toolbox().get(row["tool"]) or next(
            (t for module in self.kernel.modules for t in module.tools() if t.name == row["tool"]), None
        )
        if tool is None or tool.undo is None:
            return f"Действие «{row['tool']}» откатить нечем."

        ok = await tool.undo(row["undo_token"])
        if ok:
            self.kernel.memory.db.execute("UPDATE tool_journal SET undone = 1 WHERE id = ?", (row["id"],))
            self.kernel.memory.db.commit()
            return f"Отменила: {row['tool']} — {row['result'][:120]}"
        return f"Не получилось отменить «{row['tool']}»: похоже, уже отменено."
