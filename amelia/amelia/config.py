"""Конфигурация. Читается из окружения и .env, без внешних зависимостей."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timezone, tzinfo
from pathlib import Path

log = logging.getLogger(__name__)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный парсер .env — чтобы не тащить python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(value: str, fallback: int, *, low: int | None = None, high: int | None = None) -> int:
    """Число из окружения. Мусор не должен ронять службу.

    Под systemd падение на старте — это рестарт-петля: опечатка в .env
    превращается в бесконечно перезапускающийся сервис.
    """
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        log.warning("Некорректное число %r — беру %s", value, fallback)
        return fallback
    if low is not None and parsed < low:
        return low
    if high is not None and parsed > high:
        return high
    return parsed


def _float(value: str, fallback: float, *, low: float | None = None) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        log.warning("Некорректное число %r — беру %s", value, fallback)
        return fallback
    return max(parsed, low) if low is not None else parsed


@dataclass(slots=True)
class Config:
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    embed_model: str = "bge-m3"
    # --- Сэмплирование ---
    # Дефолт Ollama — temperature 0.8. Для маленького Qwen это главная
    # причина китайских вставок: чем выше температура, тем охотнее модель
    # уходит в язык, которого больше всего в её обучении.
    llm_temperature: float = 0.3
    llm_top_p: float = 0.9
    llm_min_p: float = 0.05           # отсекает маловероятные токены надёжнее, чем top_k
    llm_repeat_penalty: float = 1.1
    # --- Язык ответа ---
    response_language: str = "auto"   # auto | ru | en
    strip_foreign_scripts: bool = True
    db_path: Path = ROOT / "data" / "amelia.db"
    vault_path: Path = ROOT / "vault"
    bot_token: str = ""
    owner_id: str = ""
    log_level: str = "INFO"
    # --- Кузница: самописные навыки ---
    forge_enabled: bool = True
    forge_auto_approve: bool = False      # включать навык без спроса — не рекомендуется
    forge_attempts: int = 3               # попыток переписать код после ошибок валидатора
    forge_extra_imports: tuple[str, ...] = ()   # чем расширить белый список импортов
    # --- Сессии: диалоговый контекст ---
    session_window: int = 12              # сколько реплик держать в промпте
    session_compact_after: int = 24       # после скольких реплик сжимать хвост в выжимку
    # --- Агент: модули как инструменты ---
    agent_enabled: bool = True
    agent_max_steps: int = 4              # потолок вызовов инструментов на один вопрос
    agent_allow_mutations: bool = True    # можно ли агенту менять данные самому
    # --- Планировщик ---
    scheduler_enabled: bool = True
    scheduler_tick: float = 5.0           # секунд между проверками очереди
    timezone_name: str = "Europe/Warsaw"  # реальная зона: знает про переход на зимнее время
    # --- Наблюдаемость ---
    trace_enabled: bool = True
    # --- Круглосуточный режим ---
    health_port: int = 8787               # 0 = не поднимать эндпоинт
    health_host: str = "127.0.0.1"
    upkeep_enabled: bool = True
    upkeep_hour: int = 4                  # локальный час ночного обслуживания
    backup_keep: int = 14                 # сколько копий базы хранить
    log_keep_lines: int = 20000           # строк в jsonl-журналах после подрезки
    backup_command: str = ""              # выгрузка копии наружу, {path} = файл
    llm_probe_ttl: float = 30.0           # секунд между перепроверками модели

    @property
    def sampling(self) -> dict:
        """Параметры генерации для Ollama."""
        return {
            "temperature": self.llm_temperature,
            "top_p": self.llm_top_p,
            "min_p": self.llm_min_p,
            "repeat_penalty": self.llm_repeat_penalty,
        }

    @property
    def tz(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.utc

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        _load_dotenv(env_file or ROOT / ".env")
        env = os.environ.get
        return cls(
            ollama_url=env("AMELIA_OLLAMA_URL", cls.ollama_url),
            llm_model=env("AMELIA_LLM_MODEL", cls.llm_model),
            embed_model=env("AMELIA_EMBED_MODEL", cls.embed_model),
            llm_temperature=_float(env("AMELIA_TEMPERATURE", "0.3"), 0.3, low=0.0),
            llm_top_p=_float(env("AMELIA_TOP_P", "0.9"), 0.9, low=0.0),
            llm_min_p=_float(env("AMELIA_MIN_P", "0.05"), 0.05, low=0.0),
            llm_repeat_penalty=_float(env("AMELIA_REPEAT_PENALTY", "1.1"), 1.1, low=1.0),
            response_language=env("AMELIA_RESPONSE_LANGUAGE", "auto"),
            strip_foreign_scripts=_flag(env("AMELIA_STRIP_FOREIGN", "true")),
            db_path=Path(env("AMELIA_DB_PATH", str(cls.db_path))),
            vault_path=Path(env("AMELIA_VAULT_PATH", str(cls.vault_path))),
            bot_token=env("AMELIA_BOT_TOKEN", ""),
            owner_id=env("AMELIA_OWNER_ID", ""),
            log_level=env("AMELIA_LOG_LEVEL", "INFO"),
            forge_enabled=_flag(env("AMELIA_FORGE_ENABLED", "true")),
            forge_auto_approve=_flag(env("AMELIA_FORGE_AUTO_APPROVE", "false")),
            forge_attempts=_int(env("AMELIA_FORGE_ATTEMPTS", "3"), 3, low=1, high=10),
            forge_extra_imports=tuple(
                part.strip() for part in env("AMELIA_FORGE_EXTRA_IMPORTS", "").split(",") if part.strip()
            ),
            session_window=_int(env("AMELIA_SESSION_WINDOW", "12"), 12, low=1, high=200),
            session_compact_after=_int(env("AMELIA_SESSION_COMPACT_AFTER", "24"), 24, low=2, high=1000),
            agent_enabled=_flag(env("AMELIA_AGENT_ENABLED", "true")),
            agent_max_steps=_int(env("AMELIA_AGENT_MAX_STEPS", "4"), 4, low=1, high=20),
            agent_allow_mutations=_flag(env("AMELIA_AGENT_ALLOW_MUTATIONS", "true")),
            scheduler_enabled=_flag(env("AMELIA_SCHEDULER_ENABLED", "true")),
            scheduler_tick=_float(env("AMELIA_SCHEDULER_TICK", "5"), 5.0, low=0.05),
            timezone_name=env("AMELIA_TZ", "Europe/Warsaw"),
            trace_enabled=_flag(env("AMELIA_TRACE_ENABLED", "true")),
            health_port=_int(env("AMELIA_HEALTH_PORT", "8787"), 8787, low=0, high=65535),
            health_host=env("AMELIA_HEALTH_HOST", "127.0.0.1"),
            upkeep_enabled=_flag(env("AMELIA_UPKEEP_ENABLED", "true")),
            upkeep_hour=_int(env("AMELIA_UPKEEP_HOUR", "4"), 4, low=0, high=23),
            backup_keep=_int(env("AMELIA_BACKUP_KEEP", "14"), 14, low=1, high=365),
            log_keep_lines=_int(env("AMELIA_LOG_KEEP_LINES", "20000"), 20000, low=100),
            backup_command=env("AMELIA_BACKUP_COMMAND", ""),
            llm_probe_ttl=_float(env("AMELIA_LLM_PROBE_TTL", "30"), 30.0, low=1.0),
        )
