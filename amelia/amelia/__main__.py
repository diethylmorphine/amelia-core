"""Точка входа: python -m amelia <client>."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from .config import Config
from .core.kernel import Kernel

log = logging.getLogger("amelia")


def main() -> None:
    parser = argparse.ArgumentParser(prog="amelia", description="АМЕЛИЯ — локальное ядро ассистента")
    parser.add_argument("client", nargs="?", default="cli", choices=["cli", "telegram", "service"])
    args = parser.parse_args()

    config = Config.load()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.client == "cli":
        from .clients.cli import CLIClient as ClientCls
    elif args.client == "telegram":
        from .clients.telegram import TelegramClient as ClientCls
    else:
        from .clients.service import ServiceClient as ClientCls

    asyncio.run(_run(config, ClientCls))


async def _run(config: Config, client_cls) -> None:
    kernel = Kernel(config)
    await kernel.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, name), stop.set)

    runner = asyncio.create_task(client_cls(kernel).run())
    waiter = asyncio.create_task(stop.wait())
    try:
        # Что раньше: клиент завершился сам или пришёл сигнал остановки.
        await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (runner, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(runner, waiter, return_exceptions=True)
        log.info("Завершаюсь, закрываю базу")
        await kernel.stop()


if __name__ == "__main__":
    main()
