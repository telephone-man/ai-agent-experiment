"""Shared environment configuration helpers."""

from __future__ import annotations

import os


TRUE_VALUES = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in TRUE_VALUES


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default


def openai_ready() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def offline_fallback_enabled() -> bool:
    return env_bool("AI_OFFLINE_FALLBACK", False)


def ai_service_ready() -> bool:
    return openai_ready() or offline_fallback_enabled()


def ai_service_degraded_payload() -> dict[str, str]:
    return {"status": "degraded", "detail": "OPENAI_API_KEY is required"}
