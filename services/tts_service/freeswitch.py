"""FreeSWITCH ESL command construction for local TTS playback."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TTSConfig:
    engine: str = "piper"
    voice: str = "en"
    alt_engine: str | None = None
    alt_voice: str | None = None

    @classmethod
    def from_env(cls, language: str | None = None) -> "TTSConfig":
        engine = _env_for_language("FREESWITCH_TTS_ENGINE", language)
        voice = _env_for_language("FREESWITCH_TTS_VOICE", language)
        return cls(
            engine=(engine or os.getenv("FREESWITCH_TTS_ENGINE") or "piper").strip()
            or "piper",
            voice=(voice or os.getenv("FREESWITCH_TTS_VOICE") or "en").strip() or "en",
            alt_engine=(os.getenv("FREESWITCH_TTS_ALT_ENGINE") or "").strip() or None,
            alt_voice=(os.getenv("FREESWITCH_TTS_ALT_VOICE") or "").strip() or None,
        )


def _env_for_language(name: str, language: str | None) -> str | None:
    suffixes = _language_suffixes(language)
    for suffix in suffixes:
        value = os.getenv(f"{name}{suffix}")
        if value:
            return value
    return None


def _language_suffixes(language: str | None) -> list[str]:
    clean = (language or "").strip()
    if not clean:
        return []
    exact = clean.upper().replace("-", "_")
    suffixes = [f"_{exact}"]
    base = exact.split("_", 1)[0]
    if base and base != exact:
        suffixes.append(f"_{base}")
    return suffixes


def clean_tts_text(text: str) -> str:
    return " ".join((text or "").replace("|", " ").split())


def tts_variants(text: str, config: TTSConfig | None = None) -> list[str]:
    config = config or TTSConfig.from_env()
    clean = clean_tts_text(text)
    variants = [f"{config.engine}|{config.voice}|{clean}"]
    alt_engine = config.alt_engine or config.engine
    alt_voice = config.alt_voice or config.voice
    alt = f"{alt_engine}|{alt_voice}|{clean}"
    if alt not in variants:
        variants.append(alt)
    return variants


def build_sendmsg_payload(
    fs_uuid: str,
    app: str,
    arg: str,
    *,
    lock: bool = True,
    event_uuid: str | None = None,
) -> str:
    lines = [
        f"sendmsg {fs_uuid}",
        "call-command: execute",
        f"execute-app-name: {app}",
        f"execute-app-arg: {arg}",
    ]
    if event_uuid:
        lines.append(f"Event-UUID: {event_uuid}")
    if lock:
        lines.append("event-lock: true")
    return "\n".join(lines) + "\n\n"
