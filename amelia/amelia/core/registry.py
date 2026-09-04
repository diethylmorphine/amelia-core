"""Реестр модулей: находит навыки сам, ядро не знает их поимённо.

Два источника:
  amelia.modules — встроенные, грузятся всегда;
  amelia.skills  — написанные Кузницей, грузятся только со status == "enabled"
                   в манифесте <имя>.json рядом с файлом.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
import sys
from pathlib import Path
from typing import Iterator

from .context import Request
from .module import Module

log = logging.getLogger(__name__)

BUILTIN_PACKAGE = "amelia.modules"
SKILLS_PACKAGE = "amelia.skills"


class ModuleRegistry:
    def __init__(self, packages: tuple[str, ...] = (BUILTIN_PACKAGE, SKILLS_PACKAGE)) -> None:
        self.packages = packages
        self._modules: list[Module] = []

    def __iter__(self) -> Iterator[Module]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)

    def discover(self, *, reload: bool = False) -> list[Module]:
        self._modules.clear()
        if reload:
            importlib.invalidate_caches()
        for package in self.packages:
            self._load_package(package, reload=reload)
        self._modules.sort(key=lambda module: module.priority)
        log.info("Модули: %s", ", ".join(m.name for m in self._modules) or "—")
        return self._modules

    def _load_package(self, package: str, *, reload: bool) -> None:
        try:
            pkg = importlib.import_module(package)
        except ImportError:
            log.warning("Пакет %s не найден", package)
            return

        gated = package == SKILLS_PACKAGE
        base = Path(pkg.__path__[0])

        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("_"):
                continue
            if gated and not self._is_enabled(base / f"{info.name}.json"):
                continue

            full_name = f"{package}.{info.name}"
            try:
                if reload and full_name in sys.modules:
                    imported = importlib.reload(sys.modules[full_name])
                else:
                    imported = importlib.import_module(full_name)
            except Exception:
                log.exception("Не удалось загрузить %s", full_name)
                continue

            for _, obj in inspect.getmembers(imported, inspect.isclass):
                if issubclass(obj, Module) and obj is not Module and not inspect.isabstract(obj):
                    if any(existing.name == getattr(obj, "name", "") for existing in self._modules):
                        log.warning("Имя навыка %s уже занято — пропускаю %s", obj.name, full_name)
                        continue
                    instance = obj()
                    instance._origin = package
                    self._modules.append(instance)

    @staticmethod
    def _is_enabled(manifest_path: Path) -> bool:
        if not manifest_path.exists():
            return False  # без манифеста сгенерированный код не грузим
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8")).get("status") == "enabled"
        except (OSError, json.JSONDecodeError):
            return False

    def pick(self, request: Request, intent: str) -> Module | None:
        for module in self._modules:
            if module.can_handle(request, intent):
                return module
        return None

    def is_generated(self, module: Module) -> bool:
        """Навык написан кузницей — значит, ядро ему даётся урезанное."""
        return getattr(module, "_origin", "") == SKILLS_PACKAGE

    def drop(self, module: Module) -> None:
        """Убрать модуль из реестра — например, когда он упал на setup."""
        if module in self._modules:
            self._modules.remove(module)

    def by_name(self, name: str) -> Module | None:
        return next((m for m in self._modules if m.name == name), None)

    def catalog(self) -> dict[str, str]:
        return {m.name: m.description for m in self._modules}
