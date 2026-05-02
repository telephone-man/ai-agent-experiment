"""Voice gateway: outbound ESL listener plus media WebSocket endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from services.common.freeswitch_commands import (
    build_audio_stream_start_command,
    build_audio_stream_stop_command,
    build_originate_translation_leg_command,
    build_uuid_break_command,
    validate_translation_peer_aor,
)
from services.common.config import env_bool, env_float, env_int
from services.voice_gateway.clients import (
    FreeSwitchControlClient,
    LLMClient,
    STTStreamClient,
    TTSClient,
    TranslationClient,
)
from services.voice_gateway.events import VoiceEventBus
from services.voice_gateway.models import (
    CallLeg,
    CallSession,
    LegRole,
    SessionMode,
    SessionState,
)
from services.voice_gateway.reliability import (
    DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
    DEFAULT_CIRCUIT_RESET_SECONDS,
    DEFAULT_MAX_ACTIVE_SESSIONS,
    AdmissionController,
    AdmissionDecision,
    ProviderCircuitBreaker,
    ProviderCircuitOpenError,
)
from services.voice_gateway.turn_taking import SemanticTurnDetector, TurnAction
from voice_policy import (
    HeuristicSemanticInterpreter,
    PolicyAction,
    PolicyDecision,
    PolicyInput,
    SemanticFrame,
    SpeechAct,
    evaluate_policy,
)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("voice_gateway")

_VOICE_EVENT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

POLICY_CONFIRMATION_PROMPT = "Before I do anything that could affect your account, please confirm exactly what you want me to change."
POLICY_SAFE_FALLBACK_PROMPT = (
    "I can't make that change from this demo, but I can explain what would happen next."
)
SOFT_INTERJECTION_CHECKIN_PROMPT = "Yes?"
DELIVERY_RESUME_BRIDGE_PROMPT = "I'll continue where I left off."
STT_UNAVAILABLE_SPOKEN_MESSAGE = (
    "I am struggling to connect to the Speech To Text engine."
)
ACTIVE_TTS_REPLACING_POLICY_ACTIONS = {
    PolicyAction.CLARIFY,
    PolicyAction.RESPOND,
    PolicyAction.SOFT_INTERRUPT_CHECKIN,
    PolicyAction.CONFIRM_BEFORE_ACTION,
    PolicyAction.REJECT_TOOL_EXECUTION,
    PolicyAction.ESCALATE,
    PolicyAction.END_CALL,
}
DEFAULT_TURN_HOLD_ACK_MS = 700
DEFAULT_TURN_HOLD_CLARIFY_MS = 1500
DEFAULT_TURN_HOLD_TTL_SECONDS = 8.0
DEFAULT_TURN_HOLD_FILLER_TEXT = "Okay."
DEFAULT_DELIVERY_RESUME_DELAY_SECONDS = 2.0
DEFAULT_ESL_HANDSHAKE_TIMEOUT_SECONDS = 2.0
DEFAULT_SPEECH_START_BARGE_IN_DEBOUNCE_MS = 700
DEFAULT_SPEECH_START_BARGE_IN_ENABLED = False
PREVIOUS_ASSISTANT_DELIVERY_METADATA_KEY = "previous_assistant_delivery"
STT_EVENT_METADATA_KEYS = (
    "provider",
    "fallback",
    "fallback_reason",
    "raw_text",
    "confidence_source",
    "audio_ms",
    "speech_ms",
    "avg_rms",
    "peak_rms",
    "event_id",
    "item_id",
    "previous_item_id",
    "content_index",
    "audio_start_ms",
    "audio_end_ms",
    "vad_mode",
    "logprobs_token_count",
)
TERMINAL_CHUNK_PUNCTUATION = ".!?"
CLAUSE_CHUNK_PUNCTUATION = ",;:"
DEFAULT_PROGRESSIVE_CHUNK_FALLBACK_WORDS = 18


def max_active_sessions() -> int:
    """Return the configured concurrent-session cap with a safe minimum of one."""
    return max(
        1, env_int("VOICE_GATEWAY_MAX_ACTIVE_SESSIONS", DEFAULT_MAX_ACTIVE_SESSIONS)
    )


def circuit_failure_threshold() -> int:
    """Return provider circuit-breaker failure threshold with a minimum of one."""
    return max(
        1,
        env_int(
            "VOICE_GATEWAY_CIRCUIT_FAILURE_THRESHOLD", DEFAULT_CIRCUIT_FAILURE_THRESHOLD
        ),
    )


def circuit_reset_seconds() -> float:
    """Return circuit-breaker reset window in seconds, clamped to non-negative values."""
    return max(
        0.0,
        env_float("VOICE_GATEWAY_CIRCUIT_RESET_SECONDS", DEFAULT_CIRCUIT_RESET_SECONDS),
    )


@dataclass(slots=True)
class ActiveSpeech:
    """Track one in-flight TTS playback operation for a session UUID."""

    payload: dict[str, Any]
    start_task: asyncio.Task
    finish_task: asyncio.Task
    enqueued_at: float
    playback_started_at: float | None = None
    history_session: CallSession | None = None
    history_text: str | None = None
    generated_text: str | None = None
    history_committed: bool = False


@dataclass(slots=True)
class PendingAssistantTurn:
    """Store deferred assistant response state while waiting for final user intent."""

    text: str
    turn_id: str
    policy_input: PolicyInput
    semantic_frame: SemanticFrame
    policy_decision: PolicyDecision
    ack_ms: int
    clarify_ms: int | None
    ttl_seconds: float
    created_at: float
    updated_at: float
    ack_task: asyncio.Task | None = None
    expiry_task: asyncio.Task | None = None
    filler_spoken: bool = False
    clarification_spoken: bool = False


@dataclass(slots=True)
class TurnLatencyState:
    """Per-leg timing markers for the current user turn."""

    speech_started_at: float | None = None
    first_partial_at: float | None = None
    endpoint_at: float | None = None
    final_at: float | None = None
    semantic_ms: float | None = None
    policy_decision_ms: float | None = None
    policy_evaluation_ms: float | None = None
    policy_decision_at: float | None = None


@dataclass(slots=True)
class PolicyEvaluationResult:
    """Policy result plus local evaluation timing."""

    policy_input: PolicyInput
    semantic_frame: SemanticFrame | None
    policy_decision: PolicyDecision
    semantic_ms: float | None
    policy_decision_ms: float
    policy_evaluation_ms: float


class SentenceChunker:
    """Incrementally split streaming LLM text into sentence-sized speech chunks."""

    def __init__(
        self, *, fallback_words: int = DEFAULT_PROGRESSIVE_CHUNK_FALLBACK_WORDS
    ) -> None:
        """Initialize chunking state and clause-based fallback threshold."""
        self.buffer = ""
        self.fallback_words = max(8, fallback_words)

    def add_delta(self, delta: str) -> list[str]:
        """Append a new text delta and return any chunks now ready to speak."""
        if not delta:
            return []
        self.buffer += delta
        return self._pop_ready(final=False)

    def finish(self) -> list[str]:
        """Flush any buffered text into final chunk(s)."""
        return self._pop_ready(final=True)

    def _pop_ready(self, *, final: bool) -> list[str]:
        chunks: list[str] = []
        while True:
            terminal_index = self._terminal_index()
            if terminal_index >= 0:
                chunk = self.buffer[: terminal_index + 1].strip()
                self.buffer = self.buffer[terminal_index + 1 :].lstrip()
                if chunk:
                    chunks.append(chunk)
                    continue
            if final:
                chunk = " ".join(self.buffer.split())
                self.buffer = ""
                if chunk:
                    chunks.append(chunk)
            elif not chunks:
                fallback_index = self._fallback_clause_index()
                if fallback_index >= 0:
                    chunk = self.buffer[: fallback_index + 1].strip()
                    self.buffer = self.buffer[fallback_index + 1 :].lstrip()
                    if chunk:
                        chunks.append(chunk)
            return chunks

    def _terminal_index(self) -> int:
        for index, character in enumerate(self.buffer):
            if character not in TERMINAL_CHUNK_PUNCTUATION:
                continue
            next_character = self.buffer[index + 1 : index + 2]
            if next_character and not next_character.isspace():
                continue
            if self.buffer[: index + 1].strip():
                return index
        return -1

    def _fallback_clause_index(self) -> int:
        if len(self.buffer.split()) < self.fallback_words:
            return -1
        candidates = [
            index
            for index, character in enumerate(self.buffer)
            if character in CLAUSE_CHUNK_PUNCTUATION
            and self.buffer[: index + 1].strip()
        ]
        if not candidates:
            return -1
        return candidates[-1]


class VoiceGateway:
    """Orchestrate call sessions, media streaming, and AI provider interactions."""

    def __init__(self, event_bus: VoiceEventBus | None = None) -> None:
        """Create gateway runtime state, provider clients, and reliability controls."""
        self.sessions: dict[str, CallSession] = {}
        self.sessions_by_uuid: dict[str, str] = {}
        self.audio_queues: dict[str, asyncio.Queue[bytes | None]] = {}
        self.esl_sessions: dict[str, Any] = {}
        self.active_speech: dict[str, ActiveSpeech] = {}
        self.active_speech_by_event_uuid: dict[str, str] = {}
        self.soft_interjections_during_tts: dict[str, int] = {}
        self.stt_partial_transcripts: dict[str, str] = {}
        self.stt_lifecycle_items: dict[str, set[str]] = {}
        self.stt_unavailable_announced: set[str] = set()
        self.pending_assistant_turns: dict[str, PendingAssistantTurn] = {}
        self.turn_latency: dict[str, TurnLatencyState] = {}
        self.active_llm_tasks: dict[str, asyncio.Task] = {}
        self.speech_start_barge_tasks: dict[str, asyncio.Task] = {}
        self.pending_delivery_resume_tasks: dict[str, asyncio.Task] = {}
        self.event_bus = event_bus or VoiceEventBus()
        self.active_user_speech: set[str] = set()
        self.detector = SemanticTurnDetector()
        self.semantic_interpreter = HeuristicSemanticInterpreter()
        self.stt_client = STTStreamClient()
        self.llm_client = LLMClient()
        self.translation_client = TranslationClient()
        self.tts_client = TTSClient()
        self.fs_control = FreeSwitchControlClient()
        self.admission = AdmissionController(max_active_sessions=max_active_sessions())
        self.provider_circuits = {
            provider: ProviderCircuitBreaker(
                provider,
                failure_threshold=circuit_failure_threshold(),
                reset_seconds=circuit_reset_seconds(),
            )
            for provider in ("stt", "llm", "translation", "tts")
        }
        self.tasks: set[asyncio.Task] = set()

    async def connect_call(self, fs_session: Any) -> None:
        """Accept a new FreeSWITCH ESL call and route it to assistant or translation mode."""
        context = getattr(fs_session, "context", {}) or {}
        fs_uuid = context.get("Channel-Call-UUID") or context.get("Unique-ID")
        if not fs_uuid:
            await fs_session.hangup("NORMAL_TEMPORARY_FAILURE")
            return
        admission_session_id = _voice_event_session_id(context) or fs_uuid
        decision = self.admission.try_acquire(admission_session_id)
        await self.publish_admission_event(fs_uuid, decision)
        if not decision.accepted:
            await fs_session.hangup("NORMAL_TEMPORARY_FAILURE")
            return
        destination = (context.get("Caller-Destination-Number") or "").strip()
        call_type = _context_value(context, "variable_sip_h_X-type")
        if destination == "7100" or call_type == "translate_bridge":
            await self.connect_translation_call(fs_session, fs_uuid, context)
        else:
            await self.connect_assistant_call(fs_session, fs_uuid, context)
        if decision.reason == "capacity_available" and not self.find_session(fs_uuid):
            release = self.admission.release(admission_session_id)
            await self.publish_admission_release(fs_uuid, release)

    async def connect_assistant_call(
        self, fs_session: Any, fs_uuid: str, context: dict[str, Any]
    ) -> None:
        """Initialize a single-leg assistant session and begin STT streaming."""
        session_id = _voice_event_session_id(context) or fs_uuid
        call = CallSession(
            session_id=session_id,
            mode=SessionMode.ASSISTANT,
            state=SessionState.ANSWERED,
        )
        call.add_leg(
            CallLeg(
                leg_id="a",
                fs_uuid=fs_uuid,
                role=LegRole.CALLER,
                aor=context.get("variable_sip_from_uri"),
                source_language=os.getenv("VOICE_AGENT_SOURCE_LANGUAGE", "en"),
                target_language=os.getenv("VOICE_AGENT_TARGET_LANGUAGE", "en"),
                media_stream_id=fs_uuid,
            )
        )
        self.register_session(call, fs_session)
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="call.created",
            payload={
                "mode": call.mode.value,
                "aor": context.get("variable_sip_from_uri"),
                "destination": context.get("Caller-Destination-Number"),
            },
        )
        logger.info(
            "assistant session started fs_uuid=%s aor=%s",
            fs_uuid,
            context.get("variable_sip_from_uri") or "",
        )

        try:
            await fs_session.answer()
        except Exception:
            pass
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="call.answered",
            payload={"mode": call.mode.value},
        )
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="call.connected",
            payload={"mode": call.mode.value},
        )

        await self.speak(
            fs_uuid,
            "This is an AI voice assistant. Ask me a question after the tone.",
            language="en",
            wait_complete=False,
            reason="assistant_greeting",
        )
        stream_started = await self.start_audio_stream(fs_session, fs_uuid)
        call.state = SessionState.LISTENING

        if stream_started:
            self.start_stt_task(fs_uuid)

    async def connect_translation_call(
        self, fs_session: Any, fs_uuid: str, context: dict[str, Any]
    ) -> None:
        """Initialize a dual-leg translation session and originate the peer leg."""
        peer_aor = (
            _context_value(context, "variable_sip_h_X-Translate-Peer")
            or os.getenv("TRANSLATION_DEFAULT_PEER_AOR")
            or "sip:bob@voice.local"
        )
        try:
            peer_aor = validate_translation_peer_aor(peer_aor)
        except ValueError as exc:
            logger.warning("invalid translation peer for fs_uuid=%s: %s", fs_uuid, exc)
            await self.publish_voice_event(
                fs_uuid,
                source="esl",
                type="system.error",
                payload={
                    "message": "invalid translation peer",
                    "error": "invalid_translation_peer",
                },
            )
            with suppress(Exception):
                await fs_session.hangup("NORMAL_CLEARING")
            return
        source_language = (
            _context_value(context, "variable_sip_h_X-Source-Language") or "fr"
        )
        target_language = (
            _context_value(context, "variable_sip_h_X-Target-Language") or "en"
        )
        fs_host = _context_value(context, "variable_x_fs_host") or os.getenv(
            "FREESWITCH_HOST", "freeswitch"
        )
        peer_uuid = str(uuid.uuid4())
        session_id = _voice_event_session_id(context) or fs_uuid
        call = CallSession(
            session_id=session_id,
            mode=SessionMode.TRANSLATION,
            state=SessionState.ANSWERED,
            metadata={"peer_aor": peer_aor, "fs_host": fs_host},
        )
        call.add_leg(
            CallLeg(
                leg_id="a",
                fs_uuid=fs_uuid,
                role=LegRole.CALLER,
                aor=context.get("variable_sip_from_uri"),
                source_language=source_language,
                target_language=target_language,
                media_stream_id=fs_uuid,
                peer_leg_id="b",
            )
        )
        call.add_leg(
            CallLeg(
                leg_id="b",
                fs_uuid=peer_uuid,
                role=LegRole.PEER,
                aor=peer_aor,
                source_language=target_language,
                target_language=source_language,
                media_stream_id=peer_uuid,
                peer_leg_id="a",
            )
        )
        self.register_session(call, fs_session)
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="call.created",
            payload={
                "mode": call.mode.value,
                "aor": context.get("variable_sip_from_uri"),
                "peer_aor": peer_aor,
                "source_language": source_language,
                "target_language": target_language,
            },
        )
        logger.info(
            "translation session started fs_uuid=%s peer_aor=%s source=%s target=%s",
            fs_uuid,
            peer_aor,
            source_language,
            target_language,
        )

        try:
            await fs_session.answer()
        except Exception:
            pass
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="call.answered",
            payload={"mode": call.mode.value},
        )
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="call.connected",
            payload={"mode": call.mode.value},
        )

        originate = build_originate_translation_leg_command(
            peer_aor=peer_aor,
            peer_uuid=peer_uuid,
            fs_path=os.getenv("TRANSLATION_KAMAILIO_FS_PATH", "sip:kamailio:5060"),
        )
        try:
            reply = await self.fs_control.bgapi(originate, fs_host=fs_host)
        except Exception as exc:
            logger.exception(
                "translation originate command failed peer_uuid=%s",
                peer_uuid,
            )
            await self.fail_translation_setup(
                fs_uuid,
                message="translation originate failed",
                peer_uuid=peer_uuid,
                error=exc.__class__.__name__,
            )
            return
        reply_text = _reply_text(reply)
        if _reply_is_error(reply_text):
            logger.error(
                "translation originate failed peer_uuid=%s reply=%s",
                peer_uuid,
                reply_text,
            )
            await self.fail_translation_setup(
                fs_uuid,
                message="translation originate rejected",
                peer_uuid=peer_uuid,
                reply=reply_text,
            )
            return
        logger.info(
            "translation originate requested peer_uuid=%s reply=%s",
            peer_uuid,
            reply_text,
        )

        if await self.start_audio_stream(fs_session, fs_uuid, leg_id="a"):
            self.start_stt_task(fs_uuid)

        task = asyncio.create_task(
            self._prepare_translation_peer(fs_session, peer_uuid, fs_host=fs_host),
            name=f"translation-peer-{peer_uuid}",
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        call.state = SessionState.LISTENING

    async def fail_translation_setup(
        self,
        fs_uuid: str,
        *,
        message: str,
        **payload: Any,
    ) -> None:
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="system.error",
            payload={
                "message": message,
                **{key: value for key, value in payload.items() if value is not None},
            },
        )
        session = self.find_session(fs_uuid)
        leg_uuids = (
            [leg.fs_uuid for leg in session.legs.values()] if session else [fs_uuid]
        )
        leg_uuids = [leg_uuid for leg_uuid in leg_uuids if leg_uuid != fs_uuid] + [
            fs_uuid
        ]
        for leg_uuid in leg_uuids:
            await self.hangup_leg(leg_uuid)

    def register_session(self, call: CallSession, fs_session: Any) -> None:
        """Index a call session by session ID and each leg UUID."""
        self.sessions[call.session_id] = call
        self.esl_sessions[call.primary_leg().fs_uuid] = fs_session
        for leg in call.legs.values():
            self.sessions_by_uuid[leg.fs_uuid] = call.session_id
            self.audio_queues[leg.fs_uuid] = asyncio.Queue(maxsize=200)

    def start_stt_task(self, fs_uuid: str) -> None:
        task = asyncio.create_task(self._run_stt(fs_uuid), name=f"stt-{fs_uuid}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def publish_voice_event(
        self,
        fs_uuid: str,
        *,
        source: str,
        type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        session_id, call_id = self._event_context(fs_uuid)
        try:
            await self.event_bus.publish(
                session_id=session_id,
                call_id=call_id,
                source=source,  # type: ignore[arg-type]
                type=type,
                payload=payload or {},
            )
        except Exception:
            logger.debug(
                "voice event publish failed type=%s fs_uuid=%s",
                type,
                fs_uuid,
                exc_info=True,
            )

    async def publish_admission_event(
        self, fs_uuid: str, decision: AdmissionDecision
    ) -> None:
        await self.publish_admission_decision(
            fs_uuid,
            decision,
            "admission.accepted" if decision.accepted else "admission.rejected",
        )

    async def publish_admission_release(
        self, fs_uuid: str, decision: AdmissionDecision
    ) -> None:
        await self.publish_admission_decision(
            fs_uuid,
            decision,
            "admission.released",
        )

    async def publish_admission_decision(
        self, fs_uuid: str, decision: AdmissionDecision, event_type: str
    ) -> None:
        payload = {
            "session_id": decision.session_id,
            "active_sessions": decision.active_sessions,
            "max_active_sessions": decision.max_active_sessions,
            "reason": decision.reason,
        }
        if self.find_session(fs_uuid) or decision.session_id == fs_uuid:
            await self.publish_voice_event(
                fs_uuid,
                source="system",
                type=event_type,
                payload=payload,
            )
            return
        try:
            await self.event_bus.publish(
                session_id=decision.session_id,
                call_id=fs_uuid,
                source="system",
                type=event_type,
                payload=payload,
            )
        except Exception:
            logger.debug(
                "voice event publish failed type=%s fs_uuid=%s",
                event_type,
                fs_uuid,
                exc_info=True,
            )

    async def ensure_provider_available(self, provider: str, fs_uuid: str) -> None:
        circuit = self.provider_circuits.get(provider)
        if circuit is None:
            return
        allowed, event_type = circuit.allow()
        if event_type == "provider.circuit_closed":
            await self.publish_provider_circuit_event(fs_uuid, circuit, event_type)
        if allowed:
            return
        await self.publish_provider_circuit_event(
            fs_uuid, circuit, "provider.circuit_blocked"
        )
        raise ProviderCircuitOpenError(provider)

    async def record_provider_success(self, provider: str, fs_uuid: str) -> None:
        circuit = self.provider_circuits.get(provider)
        if circuit is None:
            return
        event_type = circuit.record_success()
        if event_type:
            await self.publish_provider_circuit_event(fs_uuid, circuit, event_type)

    async def record_provider_failure(
        self, provider: str, fs_uuid: str, exc: Exception
    ) -> None:
        circuit = self.provider_circuits.get(provider)
        if circuit is None:
            return
        event_type = circuit.record_failure()
        if event_type:
            await self.publish_provider_circuit_event(
                fs_uuid,
                circuit,
                event_type,
                error=_provider_error_payload(provider, exc),
            )

    async def publish_provider_circuit_event(
        self,
        fs_uuid: str,
        circuit: ProviderCircuitBreaker,
        event_type: str,
        *,
        error: dict[str, str] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "provider": circuit.provider,
            "state": "open" if circuit.is_open else "closed",
            "failure_count": circuit.failure_count,
            "failure_threshold": circuit.failure_threshold,
            "reset_seconds": circuit.reset_seconds,
        }
        if error:
            payload["error"] = error
        await self.publish_voice_event(
            fs_uuid, source="system", type=event_type, payload=payload
        )

    def _event_context(self, fs_uuid: str) -> tuple[str, str]:
        session = self.find_session(fs_uuid)
        if not session:
            return fs_uuid, fs_uuid
        return session.session_id, fs_uuid

    def reset_turn_latency(
        self, fs_uuid: str, *, speech_started_at: float | None = None
    ) -> TurnLatencyState:
        state = TurnLatencyState(speech_started_at=speech_started_at)
        self.turn_latency[fs_uuid] = state
        return state

    def turn_latency_state(self, fs_uuid: str) -> TurnLatencyState:
        return self.turn_latency.setdefault(fs_uuid, TurnLatencyState())

    def note_stt_first_partial(self, fs_uuid: str) -> None:
        state = self.turn_latency_state(fs_uuid)
        if state.first_partial_at is None:
            state.first_partial_at = time.perf_counter()

    def note_stt_endpoint(self, fs_uuid: str) -> None:
        self.turn_latency_state(fs_uuid).endpoint_at = time.perf_counter()

    def note_stt_final(self, fs_uuid: str) -> None:
        state = self.turn_latency_state(fs_uuid)
        if state.final_at is None:
            state.final_at = time.perf_counter()

    def note_policy_evaluation(
        self, fs_uuid: str, result: PolicyEvaluationResult
    ) -> None:
        state = self.turn_latency_state(fs_uuid)
        state.semantic_ms = result.semantic_ms
        state.policy_decision_ms = result.policy_decision_ms
        state.policy_evaluation_ms = result.policy_evaluation_ms

    def note_policy_decision(self, fs_uuid: str) -> None:
        self.turn_latency_state(fs_uuid).policy_decision_at = time.perf_counter()

    def stt_policy_latency_payload(
        self,
        fs_uuid: str,
        *,
        llm_started_at: float | None = None,
    ) -> dict[str, Any]:
        state = self.turn_latency.get(fs_uuid)
        if not state:
            return {}
        payload: dict[str, Any] = {}
        add_elapsed(
            payload,
            "speech_to_first_partial_ms",
            state.speech_started_at,
            state.first_partial_at,
        )
        add_elapsed(
            payload, "speech_to_endpoint_ms", state.speech_started_at, state.endpoint_at
        )
        add_elapsed(payload, "endpoint_to_final_ms", state.endpoint_at, state.final_at)
        add_elapsed(
            payload, "speech_to_final_ms", state.speech_started_at, state.final_at
        )
        add_elapsed(payload, "final_to_llm_request_ms", state.final_at, llm_started_at)
        add_elapsed(
            payload,
            "policy_to_llm_request_ms",
            state.policy_decision_at,
            llm_started_at,
        )
        if state.semantic_ms is not None:
            payload["semantic_ms"] = state.semantic_ms
        if state.policy_decision_ms is not None:
            payload["policy_decision_ms"] = state.policy_decision_ms
        if state.policy_evaluation_ms is not None:
            payload["policy_evaluation_ms"] = state.policy_evaluation_ms
        return payload

    async def mark_user_speech_started(
        self, fs_uuid: str, payload: dict[str, Any]
    ) -> None:
        self.cancel_pending_turn_ack(fs_uuid)
        if payload.get("stt_type") == "speech_started":
            self.cancel_pending_delivery_resume(fs_uuid)
        if fs_uuid in self.active_user_speech:
            return
        self.reset_turn_latency(fs_uuid, speech_started_at=time.perf_counter())
        self.active_user_speech.add(fs_uuid)
        inferred = payload.get("stt_type") != "speech_started"
        if inferred:
            await self.publish_voice_event(
                fs_uuid,
                source="stt",
                type="stt.activity_started",
                payload={
                    **payload,
                    "inferred": True,
                    "inference": "first_stt_activity",
                },
            )
        user_payload = {**payload, "inferred": inferred}
        if inferred:
            user_payload["inference"] = "first_stt_activity"
        else:
            user_payload["source_event"] = "stt.speech_started"
        await self.publish_voice_event(
            fs_uuid,
            source="stt",
            type="user.speech_started",
            payload=user_payload,
        )
        if payload.get("stt_type") == "speech_started":
            self.schedule_speech_start_barge_in(fs_uuid, payload)

    async def mark_user_speech_stopped(
        self, fs_uuid: str, payload: dict[str, Any]
    ) -> None:
        self.cancel_speech_start_barge_task(fs_uuid)
        if fs_uuid not in self.active_user_speech:
            return
        self.active_user_speech.discard(fs_uuid)
        inferred = payload.get("stt_type") not in {"speech_stopped", "endpoint"}
        user_payload = {**payload, "inferred": inferred}
        if inferred:
            user_payload["inference"] = "stt_final_or_endpoint"
        else:
            user_payload["source_event"] = (
                "stt.endpoint"
                if payload.get("stt_type") == "endpoint"
                else "stt.speech_stopped"
            )
        await self.publish_voice_event(
            fs_uuid,
            source="stt",
            type="user.speech_stopped",
            payload=user_payload,
        )

    def schedule_speech_start_barge_in(
        self, fs_uuid: str, payload: dict[str, Any]
    ) -> None:
        self.cancel_speech_start_barge_task(fs_uuid)
        if not speech_start_barge_in_enabled():
            return
        if not self.should_break_active_speech_on_speech_started(fs_uuid):
            return
        delay_ms = speech_start_barge_in_debounce_ms()
        task = asyncio.create_task(
            self.break_speech_after_speech_start_debounce(
                fs_uuid, payload, delay_ms=delay_ms
            ),
            name=f"speech-start-barge-{fs_uuid}",
        )
        self.speech_start_barge_tasks[fs_uuid] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(
            lambda completed, uuid=fs_uuid: self.discard_speech_start_barge_task(
                uuid, completed
            )
        )

    def cancel_speech_start_barge_task(self, fs_uuid: str) -> None:
        task = self.speech_start_barge_tasks.pop(fs_uuid, None)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task and task is not current and not task.done():
            task.cancel()

    def discard_speech_start_barge_task(
        self, fs_uuid: str, task: asyncio.Task
    ) -> None:
        if self.speech_start_barge_tasks.get(fs_uuid) is task:
            self.speech_start_barge_tasks.pop(fs_uuid, None)

    def should_cancel_pending_delivery_resume_for_event(
        self, event: dict[str, Any]
    ) -> bool:
        text = str(event.get("text") or "").strip()
        if not text:
            return False
        return not is_delivery_pause_control_text(text)

    def schedule_pending_delivery_resume(
        self, session: CallSession, fs_uuid: str, turn_id: str
    ) -> None:
        context = session.metadata.get(PREVIOUS_ASSISTANT_DELIVERY_METADATA_KEY)
        if not isinstance(context, dict):
            return
        self.cancel_pending_delivery_resume(fs_uuid)
        delay_seconds = delivery_resume_delay_seconds()
        task = asyncio.create_task(
            self.resume_interrupted_delivery_after_delay(
                session, fs_uuid, turn_id, delay_seconds
            ),
            name=f"delivery-resume-{fs_uuid}",
        )
        self.pending_delivery_resume_tasks[fs_uuid] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(
            lambda completed, uuid=fs_uuid: self.discard_pending_delivery_resume_task(
                uuid, completed
            )
        )

    def cancel_pending_delivery_resume(self, fs_uuid: str) -> None:
        task = self.pending_delivery_resume_tasks.pop(fs_uuid, None)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task and task is not current and not task.done():
            task.cancel()

    def discard_pending_delivery_resume_task(
        self, fs_uuid: str, task: asyncio.Task
    ) -> None:
        if self.pending_delivery_resume_tasks.get(fs_uuid) is task:
            self.pending_delivery_resume_tasks.pop(fs_uuid, None)

    async def resume_interrupted_delivery_after_delay(
        self,
        session: CallSession,
        fs_uuid: str,
        turn_id: str,
        delay_seconds: float,
    ) -> None:
        try:
            await asyncio.sleep(max(0.0, delay_seconds))
        except asyncio.CancelledError:
            return
        current = asyncio.current_task()
        if self.pending_delivery_resume_tasks.get(fs_uuid) is not current:
            return
        self.pending_delivery_resume_tasks.pop(fs_uuid, None)
        if (
            self.find_session(fs_uuid) is not session
            or session.state == SessionState.ENDED
        ):
            return
        if fs_uuid in self.active_user_speech:
            return
        active_llm = self.active_llm_tasks.get(fs_uuid)
        if active_llm and not active_llm.done():
            return
        context = session.metadata.pop(PREVIOUS_ASSISTANT_DELIVERY_METADATA_KEY, None)
        if not isinstance(context, dict):
            return
        undelivered_text = str(context.get("undelivered_text") or "").strip()
        if not undelivered_text:
            return
        resume_text = f"{DELIVERY_RESUME_BRIDGE_PROMPT} {undelivered_text}"
        await self.publish_voice_event(
            fs_uuid,
            source="system",
            type="delivery.auto_resume",
            payload={
                **delivery_context_event_payload(context),
                "resume_delay_seconds": delay_seconds,
                **describe_text_for_voice(resume_text),
            },
        )
        session.state = SessionState.SPEAKING
        await self.speak(
            fs_uuid,
            resume_text,
            wait_complete=True,
            reason="assistant_response",
            turn_id=turn_id,
            history_session=session,
            generated_text=resume_text,
            event_payload={"delivery_resume": True},
        )
        if not self.is_agent_speaking(fs_uuid):
            session.state = SessionState.LISTENING

    def should_break_active_speech_on_speech_started(self, fs_uuid: str) -> bool:
        session = self.find_session(fs_uuid)
        has_active_tts = self.is_agent_speaking(fs_uuid) or fs_uuid in self.active_speech
        return (
            bool(session)
            and session.mode == SessionMode.ASSISTANT
            and fs_uuid in self.active_user_speech
            and has_active_tts
            and self.tts_allows_interruptions(fs_uuid)
        )

    async def break_speech_after_speech_start_debounce(
        self, fs_uuid: str, payload: dict[str, Any], *, delay_ms: int
    ) -> None:
        try:
            await asyncio.sleep(max(0, delay_ms) / 1000.0)
        except asyncio.CancelledError:
            return
        current = asyncio.current_task()
        if self.speech_start_barge_tasks.get(fs_uuid) is not current:
            return
        self.speech_start_barge_tasks.pop(fs_uuid, None)
        if not self.should_break_active_speech_on_speech_started(fs_uuid):
            return
        await self.publish_voice_event(
            fs_uuid,
            source="policy",
            type="user.barge_in_detected",
            payload={
                "text": "",
                "action": PolicyAction.CANCEL_TTS_AND_LISTEN.value,
                "reason": "Caller speech continued while interruptible TTS was active.",
                "is_final": False,
                "trigger": "speech_started",
                "debounce_ms": delay_ms,
                "item_id": payload.get("item_id"),
            },
        )
        self.cancel_active_llm_stream(fs_uuid)
        await self.break_speech(
            fs_uuid,
            reason="barge_in_or_new_turn",
            publish_events=True,
        )

    async def speak(
        self,
        fs_uuid: str,
        text: str,
        *,
        language: str | None = None,
        interruptible: bool = True,
        wait_complete: bool = False,
        reason: str = "voice_response",
        turn_id: str | None = None,
        history_session: CallSession | None = None,
        event_payload: dict[str, Any] | None = None,
        tracking_text: str | None = None,
        generated_text: str | None = None,
        replace_active_speech: bool = True,
    ) -> dict[str, Any]:
        """Queue text-to-speech, publish timing events, and track active playback state."""
        text_stats = describe_text_for_voice(text)
        tts_event_uuid = str(uuid.uuid4())
        payload = {
            "text": text,
            "language": language,
            "interruptible": interruptible,
            "event_lock_requested": wait_complete,
            "reason": reason,
            "tts_event_uuid": tts_event_uuid,
            **text_stats,
        }
        if event_payload:
            payload.update(event_payload)
        if turn_id:
            payload["turn_id"] = turn_id
        await self.publish_voice_event(
            fs_uuid, source="tts", type="tts.started", payload=payload
        )
        await self.publish_voice_event(
            fs_uuid, source="tts", type="tts.enqueue_started", payload=payload
        )
        enqueue_started_at = time.perf_counter()
        tts_result: dict[str, Any] | None = None
        try:
            await self.ensure_provider_available("tts", fs_uuid)
            raw_tts_result = await self.tts_client.speak(
                fs_uuid,
                text,
                language=language,
                interruptible=interruptible,
                wait_complete=wait_complete,
                event_uuid=tts_event_uuid,
                fs_host=self.fs_host_for_uuid(fs_uuid),
            )
            await self.record_provider_success("tts", fs_uuid)
            if isinstance(raw_tts_result, dict):
                tts_result = raw_tts_result
        except Exception as exc:
            if not isinstance(exc, ProviderCircuitOpenError):
                await self.record_provider_failure("tts", fs_uuid, exc)
            await self.publish_voice_event(
                fs_uuid,
                source="tts",
                type="tts.error",
                payload={
                    **payload,
                    "error": {
                        "type": "tts_upstream_error",
                        "message": "TTS request failed",
                    },
                },
            )
            if wait_complete:
                self.clear_active_speech(fs_uuid)
                self.mark_not_speaking(fs_uuid)
            raise
        enqueue_latency_ms = elapsed_ms(enqueue_started_at)
        tracked_text = tracking_text if tracking_text is not None else text
        estimated_playback_seconds = estimate_tts_playback_seconds(tracked_text)
        estimated_start_delay_seconds = estimate_tts_start_delay_seconds(
            estimated_playback_seconds
        )
        chunk_estimated_playback_seconds = estimate_tts_playback_seconds(text)
        enqueued_payload = {
            **payload,
            "enqueue_latency_ms": enqueue_latency_ms,
            "estimated_start_delay_seconds": estimated_start_delay_seconds,
            "estimated_start_delay_ms": round(estimated_start_delay_seconds * 1000, 1),
            "estimated_playback_seconds": estimated_playback_seconds,
            "estimated_playback_ms": round(estimated_playback_seconds * 1000, 1),
            "playback_timing_source": "estimated",
        }
        if tts_result:
            command_latency_ms = safe_float(tts_result.get("command_latency_ms"), 0.0)
            if command_latency_ms > 0:
                enqueued_payload["tts_command_latency_ms"] = command_latency_ms
                enqueued_payload["tts_control_timing_source"] = (
                    "freeswitch_sendmsg_round_trip"
                )
            if tts_result.get("event_lock_requested"):
                enqueued_payload["tts_event_lock_requested"] = True
            if tts_result.get("event_uuid"):
                enqueued_payload["tts_event_uuid"] = str(tts_result["event_uuid"])
        if tracked_text != text:
            enqueued_payload.update(
                {
                    "tracking_text_chars": len(tracked_text),
                    "tracking_text_words": len(tracked_text.split()),
                    "chunk_estimated_playback_seconds": chunk_estimated_playback_seconds,
                    "chunk_estimated_playback_ms": round(
                        chunk_estimated_playback_seconds * 1000, 1
                    ),
                }
            )
        await self.publish_voice_event(
            fs_uuid, source="tts", type="tts.enqueued", payload=enqueued_payload
        )
        self.track_active_speech(
            fs_uuid,
            enqueued_payload,
            history_session=history_session,
            history_text=tracked_text if history_session else None,
            generated_text=generated_text,
            replace=replace_active_speech,
        )
        if wait_complete:
            self.mark_speaking(fs_uuid)
        timing_result = {
            "enqueue_latency_ms": enqueue_latency_ms,
            "estimated_start_delay_seconds": estimated_start_delay_seconds,
            "estimated_start_delay_ms": round(estimated_start_delay_seconds * 1000, 1),
            "estimated_playback_seconds": estimated_playback_seconds,
            "estimated_playback_ms": round(estimated_playback_seconds * 1000, 1),
            "chunk_estimated_playback_seconds": chunk_estimated_playback_seconds,
            "chunk_estimated_playback_ms": round(
                chunk_estimated_playback_seconds * 1000, 1
            ),
            **text_stats,
        }
        if tts_result:
            command_latency_ms = safe_float(tts_result.get("command_latency_ms"), 0.0)
            if command_latency_ms > 0:
                timing_result["tts_command_latency_ms"] = command_latency_ms
            if tts_result.get("event_lock_requested"):
                timing_result["tts_event_lock_requested"] = True
            if tts_result.get("event_uuid"):
                timing_result["tts_event_uuid"] = str(tts_result["event_uuid"])
        timing_result.setdefault("tts_event_uuid", tts_event_uuid)
        return timing_result

    def track_active_speech(
        self,
        fs_uuid: str,
        payload: dict[str, Any],
        *,
        history_session: CallSession | None = None,
        history_text: str | None = None,
        generated_text: str | None = None,
        replace: bool = True,
    ) -> None:
        previous = self.active_speech.get(fs_uuid)
        if replace or previous is None:
            self.clear_active_speech(fs_uuid)
            previous = None
            self.soft_interjections_during_tts[fs_uuid] = 0
        elif previous.finish_task and not previous.finish_task.done():
            previous.finish_task.cancel()
            self.untrack_active_speech_event_uuid(previous)
        finish_task = asyncio.create_task(
            self.finish_speech_after_delay(fs_uuid, payload),
            name=f"tts-finish-{fs_uuid}",
        )
        if previous is None:
            start_task = asyncio.create_task(
                self.start_speech_after_delay(fs_uuid, payload),
                name=f"tts-start-{fs_uuid}",
            )
            if history_text is not None:
                active_history_text = history_text
            elif history_session:
                active_history_text = str(payload.get("text") or "")
            else:
                active_history_text = None
            self.active_speech[fs_uuid] = ActiveSpeech(
                payload=payload,
                start_task=start_task,
                finish_task=finish_task,
                enqueued_at=time.perf_counter(),
                history_session=history_session,
                history_text=active_history_text,
                generated_text=generated_text or active_history_text,
            )
            self.track_active_speech_event_uuid(fs_uuid, payload)
            self.tasks.add(start_task)
            start_task.add_done_callback(self.tasks.discard)
        else:
            previous.payload = payload
            previous.finish_task = finish_task
            previous.history_session = history_session or previous.history_session
            if history_text is not None:
                previous.history_text = history_text
            if generated_text:
                self.update_active_speech_generated_text(fs_uuid, generated_text)
            self.track_active_speech_event_uuid(fs_uuid, payload)
        self.tasks.add(finish_task)
        finish_task.add_done_callback(self.tasks.discard)

    def track_active_speech_event_uuid(
        self, fs_uuid: str, payload: dict[str, Any]
    ) -> None:
        event_uuid = str(payload.get("tts_event_uuid") or "").strip()
        if event_uuid:
            self.active_speech_by_event_uuid[event_uuid] = fs_uuid

    def untrack_active_speech_event_uuid(self, active: ActiveSpeech) -> None:
        event_uuid = str(active.payload.get("tts_event_uuid") or "").strip()
        if event_uuid:
            self.active_speech_by_event_uuid.pop(event_uuid, None)

    def update_active_speech_generated_text(
        self, fs_uuid: str, generated_text: str
    ) -> None:
        active = self.active_speech.get(fs_uuid)
        clean_text = str(generated_text or "")
        if not active or not clean_text:
            return
        previous = active.generated_text or ""
        if not previous or len(clean_text) >= len(previous):
            active.generated_text = clean_text

    def clear_active_speech(self, fs_uuid: str) -> ActiveSpeech | None:
        active = self.active_speech.pop(fs_uuid, None)
        self.soft_interjections_during_tts.pop(fs_uuid, None)
        self.cancel_speech_start_barge_task(fs_uuid)
        if active:
            self.untrack_active_speech_event_uuid(active)
            active.start_task.cancel()
            active.finish_task.cancel()
        return active

    def active_speech_heard_snapshot(
        self, active: ActiveSpeech, *, force_complete: bool = False
    ) -> dict[str, Any]:
        text = (
            active.history_text
            if active.history_text is not None
            else str(active.payload.get("text") or "")
        )
        if force_complete:
            heard_fraction = 1.0 if text else 0.0
        elif active.playback_started_at is None:
            heard_fraction = 0.0
        else:
            estimated_playback_ms = safe_float(
                active.payload.get("estimated_playback_ms"), 0.0
            )
            if estimated_playback_ms <= 0:
                heard_fraction = 0.0
            else:
                heard_fraction = max(
                    0.0,
                    min(
                        1.0,
                        elapsed_ms(active.playback_started_at) / estimated_playback_ms,
                    ),
                )
        spoken_text = spoken_text_prefix(text, heard_fraction)
        return {
            "heard_fraction": round(heard_fraction, 3),
            "spoken_text": spoken_text,
            "spoken_text_chars": len(spoken_text),
        }

    def commit_active_speech_history(
        self, active: ActiveSpeech, *, force_complete: bool = False
    ) -> dict[str, Any]:
        payload = self.active_speech_heard_snapshot(
            active, force_complete=force_complete
        )
        committed = False
        if (
            active.history_session
            and not active.history_committed
            and payload["spoken_text"]
        ):
            active.history_session.history.append(
                {"role": "assistant", "content": payload["spoken_text"]}
            )
            active.history_committed = True
            committed = True
        payload["history_committed"] = committed
        return payload

    def delivery_context_for_interrupted_speech(
        self,
        active: ActiveSpeech,
        *,
        interruption_reason: str,
        latest_user_text: str | None = None,
    ) -> dict[str, Any] | None:
        if not active.history_session:
            return None
        if str(active.payload.get("reason") or "") != "assistant_response":
            return None
        generated_text = str(
            active.generated_text
            or active.history_text
            or active.payload.get("text")
            or ""
        )
        if not generated_text:
            return None
        heard_payload = self.active_speech_heard_snapshot(active)
        delivered_text = str(heard_payload.get("spoken_text") or "")
        undelivered_text = undelivered_suffix(generated_text, delivered_text)
        if not undelivered_text:
            return None
        if active.playback_started_at is None:
            delivery_status = "cancelled_before_playback"
        else:
            delivery_status = "interrupted"
        delivered_fraction = (
            len(delivered_text) / len(generated_text) if generated_text else 0.0
        )
        context: dict[str, Any] = {
            "delivery_status": delivery_status,
            "generated_text": generated_text,
            "delivered_text": delivered_text,
            "undelivered_text": undelivered_text,
            "delivered_fraction": round(max(0.0, min(1.0, delivered_fraction)), 3),
            "interruption_reason": interruption_reason,
            "turn_id": active.payload.get("turn_id"),
            "generated_text_chars": len(generated_text),
            "delivered_text_chars": len(delivered_text),
            "undelivered_text_chars": len(undelivered_text),
            "playback_started": active.playback_started_at is not None,
            "playback_timing_source": active.payload.get("playback_timing_source"),
        }
        if latest_user_text:
            context["interruption_user_text"] = latest_user_text
            context["latest_user_text"] = latest_user_text
        return context

    async def store_delivery_context(
        self,
        fs_uuid: str,
        session: CallSession,
        context: dict[str, Any],
    ) -> None:
        session.metadata[PREVIOUS_ASSISTANT_DELIVERY_METADATA_KEY] = context
        event_payload = delivery_context_event_payload(context)
        await self.publish_voice_event(
            fs_uuid,
            source="system",
            type="delivery.response_interrupted",
            payload=event_payload,
        )
        await self.publish_voice_event(
            fs_uuid,
            source="system",
            type="delivery.context_created",
            payload=event_payload,
        )

    def consume_delivery_context(
        self, session: CallSession, *, latest_user_text: str
    ) -> dict[str, Any] | None:
        context = session.metadata.pop(PREVIOUS_ASSISTANT_DELIVERY_METADATA_KEY, None)
        if not isinstance(context, dict):
            return None
        copied = dict(context)
        copied["latest_user_text"] = latest_user_text
        return copied

    async def start_speech_after_delay(
        self, fs_uuid: str, payload: dict[str, Any]
    ) -> None:
        try:
            await asyncio.sleep(
                float(payload.get("estimated_start_delay_seconds") or 0.0)
            )
        except asyncio.CancelledError:
            return
        active = self.active_speech.get(fs_uuid)
        if not active or active.start_task is not asyncio.current_task():
            return
        active.playback_started_at = time.perf_counter()
        self.mark_speaking(fs_uuid)
        await self.publish_voice_event(
            fs_uuid,
            source="tts",
            type="agent.speaking_started",
            payload={
                **payload,
                "playback_start_timing": "estimated_fallback",
                "playback_started_ms": elapsed_ms(active.enqueued_at),
            },
        )

    async def finish_speech_after_delay(
        self, fs_uuid: str, payload: dict[str, Any]
    ) -> None:
        try:
            await asyncio.sleep(
                float(payload.get("estimated_start_delay_seconds") or 0.0)
                + float(payload["estimated_playback_seconds"])
            )
        except asyncio.CancelledError:
            return
        active = self.active_speech.get(fs_uuid)
        if not active or active.finish_task is not asyncio.current_task():
            return
        self.active_speech.pop(fs_uuid, None)
        self.soft_interjections_during_tts.pop(fs_uuid, None)
        self.untrack_active_speech_event_uuid(active)
        self.mark_not_speaking(fs_uuid)
        history_payload = self.commit_active_speech_history(active, force_complete=True)
        finished_payload = {
            **payload,
            **history_payload,
            "playback_completion_timing": "estimated_fallback",
            "playback_completed_ms": elapsed_ms(active.enqueued_at),
        }
        await self.publish_voice_event(
            fs_uuid, source="tts", type="tts.finished", payload=finished_payload
        )
        await self.publish_voice_event(
            fs_uuid,
            source="tts",
            type="agent.speaking_stopped",
            payload=finished_payload,
        )

    async def handle_freeswitch_execute_event(
        self,
        default_fs_uuid: str,
        event: dict[str, Any],
        *,
        completed: bool,
    ) -> bool:
        app = _context_value(event, "Application")
        event_uuid = _context_value(event, "Application-UUID")
        if app != "speak" or not event_uuid:
            return False
        fs_uuid = self.active_speech_by_event_uuid.get(event_uuid)
        if not fs_uuid:
            fs_uuid = (
                _context_value(event, "Unique-ID")
                or _context_value(event, "Channel-Call-UUID")
                or default_fs_uuid
            )
        active = self.active_speech.get(fs_uuid)
        if not active or str(active.payload.get("tts_event_uuid") or "") != event_uuid:
            return False
        if completed:
            await self.finish_speech_from_freeswitch_event(fs_uuid, active, event)
            return True
        await self.start_speech_from_freeswitch_event(fs_uuid, active, event)
        return True

    async def start_speech_from_freeswitch_event(
        self,
        fs_uuid: str,
        active: ActiveSpeech,
        event: dict[str, Any],
    ) -> None:
        if active.playback_started_at is not None:
            return
        active.playback_started_at = time.perf_counter()
        if not active.start_task.done():
            active.start_task.cancel()
        self.mark_speaking(fs_uuid)
        await self.publish_voice_event(
            fs_uuid,
            source="tts",
            type="agent.speaking_started",
            payload={
                **active.payload,
                "playback_timing_source": "freeswitch_channel_event",
                "playback_start_timing": "freeswitch_channel_execute",
                "playback_started_ms": elapsed_ms(active.enqueued_at),
                "tts_event_uuid": _context_value(event, "Application-UUID"),
                "fs_event_name": _context_value(event, "Event-Name")
                or "CHANNEL_EXECUTE",
            },
        )

    async def finish_speech_from_freeswitch_event(
        self,
        fs_uuid: str,
        active: ActiveSpeech,
        event: dict[str, Any],
    ) -> None:
        if active.playback_started_at is None:
            await self.start_speech_from_freeswitch_event(fs_uuid, active, event)
        current = self.active_speech.get(fs_uuid)
        if current is not active:
            return
        self.active_speech.pop(fs_uuid, None)
        self.soft_interjections_during_tts.pop(fs_uuid, None)
        self.untrack_active_speech_event_uuid(active)
        if not active.start_task.done():
            active.start_task.cancel()
        if not active.finish_task.done():
            active.finish_task.cancel()
        self.mark_not_speaking(fs_uuid)
        history_payload = self.commit_active_speech_history(active, force_complete=True)
        completed_ms = elapsed_ms(active.enqueued_at)
        finished_payload = {
            **active.payload,
            **history_payload,
            "playback_timing_source": "freeswitch_channel_event",
            "playback_completion_timing": "freeswitch_channel_execute_complete",
            "playback_started_ms": (
                round((active.playback_started_at - active.enqueued_at) * 1000, 1)
                if active.playback_started_at is not None
                else completed_ms
            ),
            "playback_completed_ms": completed_ms,
            "tts_event_uuid": _context_value(event, "Application-UUID"),
            "fs_event_name": _context_value(event, "Event-Name")
            or "CHANNEL_EXECUTE_COMPLETE",
        }
        application_response = _context_value(event, "Application-Response")
        if application_response:
            finished_payload["application_response"] = application_response
        await self.publish_voice_event(
            fs_uuid, source="tts", type="tts.finished", payload=finished_payload
        )
        await self.publish_voice_event(
            fs_uuid,
            source="tts",
            type="agent.speaking_stopped",
            payload=finished_payload,
        )

    def is_agent_speaking(self, fs_uuid: str) -> bool:
        if fs_uuid in self.active_speech:
            return True
        session = self.find_session(fs_uuid)
        return bool(session and session.state == SessionState.SPEAKING)

    def mark_speaking(self, fs_uuid: str) -> None:
        session = self.find_session(fs_uuid)
        if session and session.state != SessionState.ENDED:
            session.state = SessionState.SPEAKING

    def mark_not_speaking(self, fs_uuid: str) -> None:
        session = self.find_session(fs_uuid)
        if session and session.state == SessionState.SPEAKING:
            session.state = SessionState.LISTENING

    def cancel_active_llm_stream(self, fs_uuid: str) -> bool:
        task = self.active_llm_tasks.get(fs_uuid)
        current = asyncio.current_task()
        if not task or task.done() or task is current:
            return False
        task.cancel()
        return True

    async def start_audio_stream(
        self, fs_session: Any, fs_uuid: str, leg_id: str = "a"
    ) -> bool:
        base_url = os.getenv("VOICE_GATEWAY_MEDIA_URL", "ws://voice-gateway:8000/media")
        session = self.find_session(fs_uuid)
        session_id = session.session_id if session else fs_uuid
        sample_rate = os.getenv("VOICE_GATEWAY_AUDIO_STREAM_RATE", "16000")
        metadata = json.dumps(
            {"session_id": session_id, "leg_id": leg_id}, separators=(",", ":")
        )
        command = build_audio_stream_start_command(
            fs_uuid,
            f"{base_url}/{fs_uuid}",
            mix_type="mono",
            sample_rate=sample_rate,
            metadata=metadata,
        )
        fs_host = _context_value(
            getattr(fs_session, "context", {}) or {}, "variable_x_fs_host"
        ) or os.getenv("FREESWITCH_HOST", "freeswitch")
        event_payload = {
            "transport": "mod_audio_stream",
            "leg_id": leg_id,
            "media_stream_id": fs_uuid,
            "sample_rate": sample_rate,
            "fs_host": fs_host,
        }
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="media.stream_start_requested",
            payload=event_payload,
        )
        command_started_at = time.perf_counter()
        try:
            reply = await self.fs_control.api(command, fs_host=fs_host)
        except Exception as exc:
            await self.publish_voice_event(
                fs_uuid,
                source="esl",
                type="media.stream_start_ack",
                payload={
                    **event_payload,
                    "command_latency_ms": elapsed_ms(command_started_at),
                    "command_success": False,
                    "error": str(exc),
                },
            )
            logger.warning(
                "audio stream start failed fs_uuid=%s leg_id=%s error=%s",
                fs_uuid,
                leg_id,
                exc,
            )
            await self.publish_voice_event(
                fs_uuid,
                source="esl",
                type="system.error",
                payload={"message": "audio stream start failed", "error": str(exc)},
            )
            return False
        reply_text = _reply_text(reply)
        if _reply_is_error(reply_text):
            await self.publish_voice_event(
                fs_uuid,
                source="esl",
                type="media.stream_start_ack",
                payload={
                    **event_payload,
                    "command_latency_ms": elapsed_ms(command_started_at),
                    "command_success": False,
                    "reply": reply_text,
                },
            )
            logger.error(
                "audio stream start rejected fs_uuid=%s leg_id=%s reply=%s",
                fs_uuid,
                leg_id,
                reply_text,
            )
            await self.publish_voice_event(
                fs_uuid,
                source="esl",
                type="system.error",
                payload={"message": "audio stream start rejected", "reply": reply_text},
            )
            return False
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="media.stream_start_ack",
            payload={
                **event_payload,
                "command_latency_ms": elapsed_ms(command_started_at),
                "command_success": True,
                "reply": reply_text,
            },
        )
        logger.info(
            "audio stream start requested fs_uuid=%s leg_id=%s session_id=%s reply=%s",
            fs_uuid,
            leg_id,
            session_id,
            reply_text,
        )
        return True

    async def _prepare_translation_peer(
        self, fs_session: Any, peer_uuid: str, *, fs_host: str | None = None
    ) -> None:
        leg = self.find_leg(peer_uuid)
        if not leg:
            return
        attempts = env_int("TRANSLATION_PEER_STREAM_ATTEMPTS", 12)
        for attempt in range(attempts):
            await asyncio.sleep(env_float("TRANSLATION_PEER_STREAM_RETRY_SECONDS", 1.0))
            if not await self.uuid_exists(peer_uuid, fs_host=fs_host):
                logger.debug(
                    "translation peer not available yet peer_uuid=%s attempt=%s/%s",
                    peer_uuid,
                    attempt + 1,
                    attempts,
                )
                continue
            if not await self.start_audio_stream(
                fs_session, peer_uuid, leg_id=leg.leg_id
            ):
                session = self.find_session(peer_uuid)
                if session:
                    await self.fail_translation_setup(
                        session.primary_leg().fs_uuid,
                        message="translation peer media stream failed",
                        peer_uuid=peer_uuid,
                    )
                return
            self.start_stt_task(peer_uuid)
            logger.info(
                "translation peer media started peer_uuid=%s leg_id=%s",
                peer_uuid,
                leg.leg_id,
            )
            return
        logger.warning(
            "translation peer did not become available peer_uuid=%s attempts=%s",
            peer_uuid,
            attempts,
        )
        session = self.find_session(peer_uuid)
        if session:
            await self.fail_translation_setup(
                session.primary_leg().fs_uuid,
                message="translation peer did not become available",
                peer_uuid=peer_uuid,
                attempts=attempts,
            )

    async def uuid_exists(self, fs_uuid: str, *, fs_host: str | None = None) -> bool:
        try:
            reply = await self.fs_control.api(f"uuid_exists {fs_uuid}", fs_host=fs_host)
        except Exception:
            return False
        text = " ".join(
            str(part or "")
            for part in (
                getattr(reply, "body", None),
                reply.get("Body") if hasattr(reply, "get") else None,
                reply.get("Reply-Text") if hasattr(reply, "get") else None,
            )
        ).lower()
        return "true" in text and "-err" not in text

    async def stop_audio_stream(self, fs_uuid: str) -> None:
        fs_session = self.control_session_for_uuid(fs_uuid)
        if fs_session:
            try:
                await fs_session.send(f"api {build_audio_stream_stop_command(fs_uuid)}")
            except Exception:
                logger.debug(
                    "audio stream stop skipped fs_uuid=%s", fs_uuid, exc_info=True
                )

    async def break_speech(
        self,
        fs_uuid: str,
        *,
        reason: str = "barge_in_or_interrupt",
        latest_user_text: str | None = None,
        publish_events: bool = True,
    ) -> bool:
        active = self.active_speech.get(fs_uuid)
        event_payload = self.break_speech_payload(reason=reason, active=active)
        command_started_at = time.perf_counter()
        if publish_events:
            await self.publish_voice_event(
                fs_uuid,
                source="tts",
                type="tts.cancel_requested",
                payload=event_payload,
            )
        command_result = await self.send_break_command(fs_uuid)
        command_sent = command_result["command_sent"]
        command_success = command_result["command_success"]
        if not publish_events:
            if command_success:
                if active:
                    self.commit_active_speech_history(active)
                self.clear_active_speech(fs_uuid)
                self.mark_not_speaking(fs_uuid)
            return command_success
        command_payload = {
            **event_payload,
            "command_sent": command_sent,
            "command_success": command_success,
            "command_path": command_result["command_path"],
            "command_latency_ms": elapsed_ms(command_started_at),
        }
        if command_result.get("fs_host"):
            command_payload["fs_host"] = command_result["fs_host"]
        if command_result.get("reply"):
            command_payload["reply"] = command_result["reply"]
        if command_result.get("error"):
            command_payload["error"] = command_result["error"]
        if command_success:
            delivery_context = (
                self.delivery_context_for_interrupted_speech(
                    active,
                    interruption_reason=reason,
                    latest_user_text=latest_user_text,
                )
                if active
                else None
            )
            history_payload = (
                self.commit_active_speech_history(active) if active else {}
            )
            command_payload.update(history_payload)
            if delivery_context and active and active.history_session:
                await self.store_delivery_context(
                    fs_uuid, active.history_session, delivery_context
                )
        await self.publish_voice_event(
            fs_uuid,
            source="tts",
            type="tts.break_sent",
            payload=command_payload,
        )
        if not command_success:
            await self.publish_voice_event(
                fs_uuid,
                source="tts",
                type="tts.error",
                payload={**command_payload, "message": "TTS break command failed"},
            )
            return False
        self.clear_active_speech(fs_uuid)
        self.mark_not_speaking(fs_uuid)
        await self.publish_voice_event(
            fs_uuid,
            source="tts",
            type="tts.cancelled",
            payload=command_payload,
        )
        await self.publish_voice_event(
            fs_uuid,
            source="tts",
            type="agent.speaking_stopped",
            payload=command_payload,
        )
        return True

    async def send_break_command(self, fs_uuid: str) -> dict[str, Any]:
        command = build_uuid_break_command(fs_uuid)
        fs_host = self.fs_host_for_uuid(fs_uuid)
        try:
            reply = await self.fs_control.api(command, fs_host=fs_host)
            reply_text = _reply_text(reply)
            if not _reply_is_error(reply_text):
                return {
                    "command_sent": True,
                    "command_success": True,
                    "command_path": "inbound_control",
                    "fs_host": fs_host,
                    "reply": reply_text,
                }
            logger.warning(
                "uuid_break rejected fs_uuid=%s fs_host=%s reply=%s",
                fs_uuid,
                fs_host,
                reply_text,
            )
            return {
                "command_sent": True,
                "command_success": False,
                "command_path": "inbound_control",
                "fs_host": fs_host,
                "reply": reply_text,
            }
        except Exception as exc:
            logger.warning(
                "uuid_break via inbound control failed fs_uuid=%s fs_host=%s error=%s",
                fs_uuid,
                fs_host,
                exc,
            )

        fs_session = self.control_session_for_uuid(fs_uuid)
        if not fs_session:
            return {
                "command_sent": False,
                "command_success": False,
                "command_path": "none",
                "fs_host": fs_host,
                "error": "no FreeSWITCH control session available",
            }
        try:
            reply = await fs_session.send(f"api {command}")
            reply_text = _reply_text(reply)
            return {
                "command_sent": True,
                "command_success": not _reply_is_error(reply_text),
                "command_path": "outbound_session_fallback",
                "fs_host": fs_host,
                "reply": reply_text,
            }
        except Exception as exc:
            logger.warning(
                "uuid_break via outbound session failed fs_uuid=%s error=%s",
                fs_uuid,
                exc,
            )
            return {
                "command_sent": False,
                "command_success": False,
                "command_path": "outbound_session_fallback",
                "fs_host": fs_host,
                "error": str(exc),
            }

    def fs_host_for_uuid(self, fs_uuid: str) -> str:
        fs_session = self.control_session_for_uuid(fs_uuid)
        context = getattr(fs_session, "context", {}) or {}
        return (
            _context_value(context, "variable_x_fs_host")
            or _context_value(context, "FreeSWITCH-Hostname")
            or os.getenv("FREESWITCH_HOST", "freeswitch")
        )

    def break_speech_payload(
        self, *, reason: str, active: ActiveSpeech | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": reason, "command": "uuid_break"}
        if not active:
            payload["active_speech"] = False
            payload["heard_fraction"] = 0.0
            payload["spoken_text"] = ""
            payload["spoken_text_chars"] = 0
            payload["history_committed"] = False
            return payload
        active_payload = active.payload
        payload.update(
            {
                "active_speech": True,
                "playback_started": active.playback_started_at is not None,
                "turn_id": active_payload.get("turn_id"),
                "active_reason": active_payload.get("reason"),
                "active_text_chars": active_payload.get("text_chars"),
                "active_text_words": active_payload.get("text_words"),
            }
        )
        payload.update(self.active_speech_heard_snapshot(active))
        payload["history_committed"] = False
        estimated_start_delay_ms = safe_float(
            active_payload.get("estimated_start_delay_ms"), 0.0
        )
        elapsed_active_ms = elapsed_ms(active.enqueued_at)
        if estimated_start_delay_ms:
            payload["estimated_start_delay_ms"] = estimated_start_delay_ms
            payload["elapsed_active_ms"] = elapsed_active_ms
            if active.playback_started_at is None:
                payload["remaining_estimated_start_delay_ms"] = max(
                    0.0,
                    round(estimated_start_delay_ms - elapsed_active_ms, 1),
                )
        estimated_playback_ms = safe_float(
            active_payload.get("estimated_playback_ms"), 0.0
        )
        if estimated_playback_ms:
            payload["estimated_playback_ms"] = estimated_playback_ms
            if active.playback_started_at is not None:
                elapsed_playback_ms = elapsed_ms(active.playback_started_at)
                payload["elapsed_playback_ms"] = elapsed_playback_ms
                payload["remaining_estimated_playback_ms"] = max(
                    0.0,
                    round(estimated_playback_ms - elapsed_playback_ms, 1),
                )
            else:
                payload["elapsed_playback_ms"] = 0.0
                payload["remaining_estimated_playback_ms"] = estimated_playback_ms
        return payload

    async def receive_audio(self, fs_uuid: str, frame: bytes) -> None:
        queue = self.audio_queues.get(fs_uuid)
        if queue is None:
            return
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            _ = queue.get_nowait()
            queue.put_nowait(frame)

    async def close_call(self, fs_uuid: str) -> None:
        session = self.find_session(fs_uuid)
        if not session:
            return
        await self.publish_voice_event(
            fs_uuid,
            source="esl",
            type="call.hangup",
            payload={"mode": session.mode.value, "state": session.state.value},
        )
        if session.mode == SessionMode.TRANSLATION:
            await self.hangup_translation_peers(session, closed_uuid=fs_uuid)
        session.state = SessionState.ENDED
        release = self.admission.release(session.session_id)
        await self.publish_admission_release(fs_uuid, release)
        for leg in list(session.legs.values()):
            await self.cancel_pending_assistant_turn(
                leg.fs_uuid, status="cancelled", reason="call_closed"
            )
            self.cancel_active_llm_stream(leg.fs_uuid)
            self.active_llm_tasks.pop(leg.fs_uuid, None)
            self.clear_active_speech(leg.fs_uuid)
            self.cancel_speech_start_barge_task(leg.fs_uuid)
            self.cancel_pending_delivery_resume(leg.fs_uuid)
            queue = self.audio_queues.pop(leg.fs_uuid, None)
            if queue is not None:
                await queue.put(None)
            await self.stop_audio_stream(leg.fs_uuid)
            self.sessions_by_uuid.pop(leg.fs_uuid, None)
            self.esl_sessions.pop(leg.fs_uuid, None)
            self.active_user_speech.discard(leg.fs_uuid)
            self.stt_partial_transcripts.pop(leg.fs_uuid, None)
            self.stt_lifecycle_items.pop(leg.fs_uuid, None)
            self.stt_unavailable_announced.discard(leg.fs_uuid)
            self.turn_latency.pop(leg.fs_uuid, None)
        self.sessions.pop(session.session_id, None)
        logger.info(
            "session ended session_id=%s mode=%s",
            session.session_id,
            session.mode.value,
        )

    async def hangup_translation_peers(
        self, session: CallSession, *, closed_uuid: str
    ) -> None:
        """Hang up parked translation legs when one side of the call closes."""
        fs_host = str(
            session.metadata.get("fs_host") or os.getenv("FREESWITCH_HOST", "freeswitch")
        )
        for leg in list(session.legs.values()):
            if leg.fs_uuid == closed_uuid:
                continue
            try:
                reply = await self.fs_control.api(
                    f"uuid_kill {leg.fs_uuid} NORMAL_CLEARING", fs_host=fs_host
                )
                logger.info(
                    "translation peer hangup requested fs_uuid=%s reply=%s",
                    leg.fs_uuid,
                    _reply_text(reply),
                )
            except Exception:
                logger.debug(
                    "translation peer inbound hangup failed fs_uuid=%s",
                    leg.fs_uuid,
                    exc_info=True,
                )
                with suppress(Exception):
                    await self.hangup_leg(leg.fs_uuid)

    async def announce_stt_unavailable(
        self, fs_uuid: str, detail: dict[str, Any] | None = None
    ) -> None:
        """Speak a caller-facing STT failure notice once per call leg."""
        if fs_uuid in self.stt_unavailable_announced:
            return
        if not self.find_session(fs_uuid):
            return
        self.stt_unavailable_announced.add(fs_uuid)
        error_type = ""
        error_message = ""
        if isinstance(detail, dict):
            error_type = str(detail.get("type") or "")
            error_message = str(detail.get("message") or "")
            error = detail.get("error")
            if isinstance(error, dict):
                error_type = error_type or str(error.get("type") or "")
                error_message = error_message or str(error.get("message") or "")
        try:
            await self.speak(
                fs_uuid,
                STT_UNAVAILABLE_SPOKEN_MESSAGE,
                language="en",
                interruptible=False,
                wait_complete=False,
                reason="stt_unavailable",
                event_payload={
                    "stt_error_type": error_type,
                    "stt_error_message": error_message,
                },
            )
        except Exception:
            logger.warning(
                "failed to announce STT unavailable fs_uuid=%s",
                fs_uuid,
                exc_info=True,
            )

    async def _run_stt(self, fs_uuid: str) -> None:
        queue = self.audio_queues[fs_uuid]
        leg = self.find_leg(fs_uuid)
        language = leg.source_language if leg else None
        stream_payload = {
            "language": language,
            "audio_queue_maxsize": queue.maxsize,
        }

        async def on_transcript(event: dict[str, Any]) -> None:
            await self.handle_transcript(fs_uuid, event)

        try:
            await self.ensure_provider_available("stt", fs_uuid)
            await self.publish_voice_event(
                fs_uuid,
                source="stt",
                type="stt.stream_started",
                payload=stream_payload,
            )
            await self.stt_client.stream_audio(
                fs_uuid, queue, on_transcript, language=language
            )
            await self.publish_voice_event(
                fs_uuid,
                source="stt",
                type="stt.stream_finished",
                payload={**stream_payload, "reason": "audio_queue_closed"},
            )
            await self.record_provider_success("stt", fs_uuid)
        except Exception as exc:
            if not isinstance(exc, ProviderCircuitOpenError):
                await self.record_provider_failure("stt", fs_uuid, exc)
            logger.warning("stt stream failed fs_uuid=%s error=%s", fs_uuid, exc)
            await self.publish_voice_event(
                fs_uuid,
                source="stt",
                type="stt.error",
                payload={
                    "detail": {
                        "type": "stt_stream_error",
                        "message": "Speech-to-text stream failed",
                        "error_type": exc.__class__.__name__,
                    }
                },
            )
            await self.announce_stt_unavailable(
                fs_uuid,
                {
                    "type": "stt_stream_error",
                    "message": "Speech-to-text stream failed",
                    "error_type": exc.__class__.__name__,
                },
            )

    async def handle_transcript(self, fs_uuid: str, event: dict[str, Any]) -> None:
        session = self.find_session(fs_uuid)
        event_type = str(event.get("type") or "")
        if event.get("type") == "error":
            detail = event.get("detail") or event
            await self.publish_voice_event(
                fs_uuid,
                source="stt",
                type="stt.error",
                payload={"detail": detail},
            )
            await self.announce_stt_unavailable(fs_uuid, detail)
            return
        if event.get("type") == "warning":
            await self.publish_voice_event(
                fs_uuid,
                source="stt",
                type="system.warning",
                payload={
                    "message": event.get("message") or "STT warning",
                    "detail": event.get("detail") or {},
                    "provider": event.get("provider"),
                    "fallback": event.get("fallback"),
                    "fallback_reason": event.get("fallback_reason"),
                },
            )
            return
        if event.get("type") == "suppressed":
            await self.publish_voice_event(
                fs_uuid,
                source="stt",
                type="stt.suppressed",
                payload={
                    **self.stt_event_payload(event),
                    "reason": event.get("reason") or "suppressed",
                },
            )
            return
        if event_type in {"speech_started", "speech_stopped", "endpoint"}:
            await self.publish_stt_lifecycle(fs_uuid, event)
            return
        if not session:
            return
        event = self.accumulate_stt_transcript(fs_uuid, event)
        await self.publish_stt_activity(fs_uuid, event)
        if session.mode == SessionMode.TRANSLATION:
            await self.handle_translation_transcript(session, fs_uuid, event)
            return

        await self.handle_assistant_transcript(session, fs_uuid, event)

    def stt_event_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        text = str(event.get("text") or "")
        event_type = str(event.get("type") or "")
        is_final = bool(event.get("is_final")) or event_type == "final"
        payload = {
            "text": text,
            "is_final": is_final,
            "confidence": event.get("confidence"),
            "language": event.get("language"),
            "stt_type": event_type,
        }
        for key in STT_EVENT_METADATA_KEYS:
            if key in event:
                payload[key] = event.get(key)
        return payload

    async def publish_stt_lifecycle(self, fs_uuid: str, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        payload = self.stt_event_payload(event)
        item_id = stt_item_id(event)
        if item_id:
            self.stt_lifecycle_items.setdefault(fs_uuid, set()).add(item_id)
        if event_type == "speech_started":
            await self.publish_voice_event(
                fs_uuid, source="stt", type="stt.speech_started", payload=payload
            )
            await self.mark_user_speech_started(fs_uuid, payload)
            return
        if event_type == "speech_stopped":
            await self.publish_voice_event(
                fs_uuid, source="stt", type="stt.speech_stopped", payload=payload
            )
            await self.mark_user_speech_stopped(fs_uuid, payload)
            return
        if event_type == "endpoint":
            self.stt_partial_transcripts.pop(fs_uuid, None)
            self.note_stt_endpoint(fs_uuid)
            await self.publish_voice_event(
                fs_uuid, source="stt", type="stt.endpoint", payload=payload
            )
            await self.mark_user_speech_stopped(fs_uuid, payload)
            return

    def accumulate_stt_transcript(
        self, fs_uuid: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        event_type = str(event.get("type") or "")
        text = str(event.get("text") or "")
        is_final = bool(event.get("is_final")) or event_type == "final"
        if event_type == "endpoint":
            self.stt_partial_transcripts.pop(fs_uuid, None)
            return event
        if not text:
            return event
        if is_final:
            self.stt_partial_transcripts.pop(fs_uuid, None)
            return event
        previous = self.stt_partial_transcripts.get(fs_uuid, "")
        accumulated = merge_stt_partial(previous, text)
        self.stt_partial_transcripts[fs_uuid] = accumulated
        if accumulated == text:
            return event
        updated = dict(event)
        updated["text"] = accumulated
        updated["raw_text"] = text
        return updated

    async def publish_stt_activity(self, fs_uuid: str, event: dict[str, Any]) -> None:
        text = str(event.get("text") or "")
        is_final = bool(event.get("is_final")) or event.get("type") == "final"
        event_type = str(event.get("type") or "")
        payload = self.stt_event_payload(event)
        has_lifecycle = self.stt_event_has_lifecycle(fs_uuid, event)
        if (
            text or event_type in {"partial", "final", "endpoint"}
        ) and not has_lifecycle:
            await self.mark_user_speech_started(fs_uuid, payload)
        if event_type == "endpoint":
            self.note_stt_endpoint(fs_uuid)
            await self.publish_voice_event(
                fs_uuid, source="stt", type="stt.endpoint", payload=payload
            )
            if not has_lifecycle or fs_uuid in self.active_user_speech:
                await self.mark_user_speech_stopped(fs_uuid, payload)
            return
        if is_final:
            self.note_stt_final(fs_uuid)
            await self.publish_voice_event(
                fs_uuid, source="stt", type="stt.final", payload=payload
            )
            if not has_lifecycle or fs_uuid in self.active_user_speech:
                await self.mark_user_speech_stopped(fs_uuid, payload)
            return
        if not text and not event_type:
            return
        self.note_stt_first_partial(fs_uuid)
        await self.publish_voice_event(
            fs_uuid, source="stt", type="stt.partial", payload=payload
        )

    def stt_event_has_lifecycle(self, fs_uuid: str, event: dict[str, Any]) -> bool:
        item_id = stt_item_id(event)
        return bool(item_id and item_id in self.stt_lifecycle_items.get(fs_uuid, set()))

    async def handle_assistant_transcript(
        self,
        session: CallSession,
        fs_uuid: str,
        event: dict[str, Any],
    ) -> None:
        if self.should_cancel_pending_delivery_resume_for_event(event):
            self.cancel_pending_delivery_resume(fs_uuid)
        if bool(event.get("is_final")) or event.get("type") == "final":
            self.note_stt_final(fs_uuid)
        policy_result = await self.evaluate_assistant_policy_for_event(
            session, fs_uuid, event
        )
        policy_input = policy_result.policy_input
        semantic_frame = policy_result.semantic_frame
        policy_decision = policy_result.policy_decision
        self.note_policy_evaluation(fs_uuid, policy_result)

        if policy_input.is_partial and str(event.get("text") or ""):
            self.cancel_pending_turn_ack(fs_uuid)

        if not policy_input.is_partial:
            resolved = await self.resolve_pending_assistant_turn(
                session,
                fs_uuid,
                event,
                policy_result,
            )
            if resolved is None:
                return
            policy_result = resolved
            policy_input = policy_result.policy_input
            semantic_frame = policy_result.semantic_frame
            policy_decision = policy_result.policy_decision
            self.note_policy_evaluation(fs_uuid, policy_result)

        if semantic_frame is not None:
            await self.publish_semantic_frame(fs_uuid, policy_input, semantic_frame)
        await self.publish_policy_decision(
            fs_uuid, policy_input, policy_decision, policy_result=policy_result
        )
        await self.publish_blocked_actions(fs_uuid, policy_input, policy_decision)

        if not policy_input.is_partial:
            logger.info(
                "assistant transcript final fs_uuid=%s action=%s",
                fs_uuid,
                policy_decision.decision.value,
            )

        self.record_soft_interjection_suppression(
            fs_uuid, policy_input, policy_decision
        )

        if self.should_hold_candidate_final(
            policy_input, semantic_frame, policy_decision
        ):
            await self.start_pending_assistant_turn(
                fs_uuid, policy_input, semantic_frame, policy_decision
            )
            return

        if policy_decision.decision in {PolicyAction.WAIT, PolicyAction.SUPPRESS}:
            return

        if policy_decision.decision == PolicyAction.SOFT_INTERRUPT_CHECKIN:
            await self.cancel_active_speech_for_user_turn(
                fs_uuid, policy_input, policy_decision
            )
            await self.speak_soft_interjection_checkin(
                session, fs_uuid, policy_input.turn_id
            )
            return

        if policy_decision.decision == PolicyAction.CANCEL_TTS_AND_LISTEN:
            await self.cancel_active_speech_for_user_turn(
                fs_uuid, policy_input, policy_decision
            )
            return

        await self.cancel_active_speech_for_user_turn(
            fs_uuid, policy_input, policy_decision
        )

        if policy_decision.decision == PolicyAction.END_CALL:
            session.history.append({"role": "user", "content": policy_input.transcript})
            await self.speak(
                fs_uuid,
                "Goodbye.",
                language="en",
                wait_complete=True,
                reason="goodbye",
                history_session=session,
            )
            fs_session = self.esl_sessions.get(fs_uuid)
            if fs_session:
                await fs_session.hangup("NORMAL_CLEARING")
            return

        if policy_decision.decision == PolicyAction.CONFIRM_BEFORE_ACTION:
            await self.speak_policy_response(
                session,
                fs_uuid,
                policy_input.transcript,
                POLICY_CONFIRMATION_PROMPT,
                reason="policy_confirmation",
            )
            return

        if policy_decision.decision in {
            PolicyAction.REJECT_TOOL_EXECUTION,
            PolicyAction.ESCALATE,
        }:
            await self.speak_policy_response(
                session,
                fs_uuid,
                policy_input.transcript,
                POLICY_SAFE_FALLBACK_PROMPT,
                reason="policy_safe_fallback",
            )
            return

        if policy_decision.decision != PolicyAction.RESPOND:
            return

        await self.respond_to_assistant_turn(
            session, fs_uuid, policy_input, semantic_frame, policy_decision
        )

    async def respond_to_assistant_turn(
        self,
        session: CallSession,
        fs_uuid: str,
        policy_input: PolicyInput,
        semantic_frame: SemanticFrame | None,
        policy_decision: PolicyDecision,
    ) -> None:
        turn_id = next_turn_id(session)
        turn_started_at = time.perf_counter()
        user_text = policy_input.transcript
        history_for_llm = list(session.history)
        policy_metadata = metadata_for_policy(policy_decision, semantic_frame)
        delivery_context = self.consume_delivery_context(
            session, latest_user_text=user_text
        )
        if delivery_context:
            policy_metadata[PREVIOUS_ASSISTANT_DELIVERY_METADATA_KEY] = delivery_context
        user_text_stats = describe_text_for_voice(user_text)
        await self.publish_voice_event(
            fs_uuid,
            source="system",
            type="turn.started",
            payload={
                "turn_id": turn_id,
                "mode": session.mode.value,
                "text": user_text,
                "history_size": len(history_for_llm),
                **user_text_stats,
            },
        )
        session.state = SessionState.THINKING
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="agent.thinking_started",
            payload={"turn_id": turn_id, "text": user_text, **user_text_stats},
        )
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="llm.request_started",
            payload={
                "turn_id": turn_id,
                "text": user_text,
                "history_size": len(history_for_llm),
                "policy_action": policy_decision.decision.value,
                "blocked_actions": list(policy_decision.blocked_actions),
                "has_previous_assistant_delivery": delivery_context is not None,
                **user_text_stats,
            },
        )
        if delivery_context:
            await self.publish_voice_event(
                fs_uuid,
                source="system",
                type="delivery.context_sent_to_llm",
                payload=delivery_context_event_payload(delivery_context),
            )
        session.history.append({"role": "user", "content": user_text})
        current_task = asyncio.current_task()
        if current_task is not None:
            previous = self.active_llm_tasks.get(fs_uuid)
            if previous and previous is not current_task and not previous.done():
                previous.cancel()
            self.active_llm_tasks[fs_uuid] = current_task
        try:
            await self.stream_assistant_response(
                session,
                fs_uuid,
                turn_id=turn_id,
                turn_started_at=turn_started_at,
                llm_started_at=time.perf_counter(),
                user_text=user_text,
                history_for_llm=history_for_llm,
                metadata=policy_metadata,
            )
        finally:
            if (
                current_task is not None
                and self.active_llm_tasks.get(fs_uuid) is current_task
            ):
                self.active_llm_tasks.pop(fs_uuid, None)
        if not self.is_agent_speaking(fs_uuid):
            session.state = SessionState.LISTENING

    async def stream_assistant_response(
        self,
        session: CallSession,
        fs_uuid: str,
        *,
        turn_id: str,
        turn_started_at: float,
        llm_started_at: float,
        user_text: str,
        history_for_llm: list[dict[str, str]],
        metadata: dict[str, Any],
    ) -> None:
        chunker = SentenceChunker(fallback_words=progressive_chunk_fallback_words())
        reply_parts: list[str] = []
        completed_text = ""
        spoken_text = ""
        chunk_index = 0
        first_llm_delta_ms: float | None = None
        first_tts_enqueue_ms: float | None = None
        first_estimated_audio_ms: float | None = None
        first_tts_timing: dict[str, Any] | None = None
        last_tts_timing: dict[str, Any] | None = None
        first_tool_call_ms: float | None = None
        tool_wait_ms = 0.0
        tool_call_count = 0
        tool_names: list[str] = []
        llm_model = ""
        llm_provider = ""
        first_llm_delta_at: float | None = None
        first_tts_enqueue_at: float | None = None
        llm_upstream_finished_ms: float | None = None

        async def publish_llm_upstream_finished(text: str) -> None:
            nonlocal llm_upstream_finished_ms
            if llm_upstream_finished_ms is not None:
                return
            llm_upstream_finished_ms = elapsed_ms(llm_started_at)
            text_stats = describe_text_for_voice(text)
            await self.publish_voice_event(
                fs_uuid,
                source="llm",
                type="llm.upstream_finished",
                payload={
                    "turn_id": turn_id,
                    "latency_ms": llm_upstream_finished_ms,
                    "provider": llm_provider,
                    "model": llm_model,
                    "timing_scope": "gateway_observed_upstream",
                    **text_stats,
                },
            )

        async def speak_chunk(chunk: str, *, is_final_chunk: bool) -> None:
            nonlocal \
                chunk_index, \
                spoken_text, \
                first_tts_enqueue_ms, \
                first_tts_enqueue_at
            nonlocal first_estimated_audio_ms, first_tts_timing, last_tts_timing
            clean_chunk = " ".join(str(chunk or "").split())
            if not clean_chunk:
                return
            chunk_index += 1
            spoken_text = append_spoken_text(spoken_text, clean_chunk)
            generated_so_far = spoken_text or completed_text or "".join(reply_parts)
            timing = await self.speak(
                fs_uuid,
                clean_chunk,
                wait_complete=True,
                reason="assistant_response",
                turn_id=turn_id,
                history_session=session,
                event_payload={
                    "progressive": True,
                    "chunk_index": chunk_index,
                    "is_final_chunk": is_final_chunk,
                },
                tracking_text=spoken_text,
                generated_text=generated_so_far,
                replace_active_speech=chunk_index == 1,
            )
            last_tts_timing = timing
            if first_tts_enqueue_ms is None:
                first_tts_timing = timing
                first_tts_enqueue_at = time.perf_counter()
                first_tts_enqueue_ms = elapsed_ms(turn_started_at)
                first_estimated_audio_ms = round(
                    first_tts_enqueue_ms + timing["estimated_start_delay_ms"], 1
                )

        try:
            stream_method = getattr(self.llm_client, "stream_respond", None)
            if not stream_method:
                await self.ensure_provider_available("llm", fs_uuid)
                try:
                    llm_result = await self.llm_client.respond(
                        fs_uuid,
                        user_text,
                        history_for_llm,
                        metadata=metadata,
                    )
                except Exception as exc:
                    if not isinstance(exc, ProviderCircuitOpenError):
                        await self.record_provider_failure("llm", fs_uuid, exc)
                    raise
                await self.record_provider_success("llm", fs_uuid)
                completed_text = getattr(llm_result, "text", str(llm_result))
                llm_model = getattr(llm_result, "model", "")
                llm_provider = getattr(llm_result, "provider", "")
                await publish_llm_upstream_finished(completed_text)
                for chunk in chunker.add_delta(completed_text):
                    await speak_chunk(chunk, is_final_chunk=False)
            else:
                await self.ensure_provider_available("llm", fs_uuid)
                llm_stream = stream_method(
                    fs_uuid,
                    user_text,
                    history_for_llm,
                    metadata=metadata,
                )
                llm_iterator = llm_stream.__aiter__()
                while True:
                    try:
                        stream_event = await llm_iterator.__anext__()
                    except StopAsyncIteration:
                        await self.record_provider_success("llm", fs_uuid)
                        break
                    except Exception as exc:
                        if not isinstance(exc, ProviderCircuitOpenError):
                            await self.record_provider_failure("llm", fs_uuid, exc)
                        raise
                    event_type = str(stream_event.get("type") or "")
                    if event_type == "started":
                        llm_model = str(stream_event.get("model") or llm_model)
                        llm_provider = str(stream_event.get("provider") or llm_provider)
                        continue
                    if event_type == "delta":
                        delta = str(stream_event.get("text") or "")
                        if not delta:
                            continue
                        if first_llm_delta_ms is None:
                            first_llm_delta_at = time.perf_counter()
                            first_llm_delta_ms = elapsed_ms(llm_started_at)
                        reply_parts.append(delta)
                        self.update_active_speech_generated_text(
                            fs_uuid, "".join(reply_parts)
                        )
                        await self.publish_voice_event(
                            fs_uuid,
                            source="llm",
                            type="llm.partial_text",
                            payload={
                                "turn_id": turn_id,
                                "text": delta,
                                "accumulated_text": "".join(reply_parts),
                            },
                        )
                        for chunk in chunker.add_delta(delta):
                            await speak_chunk(chunk, is_final_chunk=False)
                        continue
                    if event_type in {
                        "tool_call_started",
                        "tool_call_progress",
                        "tool_call_completed",
                    }:
                        tool_name = str(stream_event.get("tool_name") or "tool")
                        if event_type == "tool_call_started":
                            tool_call_count += 1
                            if first_tool_call_ms is None:
                                first_tool_call_ms = elapsed_ms(llm_started_at)
                            if tool_name not in tool_names:
                                tool_names.append(tool_name)
                        if event_type == "tool_call_completed":
                            tool_wait_ms += safe_float(
                                stream_event.get("latency_ms"), 0.0
                            )
                        voice_event_type = {
                            "tool_call_started": "tool.call_started",
                            "tool_call_progress": "tool.call_progress",
                            "tool_call_completed": "tool.call_completed",
                        }[event_type]
                        payload = {
                            key: value
                            for key, value in stream_event.items()
                            if key not in {"type", "session_id"}
                        }
                        payload["turn_id"] = turn_id
                        await self.publish_voice_event(
                            fs_uuid,
                            source="tool",
                            type=voice_event_type,
                            payload=payload,
                        )
                        speech_text = str(stream_event.get("speech_text") or "").strip()
                        if event_type == "tool_call_started" and speech_text:
                            await speak_chunk(speech_text, is_final_chunk=False)
                        continue
                    if event_type == "completed":
                        completed_text = str(stream_event.get("text") or "")
                        llm_model = str(stream_event.get("model") or llm_model)
                        llm_provider = str(stream_event.get("provider") or llm_provider)
                        await publish_llm_upstream_finished(
                            completed_text or "".join(reply_parts)
                        )
                        if completed_text:
                            self.update_active_speech_generated_text(
                                fs_uuid, completed_text
                            )
                        if completed_text and not reply_parts:
                            reply_parts.append(completed_text)
                            for chunk in chunker.add_delta(completed_text):
                                await speak_chunk(chunk, is_final_chunk=False)
                        continue
                    if event_type == "error":
                        exc = RuntimeError("LLM stream failed")
                        await self.record_provider_failure("llm", fs_uuid, exc)
                        raise exc

            if llm_upstream_finished_ms is None:
                await publish_llm_upstream_finished(
                    completed_text or "".join(reply_parts)
                )
            for chunk in chunker.finish():
                await speak_chunk(chunk, is_final_chunk=True)
            reply = completed_text or "".join(reply_parts)
            if not spoken_text and reply:
                await speak_chunk(reply, is_final_chunk=True)
                spoken_text = reply
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.publish_voice_event(
                fs_uuid,
                source="llm",
                type="llm.error",
                payload={
                    "turn_id": turn_id,
                    "error": {
                        "type": "llm_upstream_error",
                        "message": "LLM response failed",
                    },
                },
            )
            raise

        reply = completed_text or "".join(reply_parts) or spoken_text
        llm_latency_ms = elapsed_ms(llm_started_at)
        response_text_stats = describe_text_for_voice(reply)
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="llm.request_finished",
            payload={
                "turn_id": turn_id,
                "latency_ms": llm_latency_ms,
                "first_llm_delta_ms": first_llm_delta_ms,
                "provider": llm_provider,
                "model": llm_model,
                **response_text_stats,
            },
        )
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="llm.final_text",
            payload={
                "turn_id": turn_id,
                "text": reply,
                "provider": llm_provider,
                "model": llm_model,
                **response_text_stats,
            },
        )
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="agent.thinking_finished",
            payload={"turn_id": turn_id, "text": reply, **response_text_stats},
        )
        latency_payload: dict[str, Any] = {
            "turn_id": turn_id,
            "llm_request_ms": llm_latency_ms,
            "llm_upstream_ms": llm_upstream_finished_ms,
            "first_llm_delta_ms": first_llm_delta_ms,
            "first_tts_enqueue_ms": first_tts_enqueue_ms,
            "first_estimated_audio_ms": first_estimated_audio_ms,
            "response_chars": response_text_stats["text_chars"],
            "response_words": response_text_stats["text_words"],
            "progressive": True,
            "tts_chunks": chunk_index,
            "tool_call_count": tool_call_count,
            "tool_names": tool_names,
        }
        latency_payload.update(
            self.stt_policy_latency_payload(fs_uuid, llm_started_at=llm_started_at)
        )
        add_elapsed(
            latency_payload,
            "first_delta_to_first_tts_enqueue_ms",
            first_llm_delta_at,
            first_tts_enqueue_at,
        )
        if first_tool_call_ms is not None:
            latency_payload["first_tool_call_ms"] = first_tool_call_ms
        if tool_wait_ms:
            latency_payload["tool_wait_ms"] = round(tool_wait_ms, 1)
        if first_tts_timing:
            final_to_tts_enqueued_ms = None
            state = self.turn_latency.get(fs_uuid)
            if state and first_tts_enqueue_at is not None:
                final_to_tts_enqueued_ms = elapsed_between_ms(
                    state.final_at, first_tts_enqueue_at
                )
            if final_to_tts_enqueued_ms is None:
                final_to_tts_enqueued_ms = first_tts_enqueue_ms
            final_to_estimated_audio_ms = (
                round(
                    final_to_tts_enqueued_ms
                    + first_tts_timing["estimated_start_delay_ms"],
                    1,
                )
                if final_to_tts_enqueued_ms is not None
                else first_estimated_audio_ms
            )
            latency_payload.update(
                {
                    "tts_enqueue_ms": first_tts_timing["enqueue_latency_ms"],
                    "estimated_start_delay_ms": first_tts_timing[
                        "estimated_start_delay_ms"
                    ],
                    "final_to_tts_enqueued_ms": final_to_tts_enqueued_ms,
                    "final_to_estimated_audio_ms": final_to_estimated_audio_ms,
                }
            )
        if last_tts_timing:
            latency_payload["estimated_playback_ms"] = last_tts_timing[
                "estimated_playback_ms"
            ]
        await self.publish_voice_event(
            fs_uuid,
            source="system",
            type="turn.latency",
            payload=latency_payload,
        )

    async def evaluate_assistant_policy_for_event(
        self,
        session: CallSession,
        fs_uuid: str,
        event: dict[str, Any],
    ) -> PolicyEvaluationResult:
        policy_input = self.policy_input_for_assistant_event(session, fs_uuid, event)
        await self.publish_policy_evaluation_started(fs_uuid, policy_input)
        result = self.assistant_policy_result_for_event(session, fs_uuid, event)
        await self.publish_policy_evaluation_finished(fs_uuid, result)
        return result

    def assistant_policy_result_for_event(
        self,
        session: CallSession,
        fs_uuid: str,
        event: dict[str, Any],
    ) -> PolicyEvaluationResult:
        evaluation_started_at = time.perf_counter()
        policy_input = self.policy_input_for_assistant_event(session, fs_uuid, event)
        semantic_frame: SemanticFrame | None = None
        semantic_ms: float | None = None
        if not policy_input.is_partial:
            semantic_started_at = time.perf_counter()
            semantic_frame = self.semantic_interpreter.interpret(policy_input)
            semantic_ms = elapsed_ms(semantic_started_at)
        decision_started_at = time.perf_counter()
        policy_decision = evaluate_policy(policy_input, semantic_frame)
        return PolicyEvaluationResult(
            policy_input=policy_input,
            semantic_frame=semantic_frame,
            policy_decision=policy_decision,
            semantic_ms=semantic_ms,
            policy_decision_ms=elapsed_ms(decision_started_at),
            policy_evaluation_ms=elapsed_ms(evaluation_started_at),
        )

    async def resolve_pending_assistant_turn(
        self,
        session: CallSession,
        fs_uuid: str,
        event: dict[str, Any],
        policy_result: PolicyEvaluationResult,
    ) -> PolicyEvaluationResult | None:
        policy_input = policy_result.policy_input
        semantic_frame = policy_result.semantic_frame
        pending = self.pending_assistant_turns.get(fs_uuid)
        if not pending:
            return policy_result
        if semantic_frame is None:
            return policy_result

        if semantic_frame.goodbye_detected and not semantic_frame.continue_conversation:
            await self.cancel_pending_assistant_turn(
                fs_uuid, status="cancelled", reason="goodbye"
            )
            return policy_result

        if semantic_frame.speech_act == SpeechAct.BACKCHANNEL:
            self.cancel_pending_turn_ack(fs_uuid)
            return None

        if (
            not semantic_frame.addressed_to_agent
            or semantic_frame.speech_act == SpeechAct.SIDE_TALK
        ):
            return policy_result

        if (
            pending.clarification_spoken
            and is_complete_standalone_question_restatement(
                policy_input.transcript, semantic_frame
            )
        ):
            await self.cancel_pending_assistant_turn(
                fs_uuid,
                status="cancelled",
                reason="standalone_restatement_after_clarification",
            )
            return policy_result

        merged_text = merge_pending_turn_text(pending.text, policy_input.transcript)
        merged_event = dict(event)
        merged_event.update(
            {
                "text": merged_text,
                "type": "final",
                "is_final": True,
                "turn_id": pending.turn_id,
            }
        )
        await self.cancel_pending_assistant_turn(
            fs_uuid,
            status="merged",
            reason="continuation",
            merged_text=merged_text,
        )
        return await self.evaluate_assistant_policy_for_event(
            session, fs_uuid, merged_event
        )

    def should_hold_candidate_final(
        self,
        policy_input: PolicyInput,
        semantic_frame: SemanticFrame | None,
        policy_decision: PolicyDecision,
    ) -> bool:
        return (
            not policy_input.is_partial
            and semantic_frame is not None
            and not semantic_frame.utterance_complete
            and policy_decision.decision == PolicyAction.WAIT
        )

    async def start_pending_assistant_turn(
        self,
        fs_uuid: str,
        policy_input: PolicyInput,
        semantic_frame: SemanticFrame,
        policy_decision: PolicyDecision,
    ) -> None:
        await self.cancel_pending_assistant_turn(
            fs_uuid, status="cancelled", reason="replaced"
        )
        ack_ms = turn_hold_ack_ms()
        clarify_ms = (
            turn_hold_clarify_ms() if semantic_frame.clarification_needed else None
        )
        ttl_seconds = turn_hold_ttl_seconds()
        now = time.perf_counter()
        pending = PendingAssistantTurn(
            text=policy_input.transcript,
            turn_id=policy_input.turn_id,
            policy_input=policy_input,
            semantic_frame=semantic_frame,
            policy_decision=policy_decision,
            ack_ms=ack_ms,
            clarify_ms=clarify_ms,
            ttl_seconds=ttl_seconds,
            created_at=now,
            updated_at=now,
        )
        self.pending_assistant_turns[fs_uuid] = pending
        await self.publish_turn_hold_event(
            fs_uuid, pending, status="started", reason=policy_decision.reason
        )
        if self.should_break_active_speech_for_turn_hold(
            fs_uuid, policy_input, semantic_frame
        ):
            await self.break_speech(fs_uuid, reason="turn_hold", publish_events=True)

        if clarify_ms is not None:
            ack_task = asyncio.create_task(
                self.speak_turn_hold_clarification_after_delay(
                    fs_uuid, pending.turn_id, clarify_ms / 1000.0
                ),
                name=f"turn-hold-clarify-{fs_uuid}",
            )
        else:
            ack_task = asyncio.create_task(
                self.speak_turn_hold_filler_after_delay(
                    fs_uuid, pending.turn_id, ack_ms / 1000.0
                ),
                name=f"turn-hold-ack-{fs_uuid}",
            )
        expiry_task = asyncio.create_task(
            self.expire_pending_assistant_turn_after_delay(
                fs_uuid, pending.turn_id, ttl_seconds
            ),
            name=f"turn-hold-expiry-{fs_uuid}",
        )
        pending.ack_task = ack_task
        pending.expiry_task = expiry_task
        self.tasks.update({ack_task, expiry_task})
        ack_task.add_done_callback(self.tasks.discard)
        expiry_task.add_done_callback(self.tasks.discard)

    def should_break_active_speech_for_turn_hold(
        self,
        fs_uuid: str,
        policy_input: PolicyInput,
        semantic_frame: SemanticFrame,
    ) -> bool:
        has_active_tts = policy_input.agent_is_speaking or fs_uuid in self.active_speech
        return (
            has_active_tts
            and policy_input.tts_allow_interruptions
            and semantic_frame.addressed_to_agent
            and semantic_frame.speech_act
            not in {SpeechAct.BACKCHANNEL, SpeechAct.SIDE_TALK}
        )

    def cancel_pending_turn_ack(self, fs_uuid: str) -> None:
        pending = self.pending_assistant_turns.get(fs_uuid)
        if pending and pending.ack_task and not pending.ack_task.done():
            pending.ack_task.cancel()
            pending.ack_task = None

    async def cancel_pending_assistant_turn(
        self,
        fs_uuid: str,
        *,
        status: str,
        reason: str,
        merged_text: str | None = None,
    ) -> PendingAssistantTurn | None:
        pending = self.pending_assistant_turns.pop(fs_uuid, None)
        if not pending:
            return None
        self.cancel_pending_tasks(pending)
        await self.publish_turn_hold_event(
            fs_uuid,
            pending,
            status=status,
            reason=reason,
            merged_text=merged_text,
        )
        return pending

    def cancel_pending_tasks(self, pending: PendingAssistantTurn) -> None:
        current = asyncio.current_task()
        for task in (pending.ack_task, pending.expiry_task):
            if task and task is not current and not task.done():
                task.cancel()

    async def speak_turn_hold_filler_after_delay(
        self, fs_uuid: str, turn_id: str, delay_seconds: float
    ) -> None:
        try:
            await asyncio.sleep(max(0.0, delay_seconds))
        except asyncio.CancelledError:
            return
        pending = self.pending_assistant_turns.get(fs_uuid)
        if not pending or pending.turn_id != turn_id or pending.filler_spoken:
            return
        pending.filler_spoken = True
        pending.updated_at = time.perf_counter()
        await self.publish_turn_hold_event(
            fs_uuid, pending, status="filler_spoken", reason="ack_timeout"
        )
        try:
            await self.speak(
                fs_uuid,
                turn_hold_filler_text(),
                interruptible=True,
                wait_complete=False,
                reason="turn_hold_filler",
                turn_id=turn_id,
            )
        except Exception:
            logger.warning(
                "turn hold filler failed fs_uuid=%s turn_id=%s",
                fs_uuid,
                turn_id,
                exc_info=True,
            )

    async def speak_turn_hold_clarification_after_delay(
        self, fs_uuid: str, turn_id: str, delay_seconds: float
    ) -> None:
        try:
            await asyncio.sleep(max(0.0, delay_seconds))
        except asyncio.CancelledError:
            return
        pending = self.pending_assistant_turns.get(fs_uuid)
        if (
            not pending
            or pending.turn_id != turn_id
            or pending.clarification_spoken
        ):
            return
        policy_input = pending.policy_input.model_copy(
            update={
                "clarification_due": True,
                "turn_hold_elapsed_ms": elapsed_ms(pending.created_at),
            }
        )
        policy_decision = evaluate_policy(policy_input, pending.semantic_frame)
        if policy_decision.decision != PolicyAction.CLARIFY:
            return
        pending.clarification_spoken = True
        pending.policy_decision = policy_decision
        pending.updated_at = time.perf_counter()
        await self.publish_policy_decision(fs_uuid, policy_input, policy_decision)
        await self.publish_turn_hold_event(
            fs_uuid, pending, status="clarification_spoken", reason="clarification_due"
        )
        try:
            await self.speak(
                fs_uuid,
                turn_hold_clarification_text(pending.semantic_frame),
                interruptible=True,
                wait_complete=False,
                reason="turn_hold_clarification",
                turn_id=turn_id,
            )
        except Exception:
            logger.warning(
                "turn hold clarification failed fs_uuid=%s turn_id=%s",
                fs_uuid,
                turn_id,
                exc_info=True,
            )

    async def expire_pending_assistant_turn_after_delay(
        self, fs_uuid: str, turn_id: str, delay_seconds: float
    ) -> None:
        try:
            await asyncio.sleep(max(0.0, delay_seconds))
        except asyncio.CancelledError:
            return
        pending = self.pending_assistant_turns.get(fs_uuid)
        if not pending or pending.turn_id != turn_id:
            return
        await self.cancel_pending_assistant_turn(
            fs_uuid, status="expired", reason="ttl"
        )

    async def publish_turn_hold_event(
        self,
        fs_uuid: str,
        pending: PendingAssistantTurn,
        *,
        status: str,
        reason: str,
        merged_text: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": status,
            "text": pending.text,
            "buffered_text": pending.text,
            "turn_id": pending.turn_id,
            "reason": reason,
            "ack_ms": pending.ack_ms,
            "clarify_ms": pending.clarify_ms,
            "ttl_seconds": pending.ttl_seconds,
            "clarification_needed": pending.semantic_frame.clarification_needed,
            "clarification_type": pending.semantic_frame.clarification_type,
            "clarification_spoken": pending.clarification_spoken,
            "filler_spoken": pending.filler_spoken,
            "held_for_ms": elapsed_ms(pending.created_at),
        }
        if merged_text is not None:
            payload["merged_text"] = merged_text
        await self.publish_voice_event(
            fs_uuid, source="policy", type="policy.turn_hold", payload=payload
        )

    def policy_input_for_assistant_event(
        self,
        session: CallSession,
        fs_uuid: str,
        event: dict[str, Any],
    ) -> PolicyInput:
        is_final = bool(event.get("is_final")) or event.get("type") == "final"
        metadata = session.metadata
        known_slots = metadata.get("known_slots")
        return PolicyInput(
            session_id=session.session_id,
            turn_id=str(
                event.get("turn_id")
                or f"{session.session_id}:policy:{int(metadata.get('turn_number') or 0) + 1}"
            ),
            transcript=str(event.get("text") or ""),
            is_partial=not is_final,
            stt_confidence=safe_float(event.get("confidence"), 0.8),
            stt_provider=str(event.get("provider") or "") or None,
            stt_type=str(event.get("type") or "") or None,
            stt_fallback=bool(event.get("fallback")),
            stt_fallback_reason=str(event.get("fallback_reason") or "") or None,
            current_flow=str(metadata.get("current_flow") or session.mode.value),
            last_agent_message=last_assistant_message(session),
            agent_is_speaking=self.is_agent_speaking(fs_uuid),
            tts_allow_interruptions=self.tts_allows_interruptions(fs_uuid),
            prior_soft_interjections_during_tts=(
                self.soft_interjections_during_tts.get(fs_uuid, 0)
                if fs_uuid in self.active_speech
                else 0
            ),
            previous_assistant_delivery_pending=isinstance(
                metadata.get(PREVIOUS_ASSISTANT_DELIVERY_METADATA_KEY), dict
            ),
            delivery_resume_pending=fs_uuid in self.pending_delivery_resume_tasks,
            pending_action=metadata.get("pending_action"),
            pending_action_risk=metadata.get("pending_action_risk") or "none",
            known_slots=known_slots if isinstance(known_slots, dict) else {},
        )

    def tts_allows_interruptions(self, fs_uuid: str) -> bool:
        active = self.active_speech.get(fs_uuid)
        if not active:
            return True
        return bool(active.payload.get("interruptible", True))

    def should_cancel_active_speech_for_decision(
        self,
        policy_input: PolicyInput,
        policy_decision: PolicyDecision,
        fs_uuid: str | None = None,
    ) -> bool:
        has_active_tts = policy_input.agent_is_speaking or (
            bool(fs_uuid) and fs_uuid in self.active_speech
        )
        if not has_active_tts or not policy_input.tts_allow_interruptions:
            return False
        if policy_decision.decision == PolicyAction.CANCEL_TTS_AND_LISTEN:
            return True
        if (
            policy_decision.decision == PolicyAction.SOFT_INTERRUPT_CHECKIN
            and "pause_control" in policy_decision.flags
        ):
            return True
        return (
            not policy_input.is_partial
            and policy_decision.decision in ACTIVE_TTS_REPLACING_POLICY_ACTIONS
        )

    def record_soft_interjection_suppression(
        self,
        fs_uuid: str,
        policy_input: PolicyInput,
        policy_decision: PolicyDecision,
    ) -> None:
        if (
            policy_input.is_partial
            or fs_uuid not in self.active_speech
            or policy_decision.decision != PolicyAction.SUPPRESS
            or "soft_interjection_suppressed" not in policy_decision.flags
        ):
            return
        self.soft_interjections_during_tts[fs_uuid] = (
            self.soft_interjections_during_tts.get(fs_uuid, 0) + 1
        )

    async def cancel_active_speech_for_user_turn(
        self,
        fs_uuid: str,
        policy_input: PolicyInput,
        policy_decision: PolicyDecision,
    ) -> bool:
        if not self.should_cancel_active_speech_for_decision(
            policy_input, policy_decision, fs_uuid
        ):
            return False
        await self.publish_voice_event(
            fs_uuid,
            source="policy",
            type="user.barge_in_detected",
            payload={
                "text": policy_input.transcript,
                "action": policy_decision.decision.value,
                "reason": policy_decision.reason,
                "is_final": not policy_input.is_partial,
                "turn_id": policy_input.turn_id,
            },
        )
        self.cancel_active_llm_stream(fs_uuid)
        await self.break_speech(
            fs_uuid,
            reason="barge_in_or_new_turn",
            latest_user_text=policy_input.transcript,
            publish_events=True,
        )
        return True

    async def speak_soft_interjection_checkin(
        self, session: CallSession, fs_uuid: str, turn_id: str
    ) -> None:
        session.state = SessionState.SPEAKING
        await self.speak(
            fs_uuid,
            SOFT_INTERJECTION_CHECKIN_PROMPT,
            wait_complete=True,
            reason="soft_interjection_checkin",
            turn_id=turn_id,
            history_session=session,
        )
        self.schedule_pending_delivery_resume(session, fs_uuid, turn_id)
        if not self.is_agent_speaking(fs_uuid):
            session.state = SessionState.LISTENING

    def policy_input_event_payload(self, policy_input: PolicyInput) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": policy_input.transcript,
            "turn_id": policy_input.turn_id,
            "is_final": not policy_input.is_partial,
            "agent_is_speaking": policy_input.agent_is_speaking,
            "tts_allow_interruptions": policy_input.tts_allow_interruptions,
            **describe_text_for_voice(policy_input.transcript),
        }
        if policy_input.stt_provider:
            payload["stt_provider"] = policy_input.stt_provider
        if policy_input.stt_type:
            payload["stt_type"] = policy_input.stt_type
        if policy_input.stt_fallback:
            payload["stt_fallback"] = True
        if policy_input.stt_fallback_reason:
            payload["stt_fallback_reason"] = policy_input.stt_fallback_reason
        return payload

    async def publish_policy_evaluation_started(
        self, fs_uuid: str, policy_input: PolicyInput
    ) -> None:
        await self.publish_voice_event(
            fs_uuid,
            source="policy",
            type="policy.evaluation_started",
            payload=self.policy_input_event_payload(policy_input),
        )

    async def publish_policy_evaluation_finished(
        self, fs_uuid: str, result: PolicyEvaluationResult
    ) -> None:
        payload = self.policy_input_event_payload(result.policy_input)
        payload.update(
            {
                "action": result.policy_decision.decision.value,
                "reason": result.policy_decision.reason,
                "flags": list(result.policy_decision.flags),
                "blocked_actions": list(result.policy_decision.blocked_actions),
                "semantic_ms": result.semantic_ms,
                "policy_decision_ms": result.policy_decision_ms,
                "policy_evaluation_ms": result.policy_evaluation_ms,
            }
        )
        await self.publish_voice_event(
            fs_uuid,
            source="policy",
            type="policy.evaluation_finished",
            payload=payload,
        )

    async def publish_semantic_frame(
        self,
        fs_uuid: str,
        policy_input: PolicyInput,
        semantic_frame: SemanticFrame,
    ) -> None:
        payload = semantic_frame.model_dump(mode="json")
        payload.update(
            {"text": policy_input.transcript, "turn_id": policy_input.turn_id}
        )
        await self.publish_voice_event(
            fs_uuid, source="policy", type="policy.semantic_frame", payload=payload
        )
        if semantic_frame.slots:
            session = self.find_session(fs_uuid)
            if session:
                known_slots = session.metadata.setdefault("known_slots", {})
                if isinstance(known_slots, dict):
                    known_slots.update(semantic_frame.slots)

    async def publish_policy_decision(
        self,
        fs_uuid: str,
        policy_input: PolicyInput,
        policy_decision: PolicyDecision,
        *,
        policy_result: PolicyEvaluationResult | None = None,
    ) -> None:
        self.note_policy_decision(fs_uuid)
        payload = policy_decision.model_dump(mode="json")
        payload.update(
            {
                "action": policy_decision.decision.value,
                "text": policy_input.transcript,
                "turn_id": policy_input.turn_id,
                "is_final": not policy_input.is_partial,
                "agent_is_speaking": policy_input.agent_is_speaking,
                "tts_allow_interruptions": policy_input.tts_allow_interruptions,
                "should_interrupt": self.should_cancel_active_speech_for_decision(
                    policy_input, policy_decision, fs_uuid
                ),
            }
        )
        if policy_result:
            if policy_result.semantic_ms is not None:
                payload["semantic_ms"] = policy_result.semantic_ms
            payload["policy_decision_ms"] = policy_result.policy_decision_ms
            payload["policy_evaluation_ms"] = policy_result.policy_evaluation_ms
        await self.publish_voice_event(
            fs_uuid, source="policy", type="policy.decision", payload=payload
        )

    async def publish_blocked_actions(
        self,
        fs_uuid: str,
        policy_input: PolicyInput,
        policy_decision: PolicyDecision,
    ) -> None:
        if not policy_decision.blocked_actions:
            return
        await self.publish_voice_event(
            fs_uuid,
            source="policy",
            type="policy.blocked_action",
            payload={
                "action": policy_decision.blocked_actions[0],
                "actions": list(policy_decision.blocked_actions),
                "text": policy_input.transcript,
                "decision": policy_decision.decision.value,
                "requires_confirmation": policy_decision.requires_confirmation,
            },
        )

    async def speak_policy_response(
        self,
        session: CallSession,
        fs_uuid: str,
        user_text: str,
        response_text: str,
        *,
        reason: str,
    ) -> None:
        session.history.append({"role": "user", "content": user_text})
        session.state = SessionState.SPEAKING
        await self.speak(
            fs_uuid,
            response_text,
            wait_complete=True,
            reason=reason,
            history_session=session,
        )
        if not self.is_agent_speaking(fs_uuid):
            session.state = SessionState.LISTENING

    async def handle_translation_transcript(
        self,
        session: CallSession,
        fs_uuid: str,
        event: dict[str, Any],
    ) -> None:
        leg = session.leg_by_uuid(fs_uuid)
        if not leg:
            return
        peer = session.peer_for(leg)
        if not peer:
            return
        is_final = bool(event.get("is_final")) or event.get("type") == "final"
        if is_final:
            self.note_stt_final(fs_uuid)
        decision_started_at = time.perf_counter()
        peer_is_speaking = self.is_agent_speaking(peer.fs_uuid)
        decision = self.detector.decide(
            str(event.get("text") or ""),
            is_final=is_final,
            confidence=safe_float(event.get("confidence"), 1.0),
            agent_is_speaking=peer_is_speaking,
        )
        policy_decision_ms = elapsed_ms(decision_started_at)
        await self.publish_voice_event(
            fs_uuid,
            source="policy",
            type="policy.decision",
            payload={
                "action": decision.action.value,
                "text": decision.text,
                "should_interrupt": decision.should_interrupt,
                "is_final": is_final,
                "target_leg_is_speaking": peer_is_speaking,
                "leg_id": leg.leg_id,
                "target_leg_id": peer.leg_id,
                "policy_decision_ms": policy_decision_ms,
            },
        )
        if is_final:
            logger.info(
                "translation transcript final fs_uuid=%s leg_id=%s action=%s",
                fs_uuid,
                leg.leg_id,
                decision.action.value,
            )
        if decision.should_interrupt:
            publish_interruption = decision.action != TurnAction.GOODBYE
            if publish_interruption:
                await self.publish_voice_event(
                    peer.fs_uuid,
                    source="policy",
                    type="user.barge_in_detected",
                    payload={
                        "text": decision.text,
                        "source_leg_id": leg.leg_id,
                        "target_leg_id": peer.leg_id,
                    },
                )
            await self.break_speech(peer.fs_uuid, publish_events=publish_interruption)
        if decision.action == TurnAction.BACKCHANNEL:
            return
        if decision.action == TurnAction.GOODBYE:
            await self.speak(
                peer.fs_uuid,
                "The other participant ended the call.",
                language=peer.source_language,
                wait_complete=False,
                reason="translation_goodbye",
            )
            await self.hangup_leg(leg.fs_uuid)
            await self.hangup_leg(peer.fs_uuid)
            return
        if decision.action != TurnAction.USER_TURN:
            return

        turn_id = next_turn_id(session)
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="agent.thinking_started",
            payload={
                "turn_id": turn_id,
                "text": decision.text,
                "source_language": leg.source_language,
                "target_language": leg.target_language,
                "source_leg_id": leg.leg_id,
                "target_leg_id": peer.leg_id,
            },
        )
        translation_started_at = time.perf_counter()
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="llm.request_started",
            payload={
                "turn_id": turn_id,
                "text": decision.text,
                "source_language": leg.source_language,
                "target_language": leg.target_language,
                "source_leg_id": leg.leg_id,
                "target_leg_id": peer.leg_id,
            },
        )
        try:
            await self.ensure_provider_available("translation", fs_uuid)
            translation_result = await self.translation_client.translate(
                session.session_id,
                decision.text,
                source_language=leg.source_language,
                target_language=leg.target_language,
            )
            translated = translation_result_text(translation_result)
            translation_model = translation_result_field(translation_result, "model")
            translation_provider = translation_result_field(
                translation_result, "provider"
            )
            await self.record_provider_success("translation", fs_uuid)
        except Exception as exc:
            if not isinstance(exc, ProviderCircuitOpenError):
                await self.record_provider_failure("translation", fs_uuid, exc)
            await self.publish_voice_event(
                fs_uuid,
                source="llm",
                type="llm.error",
                payload={
                    "turn_id": turn_id,
                    "provider": "translation",
                    "source_language": leg.source_language,
                    "target_language": leg.target_language,
                    "error": {
                        "type": "translation_upstream_error",
                        "message": "Translation failed",
                    },
                },
            )
            raise
        translation_latency_ms = elapsed_ms(translation_started_at)
        response_text_stats = describe_text_for_voice(translated)
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="llm.upstream_finished",
            payload={
                "turn_id": turn_id,
                "latency_ms": translation_latency_ms,
                "provider": "translation",
                "model": translation_model,
                "llm_provider": translation_provider,
                "timing_scope": "gateway_observed_upstream",
                "source_language": leg.source_language,
                "target_language": leg.target_language,
                **response_text_stats,
            },
        )
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="llm.request_finished",
            payload={
                "turn_id": turn_id,
                "latency_ms": translation_latency_ms,
                "provider": "translation",
                "model": translation_model,
                "llm_provider": translation_provider,
                "source_language": leg.source_language,
                "target_language": leg.target_language,
                **response_text_stats,
            },
        )
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="llm.final_text",
            payload={
                "turn_id": turn_id,
                "text": translated,
                "source_text": decision.text,
                "source_language": leg.source_language,
                "target_language": leg.target_language,
                "model": translation_model,
                "llm_provider": translation_provider,
                **response_text_stats,
            },
        )
        await self.publish_voice_event(
            fs_uuid,
            source="llm",
            type="agent.thinking_finished",
            payload={"turn_id": turn_id, "text": translated, **response_text_stats},
        )
        session.history.append(
            {
                "role": leg.leg_id,
                "content": f"{leg.source_language}->{leg.target_language}: {decision.text} => {translated}",
            }
        )
        tts_started_at = time.perf_counter()
        tts_timing = await self.speak(
            peer.fs_uuid,
            translated,
            language=leg.target_language,
            wait_complete=False,
            reason="translation_response",
            turn_id=turn_id,
        )
        latency_payload: dict[str, Any] = {
            "turn_id": turn_id,
            "source_leg_id": leg.leg_id,
            "target_leg_id": peer.leg_id,
            "source_language": leg.source_language,
            "target_language": leg.target_language,
            "translation_model": translation_model,
            "translation_provider": translation_provider,
            "translation_request_ms": translation_latency_ms,
            "llm_request_ms": translation_latency_ms,
            "llm_upstream_ms": translation_latency_ms,
            "tts_enqueue_ms": tts_timing["enqueue_latency_ms"],
            "estimated_start_delay_ms": tts_timing["estimated_start_delay_ms"],
            "estimated_playback_ms": tts_timing["estimated_playback_ms"],
            "response_chars": response_text_stats["text_chars"],
            "response_words": response_text_stats["text_words"],
        }
        latency_payload.update(
            self.stt_policy_latency_payload(
                fs_uuid, llm_started_at=translation_started_at
            )
        )
        final_to_tts_enqueued_ms = None
        state = self.turn_latency.get(fs_uuid)
        if state:
            final_to_tts_enqueued_ms = elapsed_between_ms(
                state.final_at, time.perf_counter()
            )
        if final_to_tts_enqueued_ms is None:
            final_to_tts_enqueued_ms = elapsed_ms(tts_started_at)
        latency_payload["final_to_tts_enqueued_ms"] = final_to_tts_enqueued_ms
        latency_payload["final_to_estimated_audio_ms"] = round(
            final_to_tts_enqueued_ms + tts_timing["estimated_start_delay_ms"],
            1,
        )
        await self.publish_voice_event(
            fs_uuid,
            source="system",
            type="translation.latency",
            payload=latency_payload,
        )

    async def hangup_leg(self, fs_uuid: str) -> None:
        fs_session = self.esl_sessions.get(fs_uuid)
        if fs_session:
            await fs_session.hangup("NORMAL_CLEARING")
            return
        control = self.control_session_for_uuid(fs_uuid)
        if control:
            await control.send(f"api uuid_kill {fs_uuid} NORMAL_CLEARING")

    def find_session(self, fs_uuid: str) -> CallSession | None:
        """Return the owning call session for a leg UUID."""
        session_id = self.sessions_by_uuid.get(fs_uuid, fs_uuid)
        return self.sessions.get(session_id)

    def find_leg(self, fs_uuid: str) -> CallLeg | None:
        """Return the leg object associated with a FreeSWITCH UUID, if any."""
        session = self.find_session(fs_uuid)
        return session.leg_by_uuid(fs_uuid) if session else None

    def control_session_for_uuid(self, fs_uuid: str) -> Any | None:
        """Resolve the ESL control session that can issue commands for this UUID."""
        direct = self.esl_sessions.get(fs_uuid)
        if direct:
            return direct
        session = self.find_session(fs_uuid)
        if not session:
            return None
        return self.esl_sessions.get(session.primary_leg().fs_uuid)


gateway = VoiceGateway()
esl_task: asyncio.Task | None = None
esl_server: asyncio.AbstractServer | None = None
esl_listener_error: str | None = None


class _EOFGuardedStreamReader:
    """Convert EOF into an exception before genesis can spin on empty reads."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader

    async def readline(self) -> bytes:
        line = await self._reader.readline()
        if line == b"":
            raise EOFError("ESL peer closed connection")
        return line

    async def readexactly(self, n: int) -> bytes:
        return await self._reader.readexactly(n)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._reader, name)


