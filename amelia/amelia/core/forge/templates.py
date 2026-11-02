"""Промпт для генерации навыка и контракт, который модель обязана соблюсти."""

from __future__ import annotations

from .validator import ALLOWED_IMPORTS

EXAMPLE = '''
from __future__ import annotations

import re

from ..core.context import Request, Response
from ..core.module import Module


class TipsModule(Module):
    name = "tips"
    description = "Считает чаевые от суммы счёта"
    intents = ("tips",)
    priority = 100

    async def handle(self, request: Request, intent: str) -> Response:
        numbers = re.findall(r"\\d+(?:[.,]\\d+)?", request.text)
        if not numbers:
            return self.reply("Не вижу суммы счёта.", intent)
        total = float(numbers[0].replace(",", "."))
        percent = float(numbers[1].replace(",", ".")) if len(numbers) > 1 else 10.0
        tip = total * percent / 100
        return self.reply(f"Чаевые {percent:.0f}%: {tip:.2f} (итого {total + tip:.2f})", intent)
'''.strip()


def system_prompt(extra_imports: set[str] | frozenset[str] = frozenset()) -> str:
    allowed = ", ".join(sorted(ALLOWED_IMPORTS | set(extra_imports)))
    return f"""Ты пишешь новый навык для локального ассистента АМЕЛИЯ на Python 3.11+.

Верни РОВНО один JSON-объект, без markdown-обёрток и пояснений, с ключами:
  "name"        — короткий идентификатор навыка: латиница в нижнем регистре, цифры, _
  "description" — одна строка по-русски, что навык делает
  "intents"     — массив строк-идентификаторов (латиница, нижний регистр)
  "triggers"    — массив русских ключевых слов/фраз, по которым навык вызывается
  "code"        — полный исходник файла навыка

Жёсткие требования к коду:
1. Ровно один класс — наследник Module, с атрибутами name, description, intents (кортеж) и async def handle(self, request, intent) -> Response.
2. Импорты Module/Request/Response — относительные: from ..core.module import Module
3. Разрешены только эти внешние модули: {allowed}
   Запрещено всё остальное, включая os, sys, subprocess, shutil, socket, importlib, pickle, threading.
4. Запрещены eval, exec, compile, __import__, open, input, globals, locals, setattr, delattr и доступ к дандер-атрибутам (__globals__, __subclasses__ и т.п.).
5. На верхнем уровне файла — только импорты, константы и объявления. Никакого исполняемого кода при импорте: ни запросов, ни чтения файлов, ни принтов.
6. Ответ строится только через self.reply(текст, intent). Никаких print.
7. Метод handle обязан быть async и не должен блокировать цикл событий: тяжёлое — через await asyncio.to_thread(...).
8. Доступное изнутри: self.kernel.memory (remember/recall), self.kernel.llm (complete/embed), self.kernel.config. Шины событий у сгенерированного навыка нет. Ходить в сеть без явной просьбы пользователя не нужно.
9. Код должен работать без внешних сервисов: если чего-то нет — вернуть понятное сообщение, а не падать.
10. Обрабатывай кривой ввод: пустой текст, отсутствие чисел, неверный формат.

Пример корректного навыка:

{EXAMPLE}
"""


def user_prompt(request_text: str, existing: dict[str, str]) -> str:
    known = ", ".join(existing) or "нет"
    return (
        f"Уже существующие навыки (имена занимать нельзя): {known}\n\n"
        f"Запрос пользователя: {request_text}\n\n"
        f"Спроектируй и напиши навык, который его закрывает. Верни только JSON."
    )


def repair_prompt(problems: list[str]) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        "Предыдущая версия не прошла проверку:\n"
        f"{listed}\n\n"
        "Исправь и верни JSON целиком заново, в том же формате."
    )
