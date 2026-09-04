"""Статическая проверка кода, который написала модель.

Это не песочница и не гарантия безопасности — это фильтр грубых и явных
опасностей до того, как файл вообще попадёт на диск. Настоящая изоляция
дальше: проверочный запуск в отдельном процессе и ручное одобрение.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

MAX_CHARS = 20_000

# Что можно импортировать сгенерированному навыку.
ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "__future__",
    "re", "json", "math", "random", "statistics", "decimal", "fractions",
    "datetime", "time", "zoneinfo", "calendar",
    "textwrap", "string", "unicodedata", "html", "csv", "difflib",
    "collections", "itertools", "functools", "operator", "heapq", "bisect",
    "typing", "dataclasses", "enum", "abc",
    "hashlib", "hmac", "base64", "uuid", "secrets",
    "asyncio", "urllib", "urllib.request", "urllib.parse", "urllib.error",
    "pathlib", "logging",
})

# Имена, которые превращают питон в оружие.
DENIED_NAMES: frozenset[str] = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "setattr", "delattr", "memoryview",
})

# Пути побега из песочницы через интроспекцию.
DENIED_ATTRS: frozenset[str] = frozenset({
    "__globals__", "__subclasses__", "__bases__", "__code__", "__builtins__",
    "__mro__", "__class__", "__dict__", "__getattribute__", "__reduce__",
    "__loader__", "__spec__",
})

# На верхнем уровне модуля не должно происходить ничего исполняемого:
# импорт навыка не должен иметь побочных эффектов.
ALLOWED_TOPLEVEL = (
    ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef,
    ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign,
)


class ValidationError(Exception):
    pass


@dataclass(slots=True)
class ValidationReport:
    problems: list[str] = field(default_factory=list)
    class_name: str = ""
    skill_name: str = ""
    intents: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def validate_skill_code(code: str, extra_imports: frozenset[str] | set[str] = frozenset()) -> ValidationReport:
    report = ValidationReport()
    allowed = ALLOWED_IMPORTS | set(extra_imports)

    if len(code) > MAX_CHARS:
        report.problems.append(f"Код длиннее {MAX_CHARS} символов")
        return report

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        report.problems.append(f"Синтаксическая ошибка: {exc.msg} (строка {exc.lineno})")
        return report

    # --- верхний уровень: только объявления ---
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # докстринг
        if not isinstance(node, ALLOWED_TOPLEVEL):
            report.problems.append(
                f"На верхнем уровне разрешены только импорты и объявления, "
                f"найдено: {type(node).__name__} (строка {node.lineno})"
            )

    # --- обход всего дерева ---
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in allowed and _root_module(alias.name) not in allowed:
                    report.problems.append(f"Импорт запрещён: {alias.name} (строка {node.lineno})")

        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # относительные импорты внутри amelia — норма
            module = node.module or ""
            if module not in allowed and _root_module(module) not in allowed:
                report.problems.append(f"Импорт запрещён: from {module} (строка {node.lineno})")

        elif isinstance(node, ast.Name) and node.id in DENIED_NAMES:
            report.problems.append(f"Запрещённое имя: {node.id} (строка {node.lineno})")

        elif isinstance(node, ast.Attribute) and node.attr in DENIED_ATTRS:
            report.problems.append(f"Запрещённый атрибут: {node.attr} (строка {node.lineno})")

    # --- контракт модуля ---
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(getattr(base, "id", getattr(base, "attr", "")) == "Module" for base in node.bases)
    ]
    if len(classes) != 1:
        report.problems.append(f"Нужен ровно один класс-наследник Module, найдено {len(classes)}")
        return report

    cls = classes[0]
    report.class_name = cls.name
    report.skill_name = _literal_of(cls, "name") or ""
    report.intents = _list_literal_of(cls, "intents")

    if not report.skill_name:
        report.problems.append("У класса нет строкового атрибута name")
    if not report.intents:
        report.problems.append("У класса нет непустого кортежа intents")

    handlers = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle"]
    if not handlers:
        report.problems.append("Нет метода async def handle(self, request, intent)")

    return report


def _literal_of(cls: ast.ClassDef, attr: str) -> str | None:
    for node in cls.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == attr:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


def _list_literal_of(cls: ast.ClassDef, attr: str) -> list[str]:
    for node in cls.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == attr:
                    if isinstance(node.value, (ast.Tuple, ast.List)):
                        return [
                            element.value for element in node.value.elts
                            if isinstance(element, ast.Constant) and isinstance(element.value, str)
                        ]
    return []
