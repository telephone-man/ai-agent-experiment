import asyncio
import time

import pytest

from services.voice_gateway.main import (
    DELIVERY_RESUME_BRIDGE_PROMPT,
    SOFT_INTERJECTION_CHECKIN_PROMPT,
    VoiceGateway,
)

from tests.support.voice_gateway import (
    FakeTTSClient,
    FakeLLMClient,
    FakeControlClient,
    drain_events,
    register_assistant_session,
)


@pytest.mark.asyncio
async def test_unheard_assistant_reply_is_not_sent_in_next_llm_history():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("This answer has not reached the caller yet.")
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "What is blue?", "is_final": True, "confidence": 1.0},
    )

    assert session.history == [{"role": "user", "content": "What is blue?"}]

    gateway.llm_client.response = "Replacement answer."
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "What is red?", "is_final": True, "confidence": 1.0},
    )

    second_history = gateway.llm_client.requests[1]["history"]
    assert {"role": "user", "content": "What is blue?"} in second_history
    assert all(
        message["content"] != "This answer has not reached the caller yet."
        for message in second_history
    )

    events = await drain_events(subscription)
    cancel_event = next(event for event in events if event["type"] == "tts.cancelled")
    assert cancel_event["payload"]["spoken_text"] == ""
    assert cancel_event["payload"]["history_committed"] is False

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_partial_playback_barge_in_commits_only_spoken_assistant_prefix():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")
    full_text = "One two three four five six seven eight nine ten."

    await gateway.speak(
        "uuid-a",
        full_text,
        wait_complete=True,
        reason="assistant_response",
        history_session=session,
    )
    active = gateway.active_speech["uuid-a"]
    estimated_playback_ms = float(active.payload["estimated_playback_ms"])
    active.playback_started_at = time.perf_counter() - (estimated_playback_ms / 2000.0)

    await gateway.break_speech(
        "uuid-a", reason="barge_in_or_new_turn", publish_events=True
    )

    assert session.history[-1]["role"] == "assistant"
    assert session.history[-1]["content"]
    assert session.history[-1]["content"] != full_text
    assert full_text.startswith(session.history[-1]["content"])

    events = await drain_events(subscription)
    break_event = next(event for event in events if event["type"] == "tts.break_sent")
    assert break_event["payload"]["history_committed"] is True
    assert break_event["payload"]["spoken_text"] == session.history[-1]["content"]
    assert 0 < break_event["payload"]["heard_fraction"] < 1


@pytest.mark.asyncio
async def test_interrupted_assistant_reply_is_sent_as_delivery_context_on_next_turn():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Continue with the missing conclusion.")
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")
    full_text = "The sky is blue because shorter blue wavelengths scatter more than red wavelengths."

    await gateway.speak(
        "uuid-a",
        full_text,
        wait_complete=True,
        reason="assistant_response",
        history_session=session,
        generated_text=full_text,
    )
    active = gateway.active_speech["uuid-a"]
    estimated_playback_ms = float(active.payload["estimated_playback_ms"])
    active.playback_started_at = time.perf_counter() - (estimated_playback_ms / 2000.0)

    await gateway.break_speech(
        "uuid-a",
        reason="barge_in_or_new_turn",
        latest_user_text="Can you keep going?",
        publish_events=True,
    )

    context = session.metadata["previous_assistant_delivery"]
    assert context["delivery_status"] == "interrupted"
    assert context["generated_text"] == full_text
    assert context["delivered_text"]
    assert context["undelivered_text"]
    assert full_text.startswith(context["delivered_text"])
    assert session.history[-1] == {
        "role": "assistant",
        "content": context["delivered_text"],
    }

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Can you keep going?",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    request_metadata = gateway.llm_client.requests[-1]["metadata"]
    sent_context = request_metadata["previous_assistant_delivery"]
    assert sent_context["generated_text"] == full_text
    assert sent_context["delivered_text"] == context["delivered_text"]
    assert sent_context["undelivered_text"] == context["undelivered_text"]
    assert sent_context["latest_user_text"] == "Can you keep going?"
    assert "previous_assistant_delivery" not in session.metadata

    next_history = gateway.llm_client.requests[-1]["history"]
    assert {"role": "assistant", "content": context["delivered_text"]} in next_history
    assert all(message["content"] != full_text for message in next_history)

    events = await drain_events(subscription)
    event_types = [event["type"] for event in events]
    assert "delivery.response_interrupted" in event_types
    assert "delivery.context_created" in event_types
    assert "delivery.context_sent_to_llm" in event_types


@pytest.mark.asyncio
async def test_pause_control_checkin_auto_resumes_interrupted_delivery(monkeypatch):
    monkeypatch.setenv("VOICE_GATEWAY_DELIVERY_RESUME_DELAY_SECONDS", "0.05")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("This should not be called.")
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")
    full_text = "Cars are fast because engines, transmissions, tires, and aerodynamic designs work together."

    await gateway.speak(
        "uuid-a",
        full_text,
        wait_complete=True,
        reason="assistant_response",
        history_session=session,
        generated_text=full_text,
    )
    active = gateway.active_speech["uuid-a"]
    estimated_playback_ms = float(active.payload["estimated_playback_ms"])
    active.playback_started_at = time.perf_counter() - (estimated_playback_ms / 2000.0)

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "partial", "text": "Wait", "is_final": False, "confidence": 1.0},
    )
    context = session.metadata["previous_assistant_delivery"]
    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert gateway.tts_client.requests[-1]["text"] == SOFT_INTERJECTION_CHECKIN_PROMPT

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "Wait.", "is_final": True, "confidence": 1.0},
    )
    assert gateway.llm_client.requests == []

    await asyncio.sleep(0.08)

    assert "previous_assistant_delivery" not in session.metadata
    assert gateway.llm_client.requests == []
    resumed = str(gateway.tts_client.requests[-1]["text"])
    assert resumed.startswith(DELIVERY_RESUME_BRIDGE_PROMPT)
    assert context["undelivered_text"] in resumed

    events = await drain_events(subscription)
    policy_actions = [
        event["payload"]["action"]
        for event in events
        if event["type"] == "policy.decision"
    ]
    assert policy_actions == ["SOFT_INTERRUPT_CHECKIN", "SUPPRESS"]
    assert "delivery.auto_resume" in [event["type"] for event in events]


@pytest.mark.asyncio
async def test_interruption_before_playback_marks_whole_response_undelivered():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    full_text = "This conclusion was generated but never reached playback."

    await gateway.speak(
        "uuid-a",
        full_text,
        wait_complete=True,
        reason="assistant_response",
        history_session=session,
        generated_text=full_text,
    )

    await gateway.break_speech(
        "uuid-a",
        reason="barge_in_or_new_turn",
        latest_user_text="Actually, wait.",
        publish_events=True,
    )

    context = session.metadata["previous_assistant_delivery"]
    assert context["delivery_status"] == "cancelled_before_playback"
    assert context["delivered_text"] == ""
    assert context["undelivered_text"] == full_text
    assert session.history == []


@pytest.mark.asyncio
async def test_completed_assistant_reply_does_not_send_delivery_context():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Next answer.")
    session = register_assistant_session(gateway)
    session.history.append(
        {"role": "assistant", "content": "A completed previous answer."}
    )

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Another question?",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    request_metadata = gateway.llm_client.requests[-1]["metadata"]
    assert "previous_assistant_delivery" not in request_metadata
