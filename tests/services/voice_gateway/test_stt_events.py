import asyncio

import pytest

from services.voice_gateway.main import STT_UNAVAILABLE_SPOKEN_MESSAGE, VoiceGateway

from tests.support.voice_gateway import (
    FakeTTSClient,
    FakeLLMClient,
    drain_events,
    register_assistant_session,
)


@pytest.mark.asyncio
async def test_stt_final_event_includes_openai_audio_diagnostics():
    gateway = VoiceGateway()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe(session.session_id)

    await gateway.publish_stt_activity(
        "uuid-a",
        {
            "type": "final",
            "text": "Thank you",
            "is_final": True,
            "confidence": None,
            "confidence_source": "not_provided",
            "language": "en",
            "provider": "openai",
            "audio_ms": 220.0,
            "speech_ms": 180.0,
            "avg_rms": 900.0,
            "peak_rms": 2400.0,
        },
    )

    events = await drain_events(subscription)
    final_event = next(event for event in events if event["type"] == "stt.final")
    assert final_event["payload"]["confidence"] is None
    assert final_event["payload"]["confidence_source"] == "not_provided"
    assert final_event["payload"]["audio_ms"] == 220.0
    assert final_event["payload"]["speech_ms"] == 180.0
    assert final_event["payload"]["avg_rms"] == 900.0
    assert final_event["payload"]["peak_rms"] == 2400.0


@pytest.mark.asyncio
async def test_stt_activity_start_is_explicit_when_lifecycle_is_missing():
    gateway = VoiceGateway()
    register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.publish_stt_activity(
        "uuid-a",
        {
            "type": "partial",
            "text": "Hello",
            "is_final": False,
            "provider": "local_fallback",
            "fallback": True,
            "fallback_reason": "offline speech activity detector",
        },
    )

    events = await drain_events(subscription)
    assert [event["type"] for event in events] == [
        "stt.activity_started",
        "user.speech_started",
        "stt.partial",
    ]
    assert events[0]["payload"]["inference"] == "first_stt_activity"
    assert events[1]["payload"]["inferred"] is True
    assert events[1]["payload"]["inference"] == "first_stt_activity"


@pytest.mark.asyncio
async def test_suppressed_stt_event_is_debug_only():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "suppressed",
            "text": "Okay",
            "is_final": True,
            "reason": "speech_too_short",
            "provider": "openai",
            "language": "en",
            "speech_ms": 40.0,
            "avg_rms": 180.0,
            "peak_rms": 300.0,
        },
    )

    events = await drain_events(subscription)
    assert [event["type"] for event in events] == ["stt.suppressed"]
    assert events[0]["payload"]["reason"] == "speech_too_short"
    assert gateway.llm_client.requests == []
    assert session.history == []


@pytest.mark.asyncio
async def test_stt_error_speaks_caller_facing_failure_once():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")
    error_event = {
        "type": "error",
        "detail": {
            "type": "upstream_connection_error",
            "message": "OpenAI transcription failed",
            "error_type": "TimeoutError",
        },
    }

    await gateway.handle_transcript("uuid-a", error_event)
    await gateway.handle_transcript("uuid-a", error_event)

    events = await drain_events(subscription)
    assert [request["text"] for request in gateway.tts_client.requests] == [
        STT_UNAVAILABLE_SPOKEN_MESSAGE
    ]
    assert [event["type"] for event in events].count("stt.error") == 2
    spoken_event = next(
        event
        for event in events
        if event["type"] == "tts.started"
        and event["payload"]["reason"] == "stt_unavailable"
    )
    assert spoken_event["payload"]["text"] == STT_UNAVAILABLE_SPOKEN_MESSAGE
    assert spoken_event["payload"]["stt_error_type"] == "upstream_connection_error"
    assert spoken_event["payload"]["stt_error_message"] == "OpenAI transcription failed"
    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_stt_stream_exception_speaks_caller_facing_failure():
    class FailingSTTClient:
        async def stream_audio(self, *args, **kwargs):
            raise TimeoutError("timed out during opening handshake")

    gateway = VoiceGateway()
    gateway.stt_client = FailingSTTClient()
    gateway.tts_client = FakeTTSClient()
    register_assistant_session(gateway)
    gateway.audio_queues["uuid-a"] = asyncio.Queue(maxsize=200)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway._run_stt("uuid-a")

    events = await drain_events(subscription)
    assert [request["text"] for request in gateway.tts_client.requests] == [
        STT_UNAVAILABLE_SPOKEN_MESSAGE
    ]
    assert any(event["type"] == "stt.error" for event in events)
    assert not any("timed out during opening handshake" in str(event) for event in events)
    assert any(
        event["type"] == "tts.started"
        and event["payload"]["reason"] == "stt_unavailable"
        for event in events
    )
    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_stt_lifecycle_events_are_observability_only():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    gateway.stt_partial_transcripts["uuid-a"] = "Why is the sky"
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_started",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-1",
            "audio_start_ms": 120,
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_stopped",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-1",
            "audio_end_ms": 2340,
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "endpoint",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-1",
            "previous_item_id": "item-0",
            "audio_ms": 2220.0,
        },
    )

    events = await drain_events(subscription)
    assert [event["type"] for event in events] == [
        "stt.speech_started",
        "user.speech_started",
        "stt.speech_stopped",
        "user.speech_stopped",
        "stt.endpoint",
    ]
    assert events[0]["payload"]["item_id"] == "item-1"
    assert events[1]["payload"]["source_event"] == "stt.speech_started"
    assert events[2]["payload"]["audio_end_ms"] == 2340
    assert events[3]["payload"]["source_event"] == "stt.speech_stopped"
    assert events[4]["payload"]["previous_item_id"] == "item-0"
    assert "uuid-a" not in gateway.stt_partial_transcripts
    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests == []
    assert session.history == []


@pytest.mark.asyncio
async def test_final_after_vad_lifecycle_does_not_emit_duplicate_speech_state(
    monkeypatch,
):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_started",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-1",
            "audio_start_ms": 120,
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_stopped",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-1",
            "audio_end_ms": 2340,
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "final",
            "text": "Why is the sky",
            "is_final": True,
            "confidence": 1.0,
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-1",
        },
    )

    events = await drain_events(subscription)
    assert [
        event["type"] for event in events if event["type"].startswith("user.speech_")
    ] == [
        "user.speech_started",
        "user.speech_stopped",
    ]
    final_event = next(event for event in events if event["type"] == "stt.final")
    assert final_event["payload"]["item_id"] == "item-1"
    assert final_event["payload"]["vad_mode"] == "server_vad"
    assert gateway.llm_client.requests == []
    assert any(
        event["type"] == "policy.turn_hold" and event["payload"]["status"] == "started"
        for event in events
    )

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


def test_openai_stt_without_confidence_is_not_treated_as_certain():
    gateway = VoiceGateway()
    session = register_assistant_session(gateway)

    policy_input = gateway.policy_input_for_assistant_event(
        session,
        "uuid-a",
        {"type": "final", "text": "Hello", "is_final": True, "provider": "openai"},
    )

    assert policy_input.stt_confidence == 0.8
