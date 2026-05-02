"""Convert exported voice observability events into policy eval inputs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from voice_policy.schema import PolicyInput, RiskLevel


def load_trace_events(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [dict(event) for event in data]
    if isinstance(data, dict):
        events = data.get("events") or data.get("trace") or data.get("items")
        if isinstance(events, list):
            return [dict(event) for event in events]
    raise ValueError("trace must be a JSON list or an object with an events list")


def iter_policy_inputs_from_events(
    events: Iterable[dict[str, Any]],
) -> Iterator[PolicyInput]:
    agent_is_speaking = False
    tts_allow_interruptions = True
    last_agent_message: str | None = None
    pending_action: str | None = None
    pending_action_risk = RiskLevel.NONE
    known_slots: dict[str, Any] = {}
    recent_events: list[dict[str, Any]] = []
    final_turn = 0

    for event in events:
        event_type = str(event.get("type") or "")
        payload = dict(event.get("payload") or {})

        if event_type in {"tts.enqueued", "agent.speaking_started"}:
            agent_is_speaking = True
            tts_allow_interruptions = bool(
                payload.get("interruptible", tts_allow_interruptions)
            )
            last_agent_message = str(payload.get("text") or last_agent_message or "")
        elif event_type in {"agent.speaking_stopped", "tts.finished", "tts.cancelled"}:
            agent_is_speaking = False
        elif event_type in {
            "llm.final_text",
            "agent.thinking_finished",
        } and payload.get("text"):
            last_agent_message = str(payload.get("text"))
        elif event_type == "policy.blocked_action":
            pending_action = str(payload.get("action") or pending_action or "")
            pending_action_risk = RiskLevel(
                str(payload.get("risk") or pending_action_risk.value)
            )
        elif event_type == "policy.semantic_frame" and isinstance(
            payload.get("slots"), dict
        ):
            known_slots.update(payload["slots"])

        if event_type in {"stt.final", "stt.partial"}:
            is_partial = event_type == "stt.partial" or not bool(
                payload.get("is_final", event_type == "stt.final")
            )
            if event_type == "stt.final":
                final_turn += 1
            if event_type == "stt.final" or (is_partial and agent_is_speaking):
                confidence = payload.get("confidence")
                yield PolicyInput(
                    session_id=str(
                        event.get("session_id") or payload.get("session_id") or "trace"
                    ),
                    turn_id=f"turn_{final_turn:03d}" if final_turn else "partial",
                    transcript=str(payload.get("text") or ""),
                    is_partial=is_partial,
                    stt_confidence=1.0
                    if confidence is None
                    else float(confidence),
                    current_flow=str(payload.get("current_flow") or "trace_replay"),
                    last_agent_message=last_agent_message,
                    agent_is_speaking=agent_is_speaking,
                    tts_allow_interruptions=tts_allow_interruptions,
                    pending_action=pending_action or None,
                    pending_action_risk=pending_action_risk,
                    known_slots=known_slots,
                    recent_events=recent_events[-12:],
                )

        recent_events.append(event)
        recent_events = recent_events[-20:]