async def _callflow_entrypoint(fs_session: Any) -> None:
    """Main outbound ESL handler that binds lifecycle events to gateway actions."""
    context = getattr(fs_session, "context", {}) or {}
    fs_uuid = context.get("Channel-Call-UUID") or context.get("Unique-ID")
    done = asyncio.get_running_loop().create_future()

    async def hangup_handler(_: dict[str, Any]) -> None:
        if not done.done():
            done.set_result(None)

    async def execute_handler(event: dict[str, Any]) -> None:
        if fs_uuid:
            await gateway.handle_freeswitch_execute_event(
                fs_uuid, event, completed=False
            )

    async def execute_complete_handler(event: dict[str, Any]) -> None:
        if fs_uuid:
            await gateway.handle_freeswitch_execute_event(
                fs_uuid, event, completed=True
            )

    try:
        fs_session.on("CHANNEL_HANGUP", hangup_handler)
        fs_session.on("CHANNEL_EXECUTE", execute_handler)
        fs_session.on("CHANNEL_EXECUTE_COMPLETE", execute_complete_handler)
    except Exception:
        pass

    await gateway.connect_call(fs_session)
    try:
        await done
    finally:
        if fs_uuid:
            await gateway.close_call(fs_uuid)


async def _prepare_outbound_esl_session(fs_session: Any, timeout: float) -> None:
    """Run the required outbound ESL setup commands with a bounded handshake."""
    fs_session.context = await asyncio.wait_for(fs_session.send("connect"), timeout)
    await asyncio.wait_for(fs_session.send("myevents"), timeout)
    await asyncio.wait_for(fs_session.send("linger"), timeout)
    fs_session.is_lingering = True


