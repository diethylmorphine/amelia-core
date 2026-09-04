"""Живучесть под systemd: сторожевой таймер и точка проверки здоровья.

Процесс, который работает месяцами, обязан уметь две вещи: сообщать наружу,
что он ещё жив, и внятно отвечать на вопрос «что с тобой». Первое делает
sd_notify (systemd перезапустит зависший процесс), второе — крошечный
HTTP-эндпоинт на localhost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def sd_notify(state: str) -> bool:
    """Сообщение systemd через NOTIFY_SOCKET. Вне systemd — тихо ничего."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError:
        return False


class Health:
    def __init__(self, kernel, host: str = "127.0.0.1", port: int = 8787) -> None:
        self.kernel = kernel
        self.host = host
        self.port = port
        self.started_at = time.monotonic()
        self._server: asyncio.AbstractServer | None = None
        self._beat: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        sd_notify("READY=1")
        interval = self._watchdog_interval()
        if interval:
            self._beat = asyncio.create_task(self._heartbeat(interval))
            log.info("Сторожевой таймер systemd: пинг каждые %.0f с", interval)
        if self.port:
            try:
                self._server = await asyncio.start_server(self._serve, self.host, self.port)
                log.info("Проверка здоровья: http://%s:%d/health", self.host, self.port)
            except OSError as exc:
                log.warning("Не удалось поднять эндпоинт здоровья: %s", exc)

    async def stop(self) -> None:
        sd_notify("STOPPING=1")
        if self._beat:
            self._beat.cancel()
            try:
                await self._beat
            except asyncio.CancelledError:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _watchdog_interval() -> float:
        """systemd задаёт WATCHDOG_USEC; пингуем вдвое чаще, чем требуется."""
        raw = os.environ.get("WATCHDOG_USEC")
        if not raw or os.environ.get("WATCHDOG_PID", str(os.getpid())) != str(os.getpid()):
            return 0.0
        try:
            return max(int(raw) / 1_000_000 / 2, 1.0)
        except ValueError:
            return 0.0

    async def _heartbeat(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            sd_notify("WATCHDOG=1")

    async def snapshot(self) -> dict:
        kernel = self.kernel
        uptime = int(time.monotonic() - self.started_at)
        try:
            memories = await kernel.memory.count()
            pending = await kernel.scheduler.count_pending()
            db_ok = True
        except Exception:
            memories, pending, db_ok = -1, -1, False
        return {
            "status": "ok" if db_ok else "degraded",
            "uptime_s": uptime,
            "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "llm": await kernel.llm.available(),
            "model": kernel.config.llm_model,
            "modules": len(kernel.modules),
            "tools": len(kernel.agent.toolbox()),
            "memories": memories,
            "jobs_pending": pending,
            "requests": kernel.tracer.counters.get("requests", 0),
        }

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readline(), timeout=5.0)
            payload = json.dumps(await self.snapshot(), ensure_ascii=False).encode("utf-8")
            code = b"200 OK" if b'"status": "ok"' in payload else b"503 Service Unavailable"
            writer.write(
                b"HTTP/1.1 " + code + b"\r\nContent-Type: application/json; charset=utf-8\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload
            )
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
