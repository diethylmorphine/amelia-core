"""Самообслуживание: Амелия чинит себя своим же планировщиком.

Всё, что убивает долгоживущий сервис на маленькой машине, — это накопление:
разрастающиеся журналы, распухший WAL, отсутствие копий базы. Ежедневная
задача снимает бэкап, подрезает логи и раз в неделю сжимает базу.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...core.context import Request, Response
from ...core.logic.intents import Intent
from ...core.module import Module

MAINTENANCE_KIND = "maintenance"
DAILY = 86400

log = logging.getLogger(__name__)


class UpkeepModule(Module):
    name = "upkeep"
    description = "Бэкапы, подрезка журналов, обслуживание базы"
    intents = (Intent.UPKEEP,)
    priority = 30

    async def setup(self, kernel) -> None:
        await super().setup(kernel)
        kernel.bus.on("job.fired", self._on_fired)
        if not kernel.config.upkeep_enabled:
            return
        first = self._next_run(kernel.config.upkeep_hour)
        await kernel.scheduler.ensure_recurring(
            "обслуживание", first, kind=MAINTENANCE_KIND, every=DAILY
        )

    def _next_run(self, hour: int) -> datetime:
        zone = self.kernel.config.tz
        local = datetime.now(zone).replace(hour=hour, minute=0, second=0, microsecond=0)
        if local <= datetime.now(zone):
            local += timedelta(days=1)
        return local.astimezone(timezone.utc)

    async def _on_fired(self, job) -> None:
        if job.kind == MAINTENANCE_KIND:
            await self.run()

    # ------------------------------------------------------------------ #
    async def handle(self, request: Request, intent: str) -> Response:
        return self.reply(await self.run(), intent)

    async def run(self) -> str:
        lines: list[str] = []
        data_dir = Path(self.kernel.config.db_path).parent

        backup_path = await self._backup(data_dir)
        lines.append(f"Копия базы: {backup_path.name} "
                     f"({backup_path.stat().st_size / 1024 / 1024:.1f} МБ)")

        offsite = await self._offsite(backup_path)
        if offsite:
            lines.append(f"Наружу: {offsite}")

        trimmed = self._trim_logs(data_dir)
        if trimmed:
            lines.append("Журналы подрезаны: " + ", ".join(trimmed))

        weekly = datetime.now(timezone.utc).weekday() == 6
        size = await self.kernel.memory.maintain(vacuum=weekly)
        lines.append(f"База: {size}" + (" (сжата)" if weekly else ""))

        removed = self._rotate_backups(data_dir / "backups")
        if removed:
            lines.append(f"Старых копий удалено: {removed}")
        return "\n".join(lines)

    async def _backup(self, data_dir: Path) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = data_dir / "backups" / f"amelia-{stamp}.db"
        await self.kernel.memory.backup(target)
        return target

    async def _offsite(self, path: Path) -> str:
        """Копия на том же диске — не копия: умрёт SSD, умрёт всё разом.

        Команда задаётся в настройках, {path} подставляется. Подойдёт что
        угодно: rclone copy {path} remote:amelia, scp, rsync.
        """
        template = self.kernel.config.backup_command.strip()
        if not template:
            return ""
        command = [part.replace("{path}", str(path)) for part in shlex.split(template)]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError:
            process.kill()
            return "не уложилась в 5 минут"
        except (OSError, ValueError) as exc:
            return f"ошибка запуска: {exc}"
        if process.returncode == 0:
            return f"{command[0]} ok"
        detail = (stderr or b"").decode(errors="replace").strip()[:160]
        log.warning("Выгрузка копии не удалась: %s", detail)
        return f"{command[0]} упал: {detail}"

    def _trim_logs(self, data_dir: Path) -> list[str]:
        """Обрезать jsonl-журналы по числу строк, сохранив хвост."""
        keep = self.kernel.config.log_keep_lines
        trimmed: list[str] = []
        for path in (data_dir / "trace.jsonl", data_dir / "forge.jsonl"):
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                if len(lines) <= keep:
                    continue
                path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
                trimmed.append(f"{path.name} {len(lines)}→{keep}")
            except OSError:
                continue
        return trimmed

    def _rotate_backups(self, folder: Path) -> int:
        keep = self.kernel.config.backup_keep
        if not folder.exists():
            return 0
        copies = sorted(folder.glob("amelia-*.db"), key=lambda p: p.name, reverse=True)
        removed = 0
        for old in copies[keep:]:
            try:
                old.unlink()
                removed += 1
            except OSError:
                pass
        return removed