async def _handle_outbound_esl_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Accept one FreeSWITCH outbound ESL connection."""
    from genesis.outbound import Session

    peer = writer.get_extra_info("peername")
    timeout = max(
        0.1,
        env_float(
            "VOICE_GATEWAY_ESL_HANDSHAKE_TIMEOUT_SECONDS",
            DEFAULT_ESL_HANDSHAKE_TIMEOUT_SECONDS,
        ),
    )
    try:
        guarded_reader = _EOFGuardedStreamReader(reader)
        async with Session(guarded_reader, writer) as fs_session:
            await _prepare_outbound_esl_session(fs_session, timeout)
            await _callflow_entrypoint(fs_session)
    except asyncio.TimeoutError:
        logger.warning("Outbound ESL handshake timed out peer=%s", peer)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Outbound ESL connection failed peer=%s", peer)


async def _create_esl_server() -> asyncio.AbstractServer:
    """Bind the FreeSWITCH outbound ESL listener."""
    global esl_listener_error
    host = _outbound_esl_host()
    port = env_int("VOICE_GATEWAY_ESL_PORT", 5050)
    server = await asyncio.start_server(
        _handle_outbound_esl_connection, host, port, family=socket.AF_INET
    )
    esl_listener_error = None
    logger.info("Start outbound ESL listener on '%s:%s'.", host, port)
    return server


def _outbound_esl_host() -> str:
    explicit = (os.getenv("VOICE_GATEWAY_ESL_HOST") or "").strip()
    local_ip = (os.getenv("VOICE_GATEWAY_LOCAL_IP") or "0.0.0.0").strip()
    return explicit or local_ip or "0.0.0.0"


async def _serve_esl_server(server: asyncio.AbstractServer) -> None:
    """Serve the already-bound outbound ESL listener until shutdown."""
    global esl_listener_error
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        esl_listener_error = str(exc)
        logger.exception("Outbound ESL listener stopped unexpectedly")
        raise


def _esl_listener_status() -> dict[str, Any]:
    enabled = env_bool("VOICE_GATEWAY_ENABLE_ESL", True)
    task_running = esl_task is not None and not esl_task.done()
    listening = (
        enabled and task_running and esl_server is not None and esl_server.is_serving()
    )
    return {
        "enabled": enabled,
        "listening": listening,
        "host": _outbound_esl_host(),
        "port": env_int("VOICE_GATEWAY_ESL_PORT", 5050),
        "error": esl_listener_error,
    }


def _handle_esl_task_done(task: asyncio.Task) -> None:
    global esl_listener_error
    if task.cancelled():
        return
    exc = task.exception()
    if not exc:
        return
    esl_listener_error = str(exc)
    logger.critical(
        "Outbound ESL listener task failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    if env_bool("VOICE_GATEWAY_EXIT_ON_ESL_FAILURE", True):
        os._exit(1)


async def _start_esl_listener() -> None:
    """Start the outbound ESL listener used by FreeSWITCH call legs."""
    global esl_server
    server = await _create_esl_server()
    esl_server = server
    try:
        await _serve_esl_server(server)
    finally:
        server.close()
        await server.wait_closed()
        if esl_server is server:
            esl_server = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Application startup/shutdown hook that manages the ESL listener task."""
    global esl_server, esl_task
    if env_bool("VOICE_GATEWAY_ENABLE_ESL", True):
        esl_server = await _create_esl_server()
        esl_task = asyncio.create_task(
            _serve_esl_server(esl_server), name="outbound-esl"
        )
        esl_task.add_done_callback(_handle_esl_task_done)
    try:
        yield
    finally:
        if esl_task:
            esl_task.cancel()
            try:
                await esl_task
            except asyncio.CancelledError:
                pass
            esl_task = None
        if esl_server:
            esl_server.close()
            await esl_server.wait_closed()
            esl_server = None


