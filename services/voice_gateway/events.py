"""In-process voice observability event bus."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


VoiceEventSource = Literal[
    "stt", "llm", "tool", "tts", "esl", "webrtc", "policy", "system"
]

VOICE_EVENT_TYPES = {
    "call.created",
    "call.connected",
    "call.answered",
    "call.hangup",
    "browser.call_started",
    "browser.sip_invite_sent",
    "browser.call_established",
    "media.connected",
    "media.disconnected",
    "media.stream_start_requested",
    "media.stream_start_ack",
    "webrtc.get_user_media_started",
    "webrtc.get_user_media_ready",
    "webrtc.ice_connected",
    "webrtc.first_outbound_rtp",
    "webrtc.first_inbound_rtp",
    "admission.accepted",
    "admission.rejected",
    "admission.released",
    "turn.started",
    "turn.latency",
    "translation.latency",
    "user.speech_started",
    "user.speech_stopped",
    "user.barge_in_detected",
    "stt.partial",
    "stt.final",
    "stt.endpoint",
    "stt.activity_started",
    "stt.speech_started",
    "stt.speech_stopped",
    "stt.stream_started",
    "stt.stream_finished",
    "stt.suppressed",
    "stt.error",
    "agent.thinking_started",
    "agent.thinking_finished",
    "llm.request_started",
    "llm.upstream_finished",
    "llm.request_finished",
    "llm.partial_text",
    "llm.final_text",
    "llm.error",
    "tool.call_started",
    "tool.call_progress",
    "tool.call_completed",
    "tts.started",
    "tts.enqueue_started",
    "tts.enqueued",
    "tts.cancel_requested",
    "tts.break_sent",
    "tts.finished",
    "tts.cancelled",
    "delivery.response_interrupted",
    "delivery.context_created",
    "delivery.context_sent_to_llm",
    "delivery.auto_resume",
    "agent.speaking_started",
    "agent.speaking_stopped",
    "tts.error",
    "provider.circuit_opened",
    "provider.circuit_blocked",
    "provider.circuit_closed",
    "policy.evaluation_started",
    "policy.evaluation_finished",
    "policy.semantic_frame",
    "policy.decision",
    "policy.blocked_action",
    "policy.turn_hold",
    "system.error",
    "system.warning",
    "system.debug",
}


@dataclass(frozen=True, slots=True)
class VoiceEvent:
    seq: int
    ts: str
    session_id: str
    call_id: str
    source: VoiceEventSource
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "source": self.source,
            "type": self.type,
            "payload": self.payload,
        }


@dataclass(slots=True)
class VoiceEventSubscription:
    queue: asyncio.Queue[VoiceEvent]
    stream_id: str | None = None

    def matches(self, event: VoiceEvent) -> bool:
        if not self.stream_id or self.stream_id == "*":
            return True
        return event.session_id == self.stream_id or event.call_id == self.stream_id


class VoiceEventBus:
    """Small pub/sub bus for structured call metadata events."""

    def __init__(self, *, subscriber_queue_size: int = 200) -> None:
        self._subscriber_queue_size = subscriber_queue_size
        self._seq_by_session: dict[str, int] = {}
        self._subscribers: dict[int, VoiceEventSubscription] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self,
        *,
        session_id: str,
        call_id: str | None,
        source: VoiceEventSource,
        type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> VoiceEvent:
        if type not in VOICE_EVENT_TYPES:
            raise ValueError(f"unknown voice event type: {type}")
        clean_session_id = str(session_id or call_id or "unknown")
        clean_call_id = str(call_id or clean_session_id)
        async with self._lock:
            next_seq = self._seq_by_session.get(clean_session_id, 0) + 1
            self._seq_by_session[clean_session_id] = next_seq
            event = VoiceEvent(
                seq=next_seq,
                ts=_utc_now(),
                session_id=clean_session_id,
                call_id=clean_call_id,
                source=source,
                type=type,
                payload=dict(payload or {}),
            )
            subscribers = list(self._subscribers.values())

        for subscriber in subscribers:
            if not subscriber.matches(event):
                continue
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    subscriber.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                subscriber.queue.put_nowait(event)
        return event

    async def subscribe(
        self, stream_id: str | None = None, *, allow_wildcard: bool = False
    ) -> VoiceEventSubscription:
        clean_stream_id = (stream_id or "").strip()
        if clean_stream_id in {"", "*"} and not allow_wildcard:
            raise ValueError("voice event stream_id is required")
        subscription = VoiceEventSubscription(
            queue=asyncio.Queue(maxsize=self._subscriber_queue_size),
            stream_id=clean_stream_id or None,
        )
        async with self._lock:
            self._subscribers[id(subscription)] = subscription
        return subscription

    async def unsubscribe(self, subscription: VoiceEventSubscription) -> None:
        async with self._lock:
            self._subscribers.pop(id(subscription), None)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
