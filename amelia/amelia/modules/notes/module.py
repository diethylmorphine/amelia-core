"""Навык заметок: кладёт запись в память и опционально в Obsidian-хранилище."""

from __future__ import annotations

import re
from datetime import datetime

from ...core.agent import Tool, ToolResult
from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.logic.rules import strip_trigger
from ...core.module import Module

TAG_PATTERN = re.compile(r"#([\w\-]+)", re.UNICODE)


class NotesModule(Module):
    name = "notes"
    description = "Сохранить заметку в память (и в Obsidian)"
    intents = (Intent.NOTE,)
    priority = 20

    async def handle(self, request: Request, intent: str) -> Response:
        text = strip_trigger(request.text)
        if not text:
            return self.reply("Что именно записать?", intent)

        tags = TAG_PATTERN.findall(text)
        memory_id = await self.kernel.memory.remember(
            text, source=request.source, user_id=request.user_id, tags=tags
        )
        saved_to = self._write_vault(text, tags)
        suffix = f" → {saved_to}" if saved_to else ""
        return self.reply(f"Записал (#{memory_id}){suffix}", intent, memory_id=memory_id)

    def _write_vault(self, text: str, tags: list[str]) -> str | None:
        vault = self.kernel.config.vault_path
        if not vault:
            return None
        try:
            folder = vault / "inbox"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            path = folder / f"{stamp}.md"
            front = "\n".join(f"  - {tag}" for tag in tags)
            header = f"---\ncreated: {datetime.now().isoformat(timespec='seconds')}\ntags:\n{front}\n---\n\n"
            path.write_text(header + text + "\n", encoding="utf-8")
            return str(path)
        except OSError:
            return None

    # ------------------------------------------------------------------ #
    def tools(self) -> list[Tool]:
        return [Tool(
            name="save_note",
            description="Сохранить факт или заметку в долговременную память",
            args={"text": "что запомнить"},
            run=self._tool_save,
            mutates=True,
            undo=self._undo_save,
        )]

    async def _tool_save(self, text: str = "", user_id: str = "local") -> ToolResult:
        if not text.strip():
            return ToolResult("пустой текст")
        tags = TAG_PATTERN.findall(text)
        memory_id = await self.kernel.memory.remember(
            text, source="agent", user_id=user_id, tags=tags
        )
        self._write_vault(text, tags)
        return ToolResult(f"сохранено #{memory_id}", undo=str(memory_id))

    async def _undo_save(self, token: str) -> bool:
        try:
            return await self.kernel.memory.forget(int(token))
        except (TypeError, ValueError):
            return False
