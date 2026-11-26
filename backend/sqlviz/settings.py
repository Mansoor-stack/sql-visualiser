"""Runtime configuration, read from environment variables (see README "Configuration")."""
from __future__ import annotations

import os
from dataclasses import dataclass

AI_PROVIDERS = ("none", "ollama", "openai-compatible")
AI_DEFAULT_URLS = {"ollama": "http://127.0.0.1:11434", "openai-compatible": "http://127.0.0.1:8000/v1"}
AI_DEFAULT_MODELS = {"ollama": "qwen2.5:7b", "openai-compatible": "local-model"}


class ConfigError(ValueError):
    pass


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}.")
    return value


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    ssl_ca: str | None
    query_timeout_ms: int


@dataclass(frozen=True)
class AISettings:
    provider: str
    base_url: str
    model: str
    api_key: str | None

    @property
    def enabled(self) -> bool:
        return self.provider != "none"


def mysql() -> MySQLSettings:
    return MySQLSettings(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=_int("MYSQL_PORT", 3306, 1, 65535),
        user=os.getenv("MYSQL_USER", "visualizer"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        ssl_ca=os.getenv("MYSQL_SSL_CA") or None,
        query_timeout_ms=_int("SQLVIZ_QUERY_TIMEOUT_MS", 2500, 100, 60_000),
    )


def import_ssl() -> dict[str, str] | None:
    ca = os.getenv("SQLVIZ_IMPORT_SSL_CA")
    return {"ca": ca} if ca else None


def ai() -> AISettings:
    provider = os.getenv("AI_PROVIDER", "none").strip().lower() or "none"
    if provider not in AI_PROVIDERS:
        raise ConfigError(f"AI_PROVIDER must be one of {', '.join(AI_PROVIDERS)}, got {provider!r}.")
    return AISettings(
        provider=provider,
        base_url=(os.getenv("AI_BASE_URL") or AI_DEFAULT_URLS.get(provider, "")).rstrip("/"),
        model=os.getenv("AI_MODEL") or AI_DEFAULT_MODELS.get(provider, ""),
        api_key=os.getenv("AI_API_KEY") or None,
    )


def limits() -> dict[str, int]:
    return dict(
        max_rows=_int("SQLVIZ_MAX_ROWS", 1000, 10, 100_000),
        max_steps=_int("SQLVIZ_MAX_STEPS", 150, 10, 1000),
        max_tables=_int("SQLVIZ_MAX_TABLES", 20, 1, 200),
        max_upload_bytes=_int("SQLVIZ_MAX_UPLOAD_BYTES", 2_000_000, 10_000, 100_000_000),
    )
