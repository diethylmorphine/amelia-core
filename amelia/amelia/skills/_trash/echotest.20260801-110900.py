from __future__ import annotations

from ..core.context import Request, Response
from ..core.module import Module


class EchoModule(Module):
    name = "echotest"
    description = "Повторяет сказанное"
    intents = ("echotest",)

    async def handle(self, request: Request, intent: str) -> Response:
        return self.reply(f"эхо: {request.text}", intent)
