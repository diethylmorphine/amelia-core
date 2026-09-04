"""Проверочный запуск навыка в отдельном процессе.

Импорт сгенерированного кода — это исполнение сгенерированного кода.
Поэтому первый импорт происходит здесь, в чужом процессе, под таймаутом:
если код зациклится или упадёт, ядро этого даже не заметит.

Запуск: python -m amelia.core.forge.probe amelia.skills.<имя>
"""

from __future__ import annotations

import importlib
import inspect
import sys

from ..module import Module


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: probe <module.path>", file=sys.stderr)
        return 2

    try:
        module = importlib.import_module(sys.argv[1])
    except Exception as exc:
        print(f"импорт упал: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    classes = [
        obj for obj in vars(module).values()
        if inspect.isclass(obj) and issubclass(obj, Module)
        and obj is not Module and not inspect.isabstract(obj)
    ]
    if len(classes) != 1:
        print(f"ожидался один класс Module, найдено {len(classes)}", file=sys.stderr)
        return 1

    try:
        instance = classes[0]()
    except Exception as exc:
        print(f"конструктор упал: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not instance.name or not instance.intents:
        print("у навыка пустые name или intents", file=sys.stderr)
        return 1

    print(f"OK {instance.name} :: {','.join(instance.intents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
