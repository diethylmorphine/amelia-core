"""Кузница: ядро само себе дописывает навыки.

Конвейер строго последовательный, и на каждом шаге можно остановиться:

  запрос → модель пишет JSON → AST-валидатор → файл на диск (status=pending)
         → импорт в отдельном процессе под таймаутом → одобрение человеком
         → горячая перезагрузка реестра → навык живой

Ничего не активируется само, пока auto_approve выключен. Удаление —
не удаление, а перенос в skills/_trash, чтобы можно было откатиться.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .templates import repair_prompt, system_prompt, user_prompt
from .validator import validate_skill_code

if TYPE_CHECKING:
    from ..kernel import Kernel

log = logging.getLogger(__name__)

SKILLS_PACKAGE = "amelia.skills"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
PROBE_TIMEOUT = 20.0


@dataclass(slots=True)
class ForgeResult:
    ok: bool
    message: str
    name: str = ""
    status: str = ""
    path: Path | None = None
    intents: list[str] = field(default_factory=list)
    attempts: int = 0


class Forge:
    def __init__(self, kernel: "Kernel") -> None:
        self.kernel = kernel
        self.root = Path(__file__).resolve().parent.parent.parent  # amelia/
        self.skills_dir = self.root / "skills"
        self.trash_dir = self.skills_dir / "_trash"
        self.project_root = self.root.parent
        self.audit_path = Path(kernel.config.db_path).parent / "forge.jsonl"

    # ------------------------------------------------------------------ #
    # Создание
    # ------------------------------------------------------------------ #
    async def create(self, request_text: str) -> ForgeResult:
        if not self.kernel.config.forge_enabled:
            return ForgeResult(False, "Кузница выключена (AMELIA_FORGE_ENABLED=false)")
        if not await self.kernel.llm.available():
            return ForgeResult(False, "Модель недоступна — писать навык нечем")

        extra = set(self.kernel.config.forge_extra_imports)
        system = system_prompt(extra)
        prompt = user_prompt(request_text, self.kernel.modules.catalog())
        problems: list[str] = []

        for attempt in range(1, self.kernel.config.forge_attempts + 1):
            full_prompt = prompt if attempt == 1 else f"{prompt}\n\n{repair_prompt(problems)}"
            raw = await self.kernel.llm.complete(full_prompt, system=system)

            draft, problems = self._parse(raw)
            if problems:
                log.info("Попытка %d: модель вернула мусор — %s", attempt, problems)
                continue

            report = validate_skill_code(draft["code"], extra)
            problems = list(report.problems)
            if report.skill_name != draft["name"]:
                problems.append(f"name в коде ({report.skill_name!r}) не совпадает с JSON ({draft['name']!r})")
            if problems:
                log.info("Попытка %d: код не прошёл валидацию — %s", attempt, problems)
                continue

            result = await self._install(draft, request_text, attempt)
            await self._audit("create", asdict(result) | {"request": request_text})
            return result

        message = "Не получилось написать рабочий навык:\n" + "\n".join(f"  • {p}" for p in problems)
        await self._audit("create_failed", {"request": request_text, "problems": problems})
        return ForgeResult(False, message, attempts=self.kernel.config.forge_attempts)

    async def _install(self, draft: dict, request_text: str, attempt: int) -> ForgeResult:
        name = draft["name"]
        path = self.skills_dir / f"{name}.py"
        path.write_text(draft["code"].rstrip() + "\n", encoding="utf-8")

        manifest = {
            "name": name,
            "description": draft["description"],
            "intents": draft["intents"],
            "triggers": draft.get("triggers", []),
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request": request_text,
            "attempts": attempt,
        }
        self._write_manifest(name, manifest)

        probe_ok, probe_output = await self._probe(name)
        if not probe_ok:
            manifest["status"] = "broken"
            manifest["error"] = probe_output
            self._write_manifest(name, manifest)
            return ForgeResult(
                False,
                f"Навык «{name}» написан, но не пережил проверочный импорт:\n  {probe_output}\n"
                f"Файл оставлен для разбора: {path}",
                name=name, status="broken", path=path,
            )

        if self.kernel.config.forge_auto_approve:
            return await self.approve(name)

        preview = "\n".join(draft["code"].splitlines()[:20])
        return ForgeResult(
            True,
            f"Навык «{name}» готов и ждёт одобрения.\n"
            f"  что делает: {draft['description']}\n"
            f"  интенты: {', '.join(draft['intents'])}\n"
            f"  файл: {path}\n\n"
            f"Первые строки:\n{preview}\n...\n\n"
            f"Посмотри код и скажи «включи навык {name}», чтобы активировать.",
            name=name, status="pending", path=path, intents=draft["intents"], attempts=attempt,
        )

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    async def approve(self, name: str) -> ForgeResult:
        manifest = self._read_manifest(name)
        if manifest is None:
            return ForgeResult(False, f"Навыка «{name}» нет")
        if manifest.get("status") == "broken":
            return ForgeResult(False, f"Навык «{name}» помечен как сломанный — включать нечего")

        manifest["status"] = "enabled"
        manifest["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write_manifest(name, manifest)
        await self.kernel.reload_modules()
        await self._audit("approve", {"name": name})

        loaded = self.kernel.modules.by_name(manifest["name"]) is not None
        if not loaded:
            return ForgeResult(False, f"Навык «{name}» включён, но реестр его не подхватил — смотри логи", name=name)
        return ForgeResult(
            True,
            f"Навык «{name}» включён. Триггеры: {', '.join(manifest.get('triggers') or manifest['intents'])}",
            name=name, status="enabled", intents=manifest["intents"],
        )

    async def disable(self, name: str) -> ForgeResult:
        manifest = self._read_manifest(name)
        if manifest is None:
            return ForgeResult(False, f"Навыка «{name}» нет")
        manifest["status"] = "disabled"
        self._write_manifest(name, manifest)
        await self.kernel.reload_modules()
        await self._audit("disable", {"name": name})
        return ForgeResult(True, f"Навык «{name}» отключён. Код на месте — можно включить обратно.", name=name)

    async def drop(self, name: str) -> ForgeResult:
        path = self.skills_dir / f"{name}.py"
        if not path.exists():
            return ForgeResult(False, f"Навыка «{name}» нет")
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path.rename(self.trash_dir / f"{name}.{stamp}.py")
        manifest_path = self.skills_dir / f"{name}.json"
        if manifest_path.exists():
            manifest_path.rename(self.trash_dir / f"{name}.{stamp}.json")
        await self.kernel.reload_modules()
        await self._audit("drop", {"name": name})
        return ForgeResult(True, f"Навык «{name}» убран в {self.trash_dir.name}/ — оттуда его можно достать.", name=name)

    def catalog(self) -> list[dict]:
        items = []
        for manifest_path in sorted(self.skills_dir.glob("*.json")):
            try:
                items.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return items

    def source_of(self, name: str) -> str | None:
        path = self.skills_dir / f"{name}.py"
        return path.read_text(encoding="utf-8") if path.exists() else None

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #
    def _parse(self, raw: str) -> tuple[dict, list[str]]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text.rstrip())
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {}, ["Модель вернула не JSON"]
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            return {}, [f"JSON не разобрался: {exc}"]

        problems: list[str] = []
        for key in ("name", "description", "intents", "code"):
            if not data.get(key):
                problems.append(f"В JSON нет ключа {key}")
        if problems:
            return {}, problems

        name = str(data["name"]).strip().lower()
        if not NAME_PATTERN.match(name):
            problems.append(f"Недопустимое имя навыка: {name!r}")
        if (self.skills_dir / f"{name}.py").exists():
            problems.append(f"Навык «{name}» уже существует")
        if self.kernel.modules.by_name(name):
            problems.append(f"Имя «{name}» занято встроенным модулем")
        if not isinstance(data["intents"], list) or not data["intents"]:
            problems.append("intents должен быть непустым массивом строк")

        data["name"] = name
        return (data, problems) if not problems else ({}, problems)

    async def _probe(self, name: str) -> tuple[bool, str]:
        """Первый импорт кода — в чужом процессе и под таймаутом."""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "amelia.core.forge.probe", f"{SKILLS_PACKAGE}.{name}",
            cwd=str(self.project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return False, f"импорт завис дольше {PROBE_TIMEOUT:.0f} с — похоже на цикл или сетевой вызов"
        output = (stdout or b"").decode().strip() or (stderr or b"").decode().strip()
        return process.returncode == 0, output[:500]

    def _manifest_path(self, name: str) -> Path:
        return self.skills_dir / f"{name}.json"

    def _read_manifest(self, name: str) -> dict | None:
        path = self._manifest_path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _write_manifest(self, name: str, manifest: dict) -> None:
        self._manifest_path(name).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def _audit(self, event: str, payload: dict) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **{k: (str(v) if isinstance(v, Path) else v) for k, v in payload.items()},
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.warning("Не удалось записать журнал кузницы")
