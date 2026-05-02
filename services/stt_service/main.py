"""Streaming STT WebSocket service.

FreeSWITCH sends L16 PCM frames through mod_audio_stream. This service bridges
those frames to OpenAI Realtime transcription. A deterministic local transcript
is available only when AI_OFFLINE_FALLBACK=1.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from services.common.audio import pcm16_resample_mono, pcm16_rms
from services.common.config import (
    ai_service_degraded_payload,
    ai_service_ready,
    env_bool,
    env_float,
    env_int,
    env_str,
    offline_fallback_enabled,
    openai_ready,
)


app = FastAPI(title="STT Service")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("stt_service")
DEFAULT_LOCAL_STT_FINAL_TEXT = (
    "Speech detected by offline STT fallback; no transcript available."
)


@app.get("/health")
async def health():
    if not ai_service_ready():
        return JSONResponse(
            status_code=503,
            content=ai_service_degraded_payload(),
        )
    return {"status": "ok"}


def _upstream_failure_fallback_enabled() -> bool:
    return env_bool("STT_FALLBACK_ON_UPSTREAM_ERROR", False)


def _error_event(exc: Exception) -> dict[str, Any]:
    return {
        "type": "error",
        "detail": {
            "type": "upstream_connection_error",
            "message": "OpenAI transcription failed",
            "error_type": exc.__class__.__name__,
        },
    }


def _openai_turn_detection_config() -> dict[str, Any] | None:
    mode = (
        (os.getenv("OPENAI_STT_TURN_DETECTION_TYPE", "server_vad") or "server_vad")
        .strip()
        .lower()
    )
    if mode in {"none", "off", "disabled", "0", "false"}:
        return None
    if mode == "semantic_vad":
        eagerness = (
            (os.getenv("OPENAI_STT_SEMANTIC_EAGERNESS", "auto") or "auto")
            .strip()
            .lower()
        )
        if eagerness not in {"auto", "low", "medium", "high"}:
            eagerness = "auto"
        return {"type": "semantic_vad", "eagerness": eagerness}
    return {
        "type": "server_vad",
        "threshold": env_float("OPENAI_STT_VAD_THRESHOLD", 0.5),
        "prefix_padding_ms": env_int("OPENAI_STT_PREFIX_PADDING_MS", 300),
        "silence_duration_ms": env_int("OPENAI_STT_SILENCE_DURATION_MS", 500),
    }


def _openai_transcription_session_update(model: str, language: str) -> dict[str, Any]:
    noise_reduction_type = (
        os.getenv("OPENAI_STT_NOISE_REDUCTION_TYPE", "near_field").strip()
        or "near_field"
    )
    transcription: dict[str, Any] = {
        "model": model,
        "language": language,
    }
    prompt = env_str("OPENAI_STT_PROMPT")
    if prompt:
        transcription["prompt"] = prompt
    session: dict[str, Any] = {
        "input_audio_format": "pcm16",
        "input_audio_noise_reduction": {"type": noise_reduction_type},
        "input_audio_transcription": transcription,
        "turn_detection": _openai_turn_detection_config(),
    }
    if env_bool("OPENAI_STT_INCLUDE_LOGPROBS", False):
        session["include"] = ["item.input_audio_transcription.logprobs"]
    return {
        "type": "transcription_session.update",
        "session": session,
    }


def _openai_event_metadata(
    event: dict[str, Any], *, language: str, vad_mode: str | None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "provider": "openai",
        "language": language,
    }
    if vad_mode:
        metadata["vad_mode"] = vad_mode
    for key in (
        "event_id",
        "item_id",
        "previous_item_id",
        "content_index",
        "audio_start_ms",
        "audio_end_ms",
    ):
        if key in event and event.get(key) is not None:
            metadata[key] = event.get(key)
    return metadata


def _confidence_from_logprobs(logprobs: Any) -> float | None:
    if not isinstance(logprobs, list):
        return None
    token_logprobs: list[float] = []
    for item in logprobs:
        if not isinstance(item, dict) or "logprob" not in item:
            continue
        try:
            token_logprobs.append(float(item["logprob"]))
        except (TypeError, ValueError):
            continue
    if not token_logprobs:
        return None
    avg_logprob = sum(token_logprobs) / len(token_logprobs)
    return round(max(0.0, min(1.0, math.exp(avg_logprob))), 4)


def _local_stt_confidence() -> float:
    return max(0.0, min(1.0, env_float("LOCAL_STT_CONFIDENCE", 0.0)))


def _openai_partial_event(
    event: dict[str, Any], *, language: str, vad_mode: str | None
) -> dict[str, Any]:
    return {
        "type": "partial",
        "text": event.get("delta", ""),
        "is_final": False,
        **_openai_event_metadata(event, language=language, vad_mode=vad_mode),
    }


def _openai_lifecycle_event(
    mapped_type: str,
    event: dict[str, Any],
    *,
    language: str,
    vad_mode: str | None,
    stats: dict[str, float] | None = None,
) -> dict[str, Any]:
    payload = {
        "type": mapped_type,
        "text": "",
        "is_final": False,
        **_openai_event_metadata(event, language=language, vad_mode=vad_mode),
    }
    if stats:
        payload.update(stats)
    return payload


@dataclass
class AudioActivityStats:
    sample_rate: int
    speech_rms_threshold: float = field(
        default_factory=lambda: env_float("STT_FINAL_SPEECH_RMS_THRESHOLD", 250.0)
    )
    min_speech_ms: float = field(
        default_factory=lambda: env_float("STT_FINAL_MIN_SPEECH_MS", 120.0)
    )
    min_avg_rms: float = field(
        default_factory=lambda: env_float("STT_FINAL_MIN_AVG_RMS", 250.0)
    )
    min_peak_rms: float = field(
        default_factory=lambda: env_float("STT_FINAL_MIN_PEAK_RMS", 400.0)
    )
    audio_ms: float = 0.0
    speech_ms: float = 0.0
    speech_rms_ms: float = 0.0
    peak_rms: float = 0.0

    def add(self, data: bytes) -> None:
        if len(data) < 2 or self.sample_rate <= 0:
            return
        sample_count = len(data) // 2
        duration_ms = sample_count / self.sample_rate * 1000.0
        rms = pcm16_rms(data)
        self.audio_ms += duration_ms
        self.peak_rms = max(self.peak_rms, rms)
        if rms >= self.speech_rms_threshold:
            self.speech_ms += duration_ms
            self.speech_rms_ms += rms * duration_ms

    def snapshot(self) -> dict[str, float]:
        avg_rms = self.speech_rms_ms / self.speech_ms if self.speech_ms > 0 else 0.0
        return {
            "audio_ms": round(self.audio_ms, 1),
            "speech_ms": round(self.speech_ms, 1),
            "avg_rms": round(avg_rms, 1),
            "peak_rms": round(self.peak_rms, 1),
        }

    def reset(self) -> None:
        self.audio_ms = 0.0
        self.speech_ms = 0.0
        self.speech_rms_ms = 0.0
        self.peak_rms = 0.0

    def suppression_reason(
        self, transcript: str, stats: dict[str, float] | None = None
    ) -> str | None:
        if not env_bool("STT_SUPPRESS_LOW_AUDIO_FINALS", True):
            return None
        if not transcript.strip():
            return "empty_transcript"
        activity_stats = stats or self.snapshot()
        if activity_stats["speech_ms"] < self.min_speech_ms:
            return "speech_too_short"
        if activity_stats["avg_rms"] < self.min_avg_rms:
            return "speech_energy_too_low"
        if activity_stats["peak_rms"] < self.min_peak_rms:
            return "speech_peak_too_low"
        return None


def _stt_suppressed_event(
    transcript: str,
    reason: str,
    stats: dict[str, float],
    language: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "suppressed",
        "text": transcript,
        "is_final": True,
        "provider": "openai",
        "reason": reason,
        "language": language,
        **stats,
        **(metadata or {}),
    }


def _openai_final_event(
    transcript: str,
    stats: dict[str, float],
    language: str,
    *,
    metadata: dict[str, Any] | None = None,
    logprobs: Any = None,
) -> dict[str, Any]:
    confidence = _confidence_from_logprobs(logprobs)
    confidence_source = "openai_logprobs" if confidence is not None else "not_provided"
    payload = {
        "type": "final",
        "text": transcript,
        "is_final": True,
        "confidence": confidence,
        "confidence_source": confidence_source,
        "language": language,
        "provider": "openai",
        **stats,
        **(metadata or {}),
    }
    if isinstance(logprobs, list):
        payload["logprobs_token_count"] = len(logprobs)
    return payload


@dataclass
class OpenAICompletionOrderer:
    committed_order: list[str] = field(default_factory=list)
    completed_by_item: dict[str, dict[str, Any]] = field(default_factory=dict)

    def commit(self, item_id: Any) -> None:
        if not item_id:
            return
        clean_item_id = str(item_id)
        if clean_item_id not in self.committed_order:
            self.committed_order.append(clean_item_id)

    def add_completed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        item_id = event.get("item_id")
        if not item_id:
            return [event]
        clean_item_id = str(item_id)
        if not self.committed_order:
            return [event]
        self.completed_by_item[clean_item_id] = event
        return self.flush_ready()

    def flush_ready(self) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        while (
            self.committed_order and self.committed_order[0] in self.completed_by_item
        ):
            item_id = self.committed_order.pop(0)
            ready.append(self.completed_by_item.pop(item_id))
        return ready


@dataclass
class LocalTurnBuffer:
    session_id: str
    language: str = "en"
    sample_rate: int = 16000
    speech_threshold: float = 300.0
    min_speech_bytes: int = 16000
    bytes_seen: int = 0
    speech_bytes: int = 0
    last_voice_at: float = field(default_factory=time.monotonic)
    emitted_partial: bool = False
    emitted_final: bool = False
    fallback_text: str = field(
        default_factory=lambda: env_str(
            "LOCAL_STT_FINAL_TEXT", DEFAULT_LOCAL_STT_FINAL_TEXT
        )
    )
    fallback_confidence: float = field(default_factory=_local_stt_confidence)

    def add(self, data: bytes) -> dict[str, Any] | None:
        now = time.monotonic()
        self.bytes_seen += len(data)
        if pcm16_rms(data) >= self.speech_threshold:
            self.speech_bytes += len(data)
            self.last_voice_at = now
            self.emitted_final = False
            if self.emitted_partial:
                return None
            self.emitted_partial = True
            return {
                "type": "partial",
                "text": "Listening...",
                "is_final": False,
                "provider": "local_fallback",
                "fallback": True,
                "fallback_reason": "offline speech activity detector",
            }
        if (
            self.speech_bytes >= self.min_speech_bytes
            and not self.emitted_final
            and now - self.last_voice_at >= 0.8
        ):
            self.emitted_final = True
            self.emitted_partial = False
            self.speech_bytes = 0
            return {
                "type": "final",
                "text": self.fallback_text,
                "is_final": True,
                "confidence": self.fallback_confidence,
                "language": self.language,
                "provider": "local_fallback",
                "fallback": True,
                "fallback_reason": "offline speech activity detector",
            }
        if (
            self.emitted_partial
            and self.speech_bytes < self.min_speech_bytes
            and now - self.last_voice_at >= 0.8
        ):
            self.emitted_partial = False
            self.speech_bytes = 0
        return None

    def commit(self) -> dict[str, Any] | None:
        if self.speech_bytes <= 0 or self.emitted_final:
            return None
        self.emitted_final = True
        self.emitted_partial = False
        self.speech_bytes = 0
        return {
            "type": "final",
            "text": self.fallback_text,
            "is_final": True,
            "confidence": self.fallback_confidence,
            "language": self.language,
            "provider": "local_fallback",
            "fallback": True,
            "fallback_reason": "offline speech activity detector",
        }


async def _local_transcribe(
    websocket: WebSocket, session_id: str, language: str
) -> None:
    buffer = LocalTurnBuffer(session_id=session_id, language=language)
    while True:
        try:
            message = await websocket.receive()
        except (RuntimeError, WebSocketDisconnect):
            return
        if "bytes" in message and message["bytes"] is not None:
            event = buffer.add(message["bytes"])
            if event:
                if event.get("is_final"):
                    logger.info(
                        "local transcript final session_id=%s language=%s",
                        session_id,
                        language,
                    )
                await websocket.send_json(event)
        elif "text" in message and message["text"]:
            payload = json.loads(message["text"])
            if payload.get("type") == "commit":
                event = buffer.commit()
                if event:
                    logger.info(
                        "local transcript final session_id=%s language=%s",
                        session_id,
                        language,
                    )
                    await websocket.send_json(event)


def _client_disconnect_runtime_error(exc: RuntimeError) -> bool:
    return 'Cannot call "receive" once a disconnect message has been received' in str(
        exc
    )


async def _openai_transcribe(
    websocket: WebSocket, session_id: str, language: str
) -> None:
    import websockets

    api_key = os.environ["OPENAI_API_KEY"]
    url = os.getenv(
        "OPENAI_REALTIME_URL", "wss://api.openai.com/v1/realtime?intent=transcription"
    )
    model = os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe")
    source_rate = int(os.getenv("STT_INPUT_SAMPLE_RATE", "16000"))
    target_rate = 24000
    activity = AudioActivityStats(sample_rate=source_rate)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1",
    }
    async with websockets.connect(
        url, additional_headers=headers, max_size=None
    ) as upstream:
        session_update = _openai_transcription_session_update(model, language)
        turn_detection = session_update["session"].get("turn_detection")
        vad_mode = (
            turn_detection.get("type") if isinstance(turn_detection, dict) else None
        )
        await upstream.send(json.dumps(session_update))
        stats_by_item: dict[str, dict[str, float]] = {}
        previous_by_item: dict[str, Any] = {}
        orderer = OpenAICompletionOrderer()

        async def recv_openai() -> None:
            async for raw in upstream:
                event = json.loads(raw)
                event_type = event.get("type", "")
                if event_type.endswith("input_audio_transcription.delta"):
                    await websocket.send_json(
                        _openai_partial_event(
                            event, language=language, vad_mode=vad_mode
                        )
                    )
                elif event_type.endswith("input_audio_buffer.speech_started"):
                    await websocket.send_json(
                        _openai_lifecycle_event(
                            "speech_started",
                            event,
                            language=language,
                            vad_mode=vad_mode,
                        )
                    )
                elif event_type.endswith("input_audio_buffer.speech_stopped"):
                    await websocket.send_json(
                        _openai_lifecycle_event(
                            "speech_stopped",
                            event,
                            language=language,
                            vad_mode=vad_mode,
                        )
                    )
                elif event_type.endswith("input_audio_buffer.committed"):
                    item_id = event.get("item_id")
                    activity_stats = activity.snapshot()
                    if item_id:
                        clean_item_id = str(item_id)
                        stats_by_item[clean_item_id] = activity_stats
                        previous_by_item[clean_item_id] = event.get("previous_item_id")
                    orderer.commit(item_id)
                    activity.reset()
                    await websocket.send_json(
                        _openai_lifecycle_event(
                            "endpoint",
                            event,
                            language=language,
                            vad_mode=vad_mode,
                            stats=activity_stats,
                        )
                    )
                elif event_type.endswith("input_audio_transcription.completed"):
                    transcript = str(event.get("transcript") or "")
                    item_id = event.get("item_id")
                    clean_item_id = str(item_id) if item_id else ""
                    if (
                        clean_item_id
                        and "previous_item_id" not in event
                        and clean_item_id in previous_by_item
                    ):
                        event = {
                            **event,
                            "previous_item_id": previous_by_item[clean_item_id],
                        }
                    activity_stats = (
                        stats_by_item.pop(clean_item_id, None)
                        if clean_item_id
                        else None
                    )
                    if activity_stats is None:
                        activity_stats = activity.snapshot()
                        activity.reset()
                    metadata = _openai_event_metadata(
                        event, language=language, vad_mode=vad_mode
                    )
                    suppression_reason = activity.suppression_reason(
                        transcript, activity_stats
                    )
                    if suppression_reason:
                        logger.info(
                            "openai transcript suppressed session_id=%s language=%s reason=%s stats=%s",
                            session_id,
                            language,
                            suppression_reason,
                            activity_stats,
                        )
                        completed_event = _stt_suppressed_event(
                            transcript,
                            suppression_reason,
                            activity_stats,
                            language,
                            metadata=metadata,
                        )
                    else:
                        logger.info(
                            "openai transcript final session_id=%s language=%s",
                            session_id,
                            language,
                        )
                        completed_event = _openai_final_event(
                            transcript,
                            activity_stats,
                            language,
                            metadata=metadata,
                            logprobs=event.get("logprobs"),
                        )
                    for ready_event in orderer.add_completed(completed_event):
                        await websocket.send_json(ready_event)
                elif event_type.endswith("error"):
                    await websocket.send_json({"type": "error", "detail": event})

        recv_task = asyncio.create_task(recv_openai())
        try:
            while True:
                try:
                    message = await websocket.receive()
                except WebSocketDisconnect:
                    return
                except RuntimeError as exc:
                    if _client_disconnect_runtime_error(exc):
                        return
                    raise
                if "bytes" in message and message["bytes"] is not None:
                    audio = message["bytes"]
                    activity.add(audio)
                    pcm24 = pcm16_resample_mono(audio, source_rate, target_rate)
                    await upstream.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm24).decode("ascii"),
                            }
                        )
                    )
                elif "text" in message and message["text"]:
                    payload = json.loads(message["text"])
                    if payload.get("type") == "commit":
                        await upstream.send(
                            json.dumps({"type": "input_audio_buffer.commit"})
                        )
        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass


@app.websocket("/v1/audio/{session_id}")
async def audio_stream(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    language = websocket.query_params.get("language") or os.getenv(
        "OPENAI_STT_LANGUAGE", "en"
    )
    try:
        if offline_fallback_enabled():
            await _local_transcribe(websocket, session_id, language)
        elif openai_ready():
            try:
                await _openai_transcribe(websocket, session_id, language)
            except Exception as exc:
                logger.exception(
                    "openai transcription failed session_id=%s", session_id
                )
                if not _upstream_failure_fallback_enabled():
                    await websocket.send_json(_error_event(exc))
                    return
                await websocket.send_json(
                    {
                        "type": "warning",
                        "message": "OpenAI transcription failed; using local fallback",
                        "detail": _error_event(exc)["detail"],
                        "provider": "local_fallback",
                        "fallback": True,
                        "fallback_reason": "upstream_connection_error",
                    }
                )
                await _local_transcribe(websocket, session_id, language)
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "detail": "OPENAI_API_KEY is required unless AI_OFFLINE_FALLBACK=1",
                }
            )
            await websocket.close(code=1011)
    except WebSocketDisconnect:
        return
