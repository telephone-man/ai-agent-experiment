import pytest

from services.voice_gateway.clients import (
    DEFAULT_LLM_MAX_CONCURRENCY,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_ACQUIRE_TIMEOUT_SECONDS,
    DEFAULT_STT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_STT_MAX_CONCURRENCY,
    DEFAULT_TRANSLATION_MAX_CONCURRENCY,
    DEFAULT_TRANSLATION_TIMEOUT_SECONDS,
    DEFAULT_TTS_MAX_CONCURRENCY,
    DEFAULT_TTS_TIMEOUT_SECONDS,
    LLMClient,
    ProviderCapacityError,
    ProviderLimiter,
    STTStreamClient,
    TTSClient,
    TranslationClient,
)


PROVIDER_ENV_VARS = [
    "VOICE_GATEWAY_STT_CONNECT_TIMEOUT_SECONDS",
    "VOICE_GATEWAY_LLM_TIMEOUT_SECONDS",
    "VOICE_GATEWAY_TRANSLATION_TIMEOUT_SECONDS",
    "VOICE_GATEWAY_TTS_TIMEOUT_SECONDS",
    "VOICE_GATEWAY_PROVIDER_ACQUIRE_TIMEOUT_SECONDS",
    "VOICE_GATEWAY_STT_MAX_CONCURRENCY",
    "VOICE_GATEWAY_LLM_MAX_CONCURRENCY",
    "VOICE_GATEWAY_TRANSLATION_MAX_CONCURRENCY",
    "VOICE_GATEWAY_TTS_MAX_CONCURRENCY",
]


def clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_provider_client_defaults_match_documented_env(monkeypatch):
    clear_provider_env(monkeypatch)

    stt = STTStreamClient()
    llm = LLMClient()
    translation = TranslationClient()
    tts = TTSClient()

    assert stt.connect_timeout_seconds == DEFAULT_STT_CONNECT_TIMEOUT_SECONDS
    assert stt.max_concurrency == DEFAULT_STT_MAX_CONCURRENCY
    assert (
        stt.provider_acquire_timeout_seconds == DEFAULT_PROVIDER_ACQUIRE_TIMEOUT_SECONDS
    )

    assert llm.timeout_seconds == DEFAULT_LLM_TIMEOUT_SECONDS
    assert llm.max_concurrency == DEFAULT_LLM_MAX_CONCURRENCY
    assert (
        llm.provider_acquire_timeout_seconds == DEFAULT_PROVIDER_ACQUIRE_TIMEOUT_SECONDS
    )

    assert translation.timeout_seconds == DEFAULT_TRANSLATION_TIMEOUT_SECONDS
    assert translation.max_concurrency == DEFAULT_TRANSLATION_MAX_CONCURRENCY
    assert (
        translation.provider_acquire_timeout_seconds
        == DEFAULT_PROVIDER_ACQUIRE_TIMEOUT_SECONDS
    )

    assert tts.timeout_seconds == DEFAULT_TTS_TIMEOUT_SECONDS
    assert tts.max_concurrency == DEFAULT_TTS_MAX_CONCURRENCY
    assert (
        tts.provider_acquire_timeout_seconds == DEFAULT_PROVIDER_ACQUIRE_TIMEOUT_SECONDS
    )


def test_provider_client_env_overrides_timeout_and_concurrency(monkeypatch):
    monkeypatch.setenv("VOICE_GATEWAY_PROVIDER_ACQUIRE_TIMEOUT_SECONDS", "0.25")
    monkeypatch.setenv("VOICE_GATEWAY_STT_CONNECT_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("VOICE_GATEWAY_STT_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("VOICE_GATEWAY_LLM_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("VOICE_GATEWAY_LLM_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("VOICE_GATEWAY_TRANSLATION_TIMEOUT_SECONDS", "14.5")
    monkeypatch.setenv("VOICE_GATEWAY_TRANSLATION_MAX_CONCURRENCY", "4")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_MAX_CONCURRENCY", "6")

    stt = STTStreamClient()
    llm = LLMClient()
    translation = TranslationClient()
    tts = TTSClient()

    assert stt.connect_timeout_seconds == 3.5
    assert stt.max_concurrency == 2
    assert stt.provider_acquire_timeout_seconds == 0.25

    assert llm.timeout_seconds == 12.5
    assert llm.max_concurrency == 3
    assert llm.provider_acquire_timeout_seconds == 0.25

    assert translation.timeout_seconds == 14.5
    assert translation.max_concurrency == 4
    assert translation.provider_acquire_timeout_seconds == 0.25

    assert tts.timeout_seconds == 5.5
    assert tts.max_concurrency == 6
    assert tts.provider_acquire_timeout_seconds == 0.25


@pytest.mark.asyncio
async def test_provider_limiter_raises_capacity_error_when_full():
    limiter = ProviderLimiter("llm", max_concurrency=1, acquire_timeout_seconds=0.01)

    async with limiter:
        with pytest.raises(ProviderCapacityError) as exc_info:
            async with limiter:
                pass

    error = exc_info.value
    assert error.provider == "llm"
    assert error.max_concurrency == 1
    assert error.acquire_timeout_seconds == 0.01
