"""HTTP and WebSocket clients used by the voice gateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from services.common.config import env_float, env_int
from services.common.freeswitch import inbound_connection


TranscriptCallback = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_STT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
DEFAULT_TRANSLATION_TIMEOUT_SECONDS = 60.0
DEFAULT_TTS_TIMEOUT_SECONDS = 30.0
DEFAULT_PROVIDER_ACQUIRE_TIMEOUT_SECONDS = 2.0
DEFAULT_STT_MAX_CONCURRENCY = 16
DEFAULT_LLM_MAX_CONCURRENCY = 8
DEFAULT_TRANSLATION_MAX_CONCURRENCY = 8
DEFAULT_TTS_MAX_CONCURRENCY = 16


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    provider: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    text: str
    model: str
    provider: str


class ProviderCapacityError(RuntimeError):
    """Raised when a provider client cannot acquire local call capacity."""

    def __init__(
        self, provider: str, *, max_concurrency: int, acquire_timeout_seconds: float
    ) -> None:
        self.provider = provider
        self.max_concurrency = max_concurrency
        self.acquire_timeout_seconds = acquire_timeout_seconds
        super().__init__(
            f"{provider} provider capacity unavailable after "
            f"{acquire_timeout_seconds:g}s with max_concurrency={max_concurrency}"
        )


class ProviderLimiter:
    def __init__(
        self, provider: str, *, max_concurrency: int, acquire_timeout_seconds: float
    ) -> None:
        self.provider = provider
        self.max_concurrency = max(1, int(max_concurrency))
        self.acquire_timeout_seconds = max(0.0, float(acquire_timeout_seconds))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def __aenter__(self) -> "ProviderLimiter":
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.acquire_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ProviderCapacityError(
                self.provider,
                max_concurrency=self.max_concurrency,
                acquire_timeout_seconds=self.acquire_timeout_seconds,
            ) from exc
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._semaphore.release()


@dataclass(frozen=True, slots=True)
class ProviderClientSettings:
    base_url: str
    timeout_seconds: float
    provider_acquire_timeout_seconds: float
    max_concurrency: int
    limiter: ProviderLimiter


def _provider_client_settings(
    provider: str,
    base_url: str | None,
    *,
    service_url_env: str,
    default_base_url: str,
    timeout_seconds: float | None,
    timeout_env: str,
    default_timeout_seconds: float,
    max_concurrency: int | None,
    max_concurrency_env: str,
    default_max_concurrency: int,
    provider_acquire_timeout_seconds: float | None,
) -> ProviderClientSettings:
    resolved_base_url = (
        base_url or os.getenv(service_url_env) or default_base_url
    ).rstrip("/")
    resolved_timeout_seconds = (
        timeout_seconds
        if timeout_seconds is not None
        else env_float(timeout_env, default_timeout_seconds)
    )
    resolved_acquire_timeout_seconds = max(
        0.0,
        provider_acquire_timeout_seconds
        if provider_acquire_timeout_seconds is not None
        else env_float(
            "VOICE_GATEWAY_PROVIDER_ACQUIRE_TIMEOUT_SECONDS",
            DEFAULT_PROVIDER_ACQUIRE_TIMEOUT_SECONDS,
        ),
    )
    resolved_max_concurrency = max(
        1,
        max_concurrency
        if max_concurrency is not None
        else env_int(max_concurrency_env, default_max_concurrency),
    )
    return ProviderClientSettings(
        base_url=resolved_base_url,
        timeout_seconds=resolved_timeout_seconds,
        provider_acquire_timeout_seconds=resolved_acquire_timeout_seconds,
        max_concurrency=resolved_max_concurrency,
        limiter=ProviderLimiter(
            provider,
            max_concurrency=resolved_max_concurrency,
            acquire_timeout_seconds=resolved_acquire_timeout_seconds,
        ),
    )


class STTStreamClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        connect_timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        provider_acquire_timeout_seconds: float | None = None,
    ) -> None:
        settings = _provider_client_settings(
            "stt",
            base_url,
            service_url_env="STT_SERVICE_URL",
            default_base_url="http://stt_service:8001",
            timeout_seconds=connect_timeout_seconds,
            timeout_env="VOICE_GATEWAY_STT_CONNECT_TIMEOUT_SECONDS",
            default_timeout_seconds=DEFAULT_STT_CONNECT_TIMEOUT_SECONDS,
            max_concurrency=max_concurrency,
            max_concurrency_env="VOICE_GATEWAY_STT_MAX_CONCURRENCY",
            default_max_concurrency=DEFAULT_STT_MAX_CONCURRENCY,
            provider_acquire_timeout_seconds=provider_acquire_timeout_seconds,
        )
        self.base_url = settings.base_url
        self.connect_timeout_seconds = settings.timeout_seconds
        self.provider_acquire_timeout_seconds = (
            settings.provider_acquire_timeout_seconds
        )
        self.max_concurrency = settings.max_concurrency
        self._limiter = settings.limiter

    def _ws_url_for_language(self, session_id: str, language: str | None) -> str:
        if self.base_url.startswith("https://"):
            base = "wss://" + self.base_url[len("https://") :]
        elif self.base_url.startswith("http://"):
            base = "ws://" + self.base_url[len("http://") :]
        else:
            base = self.base_url
        url = f"{base}/v1/audio/{session_id}"
        if language:
            url = f"{url}?language={quote(language)}"
        return url

    async def stream_audio(
        self,
        session_id: str,
        audio_queue: "asyncio.Queue[bytes | None]",
        on_transcript: TranscriptCallback,
        *,
        language: str | None = None,
    ) -> None:
        async with (
            self._limiter,
            websockets.connect(
                self._ws_url_for_language(session_id, language),
                max_size=None,
                open_timeout=self.connect_timeout_seconds,
            ) as ws,
        ):

            async def receiver() -> None:
                async for message in ws:
                    await on_transcript(json.loads(message))

            receiver_task = asyncio.create_task(receiver())
            try:
                while True:
                    frame = await audio_queue.get()
                    if frame is None:
                        await ws.send(json.dumps({"type": "commit"}))
                        break
                    await ws.send(frame)
            finally:
                receiver_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                    await receiver_task


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        provider_acquire_timeout_seconds: float | None = None,
    ) -> None:
        settings = _provider_client_settings(
            "llm",
            base_url,
            service_url_env="LLM_SERVICE_URL",
            default_base_url="http://llm_service:8002",
            timeout_seconds=timeout_seconds,
            timeout_env="VOICE_GATEWAY_LLM_TIMEOUT_SECONDS",
            default_timeout_seconds=DEFAULT_LLM_TIMEOUT_SECONDS,
            max_concurrency=max_concurrency,
            max_concurrency_env="VOICE_GATEWAY_LLM_MAX_CONCURRENCY",
            default_max_concurrency=DEFAULT_LLM_MAX_CONCURRENCY,
            provider_acquire_timeout_seconds=provider_acquire_timeout_seconds,
        )
        self.base_url = settings.base_url
        self.timeout_seconds = settings.timeout_seconds
        self.provider_acquire_timeout_seconds = (
            settings.provider_acquire_timeout_seconds
        )
        self.max_concurrency = settings.max_concurrency
        self._limiter = settings.limiter
        self._client: httpx.AsyncClient | None = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def respond(
        self,
        session_id: str,
        text: str,
        history: list[dict[str, str]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        async with self._limiter:
            response = await self._http_client().post(
                f"{self.base_url}/v1/respond",
                json={
                    "session_id": session_id,
                    "text": text,
                    "history": history,
                    "metadata": metadata or {},
                },
            )
        response.raise_for_status()
        body = response.json()
        return LLMResponse(
            text=str(body["text"]),
            model=str(body.get("model") or ""),
            provider=str(body.get("provider") or ""),
        )

    async def stream_respond(
        self,
        session_id: str,
        text: str,
        history: list[dict[str, str]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._limiter:
            async with self._http_client().stream(
                "POST",
                f"{self.base_url}/v1/respond/stream",
                json={
                    "session_id": session_id,
                    "text": text,
                    "history": history,
                    "metadata": metadata or {},
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    yield json.loads(line)


class TranslationClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        provider_acquire_timeout_seconds: float | None = None,
    ) -> None:
        settings = _provider_client_settings(
            "translation",
            base_url,
            service_url_env="LLM_SERVICE_URL",
            default_base_url="http://llm_service:8002",
            timeout_seconds=timeout_seconds,
            timeout_env="VOICE_GATEWAY_TRANSLATION_TIMEOUT_SECONDS",
            default_timeout_seconds=DEFAULT_TRANSLATION_TIMEOUT_SECONDS,
            max_concurrency=max_concurrency,
            max_concurrency_env="VOICE_GATEWAY_TRANSLATION_MAX_CONCURRENCY",
            default_max_concurrency=DEFAULT_TRANSLATION_MAX_CONCURRENCY,
            provider_acquire_timeout_seconds=provider_acquire_timeout_seconds,
        )
        self.base_url = settings.base_url
        self.timeout_seconds = settings.timeout_seconds
        self.provider_acquire_timeout_seconds = (
            settings.provider_acquire_timeout_seconds
        )
        self.max_concurrency = settings.max_concurrency
        self._limiter = settings.limiter

    async def translate(
        self,
        session_id: str,
        text: str,
        *,
        source_language: str,
        target_language: str,
    ) -> TranslationResult:
        async with (
            self._limiter,
            httpx.AsyncClient(timeout=self.timeout_seconds) as client,
        ):
            response = await client.post(
                f"{self.base_url}/v1/translate",
                json={
                    "session_id": session_id,
                    "text": text,
                    "source_language": source_language,
                    "target_language": target_language,
                },
            )
            response.raise_for_status()
            body = response.json()
            return TranslationResult(
                text=str(body["text"]),
                model=str(body.get("model") or ""),
                provider=str(body.get("provider") or ""),
            )


class TTSClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        provider_acquire_timeout_seconds: float | None = None,
    ) -> None:
        settings = _provider_client_settings(
            "tts",
            base_url,
            service_url_env="TTS_SERVICE_URL",
            default_base_url="http://tts_service:8003",
            timeout_seconds=timeout_seconds,
            timeout_env="VOICE_GATEWAY_TTS_TIMEOUT_SECONDS",
            default_timeout_seconds=DEFAULT_TTS_TIMEOUT_SECONDS,
            max_concurrency=max_concurrency,
            max_concurrency_env="VOICE_GATEWAY_TTS_MAX_CONCURRENCY",
            default_max_concurrency=DEFAULT_TTS_MAX_CONCURRENCY,
            provider_acquire_timeout_seconds=provider_acquire_timeout_seconds,
        )
        self.base_url = settings.base_url
        self.timeout_seconds = settings.timeout_seconds
        self.provider_acquire_timeout_seconds = (
            settings.provider_acquire_timeout_seconds
        )
        self.max_concurrency = settings.max_concurrency
        self._limiter = settings.limiter

    async def speak(
        self,
        fs_uuid: str,
        text: str,
        *,
        language: str | None = None,
        interruptible: bool = True,
        wait_complete: bool = False,
        event_uuid: str | None = None,
        fs_host: str | None = None,
    ) -> dict[str, Any]:
        async with (
            self._limiter,
            httpx.AsyncClient(timeout=self.timeout_seconds) as client,
        ):
            response = await client.post(
                f"{self.base_url}/v1/speak",
                json={
                    "fs_uuid": fs_uuid,
                    "text": text,
                    "language": language,
                    "interruptible": interruptible,
                    "event_lock": wait_complete,
                    "event_uuid": event_uuid,
                    "fs_host": fs_host,
                },
            )
            response.raise_for_status()
            return response.json()


class FreeSwitchControlClient:
    def __init__(
        self,
        *,
        default_host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        ) -> None:
        self.default_host = default_host or os.getenv("FREESWITCH_HOST", "freeswitch")
        self.port = port or env_int("FREESWITCH_ESL_PORT", 8021)
        self.password = password or os.getenv("FREESWITCH_ESL_PASSWORD", "")
        self._lock = asyncio.Lock()
        self._connections: dict[str, Any] = {}

    async def _inbound(self, fs_host: str | None = None) -> Any:
        if not self.password:
            raise RuntimeError("FREESWITCH_ESL_PASSWORD is required")
        host = fs_host or self.default_host
        return await inbound_connection(
            host=host,
            port=self.port,
            password=self.password,
            events="events plain BACKGROUND_JOB CHANNEL_ANSWER CHANNEL_PARK CHANNEL_HANGUP",
            lock=self._lock,
            connections=self._connections,
        )

    async def api(self, command: str, *, fs_host: str | None = None) -> Any:
        ctl = await self._inbound(fs_host)
        return await ctl.send(f"api {command}")

    async def bgapi(self, command: str, *, fs_host: str | None = None) -> Any:
        ctl = await self._inbound(fs_host)
        return await ctl.send(f"bgapi {command}")