app = FastAPI(title="Voice Gateway", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    """Return gateway liveness and runtime reliability counters."""
    payload: dict[str, Any] = {
        "status": "ok",
        "active_sessions": len(gateway.sessions),
        "max_active_sessions": gateway.admission.max_active_sessions,
        "esl_listener": _esl_listener_status(),
        "provider_circuits": {
            provider: {
                "state": "open" if circuit.is_open else "closed",
                "failure_count": circuit.failure_count,
                "failure_threshold": circuit.failure_threshold,
                "reset_seconds": circuit.reset_seconds,
            }
            for provider, circuit in gateway.provider_circuits.items()
        },
    }
    esl = payload["esl_listener"]
    if esl["enabled"] and not esl["listening"]:
        payload["status"] = "degraded"
        return JSONResponse(status_code=503, content=payload)
    return JSONResponse(content=payload)


@app.websocket("/media/{fs_uuid}")
async def media(websocket: WebSocket, fs_uuid: str) -> None:
    """Receive raw media frames from mod_audio_stream and forward audio to STT queues."""
    await websocket.accept()
    await gateway.publish_voice_event(
        fs_uuid,
        source="esl",
        type="media.connected",
        payload={"transport": "mod_audio_stream"},
    )
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if "bytes" in message and message["bytes"] is not None:
                await gateway.receive_audio(fs_uuid, message["bytes"])
            elif "text" in message and message["text"]:
                # mod_audio_stream can send metadata or JSON events. Keep them
                # accepted but do not send them to STT.
                continue
    except WebSocketDisconnect:
        return
    finally:
        await gateway.publish_voice_event(
            fs_uuid,
            source="esl",
            type="media.disconnected",
            payload={"transport": "mod_audio_stream"},
        )


@app.websocket("/events")
async def voice_events(websocket: WebSocket) -> None:
    """Stream voice events for a session inferred from websocket query parameters."""
    stream_id = (
        websocket.query_params.get("session_id")
        or websocket.query_params.get("call_id")
        or websocket.query_params.get("stream_id")
        or None
    )
    await _voice_events_socket(websocket, stream_id)


@app.websocket("/events/{stream_id}")
async def voice_events_for_stream(websocket: WebSocket, stream_id: str) -> None:
    """Stream voice events for an explicit stream/session identifier."""
    await _voice_events_socket(websocket, stream_id)


async def _voice_events_socket(websocket: WebSocket, stream_id: str | None) -> None:
    """Run bidirectional websocket loop for event-bus subscriptions."""
    clean_stream_id = (stream_id or "").strip()
    allow_wildcard = env_bool("VOICE_GATEWAY_ALLOW_WILDCARD_EVENTS", False)
    if clean_stream_id in {"", "*"} and not allow_wildcard:
        await websocket.close(code=1008, reason="session_id or call_id is required")
        return
    await websocket.accept()
    subscription = await gateway.event_bus.subscribe(
        clean_stream_id,
        allow_wildcard=allow_wildcard,
    )

    async def sender() -> None:
        while True:
            event = await subscription.queue.get()
            await websocket.send_json(event.to_dict())

    async def receiver() -> None:
        while True:
            await websocket.receive_text()

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait(
            {sender_task, receiver_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            with suppress(WebSocketDisconnect, RuntimeError):
                task.result()
    except WebSocketDisconnect:
        return
    finally:
        sender_task.cancel()
        receiver_task.cancel()
        with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await sender_task
        with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await receiver_task
        await gateway.event_bus.unsubscribe(subscription)


def _context_value(context: dict[str, Any], key: str) -> str:
    """Fetch a context value using common header-key normalizations."""
    candidates = [
        key,
        key.replace("-", "_"),
        key.lower(),
        key.lower().replace("-", "_"),
    ]
    for candidate in candidates:
        value = context.get(candidate)
        if value is not None:
            return str(value).strip()
    return ""


def _voice_event_session_id(context: dict[str, Any]) -> str:
    session_id = _context_value(context, "variable_sip_h_X-Voice-Events-Session")
    if not session_id:
        return ""
    if not _VOICE_EVENT_SESSION_ID_RE.fullmatch(session_id):
        logger.warning("ignoring invalid voice event session id")
        return ""
    return session_id


def _provider_error_payload(provider: str, exc: Exception) -> dict[str, str]:
    if provider in {"llm", "stt", "tts", "translation"}:
        return {
            "type": exc.__class__.__name__,
            "message": f"{provider} provider request failed",
        }
    return {"type": exc.__class__.__name__, "message": str(exc)}


def last_assistant_message(session: CallSession) -> str | None:
    """Return the most recent assistant utterance from session history."""
    for message in reversed(session.history):
        if message.get("role") == "assistant":
            content = message.get("content")
            return str(content) if content is not None else None
    return None


def metadata_for_policy(
    policy_decision: PolicyDecision,
    semantic_frame: SemanticFrame | None,
) -> dict[str, Any]:
    """Serialize policy and semantic-frame data into session metadata."""
    metadata: dict[str, Any] = {"policy": policy_decision.model_dump(mode="json")}
    if semantic_frame:
        metadata["semantic_frame"] = semantic_frame.model_dump(mode="json")
    return metadata


def next_turn_id(session: CallSession) -> str:
    """Increment and return a stable turn identifier for a session."""
    turn_number = int(session.metadata.get("turn_number") or 0) + 1
    session.metadata["turn_number"] = turn_number
    return f"{session.session_id}:turn:{turn_number}"


def merge_stt_partial(previous: str, current: str) -> str:
    """Merge STT partial fragments while preserving punctuation attachment."""
    previous_clean = " ".join(str(previous or "").split())
    current_clean = " ".join(str(current or "").split())
    if not current_clean:
        return previous_clean
    if not previous_clean:
        return current_clean
    if current_clean.lower().startswith(previous_clean.lower()):
        return current_clean
    if re.match(r"^[,.;:!?]", current_clean):
        return f"{previous_clean}{current_clean}"
    return f"{previous_clean} {current_clean}".strip()


def merge_pending_turn_text(previous: str, current: str) -> str:
    """Merge deferred-turn text and smooth sentence continuation casing."""
    previous_clean = " ".join(str(previous or "").split())
    current_clean = " ".join(str(current or "").split())
    if not current_clean:
        return previous_clean
    if not previous_clean:
        return current_clean
    join_base = previous_clean
    if not re.match(r"^[,.;:!?]", current_clean):
        stripped = re.sub(r"[.?!]+$", "", join_base).strip()
        if stripped != join_base:
            join_base = stripped
            if current_clean.startswith("The "):
                current_clean = f"the {current_clean[4:]}"
            elif current_clean.startswith("A "):
                current_clean = f"a {current_clean[2:]}"
            elif current_clean.startswith("An "):
                current_clean = f"an {current_clean[3:]}"
    return merge_stt_partial(join_base, current_clean)


def is_complete_standalone_question_restatement(
    text: str, semantic_frame: SemanticFrame
) -> bool:
    """Return True when a post-clarification final replaces the held fragment."""
    if (
        not semantic_frame.utterance_complete
        or semantic_frame.speech_act != SpeechAct.QUESTION
    ):
        return False
    words = re.sub(r"[^a-z0-9' ]+", "", str(text or "").lower()).split()
    if len(words) < 4:
        return False
    if words[0] in {"why", "what", "how", "when", "where", "who", "which"}:
        return True
    starts = (
        ("let", "me", "ask"),
        ("can", "i", "ask"),
        ("could", "i", "ask"),
        ("can", "you", "tell", "me"),
        ("could", "you", "tell", "me"),
        ("would", "you", "tell", "me"),
        ("can", "you", "explain"),
        ("could", "you", "explain"),
    )
    if any(tuple(words[: len(prefix)]) == prefix for prefix in starts):
        return True
    return words[0] in {"is", "are", "do", "does", "did", "can", "could", "would"}


def append_spoken_text(previous: str, chunk: str) -> str:
    """Append spoken chunk text with punctuation-aware spacing."""
    previous_clean = " ".join(str(previous or "").split())
    chunk_clean = " ".join(str(chunk or "").split())
    if not previous_clean:
        return chunk_clean
    if not chunk_clean:
        return previous_clean
    if chunk_clean.startswith(("'", ",", ".", "!", "?", ";", ":")):
        return f"{previous_clean}{chunk_clean}"
    return f"{previous_clean} {chunk_clean}".strip()


def describe_text_for_voice(text: str) -> dict[str, int]:
    """Return lightweight length metadata used in voice events."""
    clean = str(text or "")
    return {
        "text_chars": len(clean),
        "text_words": len(clean.split()),
    }


def spoken_text_prefix(text: str, heard_fraction: float) -> str:
    """Estimate a natural word-boundary prefix likely heard by the caller."""
    clean = str(text or "")
    if not clean or heard_fraction <= 0:
        return ""
    if heard_fraction >= 0.995:
        return clean
    target_chars = max(0, min(len(clean), int(len(clean) * heard_fraction)))
    if target_chars <= 0:
        return ""
    prefix = clean[:target_chars].rstrip()
    if not prefix:
        return ""
    last_space = prefix.rfind(" ")
    if last_space > 0 and target_chars - last_space <= 18:
        prefix = prefix[:last_space].rstrip()
    return prefix


def undelivered_suffix(generated_text: str, delivered_text: str) -> str:
    """Return generated text that was likely not yet delivered in playback."""
    generated = str(generated_text or "")
    delivered = str(delivered_text or "")
    if not generated:
        return ""
    if not delivered:
        return generated.strip()
    if generated.startswith(delivered):
        return generated[len(delivered) :].lstrip()
    delivered_index = generated.find(delivered)
    if delivered_index >= 0:
        return generated[delivered_index + len(delivered) :].lstrip()
    return generated


def delivery_context_event_payload(context: dict[str, Any]) -> dict[str, Any]:
    """Convert delivery-tracking context into a publishable event payload."""
    payload = {
        "delivery_status": context.get("delivery_status"),
        "turn_id": context.get("turn_id"),
        "delivered_fraction": context.get("delivered_fraction"),
        "interruption_reason": context.get("interruption_reason"),
        "generated_text_chars": context.get(
            "generated_text_chars", len(str(context.get("generated_text") or ""))
        ),
        "delivered_text_chars": context.get(
            "delivered_text_chars", len(str(context.get("delivered_text") or ""))
        ),
        "undelivered_text_chars": context.get(
            "undelivered_text_chars",
            len(str(context.get("undelivered_text") or "")),
        ),
        "playback_started": context.get("playback_started"),
        "playback_timing_source": context.get("playback_timing_source"),
    }
    if context.get("latest_user_text"):
        payload["latest_user_text_chars"] = len(
            str(context.get("latest_user_text") or "")
        )
    if env_bool("VOICE_GATEWAY_DELIVERY_DEBUG_TEXT", False):
        payload.update(
            {
                "generated_text": context.get("generated_text") or "",
                "delivered_text": context.get("delivered_text") or "",
                "undelivered_text": context.get("undelivered_text") or "",
                "latest_user_text": context.get("latest_user_text") or "",
            }
        )
    return payload


def elapsed_ms(started_at: float) -> float:
    """Return elapsed milliseconds from a perf-counter start timestamp."""
    return round((time.perf_counter() - started_at) * 1000, 1)


def elapsed_between_ms(
    started_at: float | None, finished_at: float | None
) -> float | None:
    """Return elapsed milliseconds between two perf-counter timestamps."""
    if started_at is None or finished_at is None:
        return None
    return round(max(0.0, finished_at - started_at) * 1000, 1)


def translation_result_text(result: Any) -> str:
    return str(getattr(result, "text", result))


def translation_result_field(result: Any, field: str) -> str:
    return str(getattr(result, field, "") or "")


def add_elapsed(
    payload: dict[str, Any],
    field: str,
    started_at: float | None,
    finished_at: float | None,
) -> None:
    value = elapsed_between_ms(started_at, finished_at)
    if value is not None:
        payload[field] = value


def safe_float(value: Any, default: float) -> float:
    """Cast to float or fall back to a default when parsing fails."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def stt_item_id(event: dict[str, Any]) -> str:
    """Extract the canonical STT item identifier from an event payload."""
    value = event.get("item_id")
    return str(value) if value else ""


def estimate_tts_playback_seconds(text: str) -> float:
    """Estimate playback duration for text using configurable speech-rate bounds."""
    words = max(1, len(str(text or "").split()))
    words_per_minute = max(1.0, env_float("VOICE_GATEWAY_TTS_WPM", 165.0))
    padding_seconds = max(
        0.0, env_float("VOICE_GATEWAY_TTS_FINISH_PADDING_SECONDS", 0.5)
    )
    min_seconds = max(0.0, env_float("VOICE_GATEWAY_TTS_MIN_SECONDS", 0.8))
    max_seconds = max(min_seconds, env_float("VOICE_GATEWAY_TTS_MAX_SECONDS", 45.0))
    estimate = (words / words_per_minute) * 60.0 + padding_seconds
    return min(max(estimate, min_seconds), max_seconds)


def estimate_tts_start_delay_seconds(estimated_playback_seconds: float) -> float:
    """Estimate delay before audible TTS starts after command acceptance."""
    # FreeSWITCH accepts the speak command before mod_piper_tts has generated
    # the WAV. Local Piper logs show roughly one second of fixed setup plus
    # about 10% of the eventual audio duration before audible playback starts.
    fixed_seconds = max(0.0, env_float("VOICE_GATEWAY_TTS_START_FIXED_SECONDS", 0.95))
    realtime_factor = max(
        0.0, env_float("VOICE_GATEWAY_TTS_START_REALTIME_FACTOR", 0.10)
    )
    min_seconds = max(0.0, env_float("VOICE_GATEWAY_TTS_START_MIN_SECONDS", 0.7))
    max_seconds = max(
        min_seconds, env_float("VOICE_GATEWAY_TTS_START_MAX_SECONDS", 3.0)
    )
    estimate = fixed_seconds + max(0.0, estimated_playback_seconds) * realtime_factor
    return round(min(max(estimate, min_seconds), max_seconds), 3)


def progressive_chunk_fallback_words() -> int:
    """Return clause-chunk fallback threshold with defensive env parsing."""
    try:
        return env_int(
            "VOICE_GATEWAY_PROGRESSIVE_CHUNK_FALLBACK_WORDS",
            DEFAULT_PROGRESSIVE_CHUNK_FALLBACK_WORDS,
        )
    except (TypeError, ValueError):
        return DEFAULT_PROGRESSIVE_CHUNK_FALLBACK_WORDS


def turn_hold_ack_ms() -> int:
    """Return acknowledgement delay for pending-turn hold filler audio."""
    return max(0, env_int("VOICE_TURN_HOLD_ACK_MS", DEFAULT_TURN_HOLD_ACK_MS))


def turn_hold_clarify_ms() -> int:
    """Return delay before clarifying a held underspecified question."""
    return max(
        0,
        env_int("VOICE_TURN_HOLD_CLARIFY_MS", DEFAULT_TURN_HOLD_CLARIFY_MS),
    )


def speech_start_barge_in_debounce_ms() -> int:
    """Return how long speech must continue before VAD-only barge-in fires."""
    return max(
        0,
        env_int(
            "VOICE_GATEWAY_SPEECH_START_BARGE_IN_DEBOUNCE_MS",
            DEFAULT_SPEECH_START_BARGE_IN_DEBOUNCE_MS,
        ),
    )


def speech_start_barge_in_enabled() -> bool:
    """Return whether VAD-only speech-start events may interrupt active TTS."""
    return env_bool(
        "VOICE_GATEWAY_SPEECH_START_BARGE_IN_ENABLED",
        DEFAULT_SPEECH_START_BARGE_IN_ENABLED,
    )


def delivery_resume_delay_seconds() -> float:
    """Return how long to wait after a pause check-in before auto-resuming."""
    return max(
        0.0,
        env_float(
            "VOICE_GATEWAY_DELIVERY_RESUME_DELAY_SECONDS",
            DEFAULT_DELIVERY_RESUME_DELAY_SECONDS,
        ),
    )


def turn_hold_ttl_seconds() -> float:
    """Return expiration window for pending assistant turns."""
    return max(
        0.0, env_float("VOICE_TURN_HOLD_TTL_SECONDS", DEFAULT_TURN_HOLD_TTL_SECONDS)
    )


def turn_hold_filler_text() -> str:
    """Return configured short filler text for turn-hold acknowledgements."""
    return (
        os.getenv("VOICE_TURN_HOLD_FILLER_TEXT", DEFAULT_TURN_HOLD_FILLER_TEXT).strip()
        or DEFAULT_TURN_HOLD_FILLER_TEXT
    )


def turn_hold_clarification_text(semantic_frame: SemanticFrame) -> str:
    """Return a short clarification prompt for an underspecified question."""
    if semantic_frame.clarification_type == "bare_why":
        return "What are you asking why about?"
    if semantic_frame.clarification_type in {
        "bare_question_word",
        "empty_question_preamble",
    }:
        return "What would you like to ask?"
    return "Can you finish the question?"


def is_delivery_pause_control_text(text: str) -> bool:
    """Return whether text is only a short pause command for delivery recovery."""
    normalized = re.sub(r"[^a-z0-9' ]+", "", str(text or "").lower()).strip()
    return normalized in {
        "wait",
        "wait a second",
        "wait one second",
        "hang on",
        "hold on",
        "pause",
        "one second",
        "just a sec",
        "just a second",
    }


def _reply_text(reply: Any) -> str:
    """Normalize FreeSWITCH command reply objects into a single text string."""
    parts = [
        getattr(reply, "body", None),
        reply.get("Body") if hasattr(reply, "get") else None,
        reply.get("Reply-Text") if hasattr(reply, "get") else None,
    ]
    return " ".join(
        str(part or "").strip() for part in parts if part is not None
    ).strip()


def _reply_is_error(reply_text: str) -> bool:
    """Return True when a FreeSWITCH reply indicates an -ERR result."""
    return reply_text.lower().startswith("-err")
