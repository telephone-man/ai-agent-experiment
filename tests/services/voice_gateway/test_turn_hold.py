import asyncio

import pytest

from services.voice_gateway.main import VoiceGateway

from services.voice_gateway.models import (
    CallLeg,
    CallSession,
    LegRole,
    SessionMode,
    SessionState,
)

from tests.support.voice_gateway import (
    FakeFSSession,
    FakeTTSClient,
    FakeLLMClient,
    FakeControlClient,
    drain_events,
    register_assistant_session,
)


@pytest.mark.asyncio
async def test_mid_thought_final_waits_without_llm_or_tts():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "I need to", "is_final": True, "confidence": 1.0},
    )

    events = await drain_events(subscription)
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert policy_event["payload"]["action"] == "WAIT"
    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests == []

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


@pytest.mark.asyncio
async def test_question_preamble_final_is_held_without_llm_or_response_tts(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    session.history.append(
        {"role": "assistant", "content": "Sure - go ahead and ask your question."}
    )
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Let me ask why.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    events = await drain_events(subscription)
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    hold_event = next(event for event in events if event["type"] == "policy.turn_hold")
    assert policy_event["payload"]["action"] == "WAIT"
    assert hold_event["payload"]["status"] == "started"
    assert hold_event["payload"]["buffered_text"] == "Let me ask why."
    assert hold_event["payload"]["held_for_ms"] >= 0
    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests == []
    assert session.metadata.get("turn_number") is None

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


@pytest.mark.asyncio
async def test_empty_question_preamble_final_is_held(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "Let me ask.", "is_final": True, "confidence": 1.0},
    )

    events = await drain_events(subscription)
    semantic_event = next(
        event for event in events if event["type"] == "policy.semantic_frame"
    )
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    hold_event = next(event for event in events if event["type"] == "policy.turn_hold")
    assert semantic_event["payload"]["utterance_complete"] is False
    assert policy_event["payload"]["action"] == "WAIT"
    assert hold_event["payload"]["buffered_text"] == "Let me ask."
    assert gateway.llm_client.requests == []

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


@pytest.mark.asyncio
async def test_held_candidate_final_breaks_interruptible_active_tts(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = register_assistant_session(gateway)
    session.history.append(
        {"role": "assistant", "content": "Sure - go ahead and ask your question."}
    )
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "Sure, go ahead and ask your question.",
        wait_complete=True,
        reason="assistant_response",
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Let me ask why.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert fs_session.sent == []
    assert gateway.llm_client.requests == []

    events = await drain_events(subscription)
    assert any(
        event["type"] == "policy.turn_hold" and event["payload"]["status"] == "started"
        for event in events
    )
    assert any(event["type"] == "tts.break_sent" for event in events)

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


@pytest.mark.asyncio
async def test_pending_question_preamble_merges_next_final_and_calls_llm_once(
    monkeypatch,
):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Merged answer.")
    session = register_assistant_session(gateway)
    session.history.append(
        {"role": "assistant", "content": "Sure - go ahead and ask your question."}
    )
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Let me ask why.",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "The sky is blue.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert [request["text"] for request in gateway.llm_client.requests] == [
        "Let me ask why the sky is blue."
    ]
    assert gateway.tts_client.requests[-1]["text"] == "Merged answer."
    assert session.history[-1] == {
        "role": "user",
        "content": "Let me ask why the sky is blue.",
    }

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "merged",
    ]
    assert (
        hold_events[-1]["payload"]["merged_text"] == "Let me ask why the sky is blue."
    )
    turn_events = [event for event in events if event["type"] == "turn.started"]
    assert len(turn_events) == 1
    assert turn_events[0]["payload"]["text"] == "Let me ask why the sky is blue."

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_pending_question_preamble_with_incomplete_merged_body_stays_held(
    monkeypatch,
):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Should not answer yet.")
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Let me ask why?",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "The sky blue.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests == []
    pending = gateway.pending_assistant_turns["uuid-a"]
    assert pending.text == "Let me ask why the sky blue."

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "merged",
        "started",
    ]
    assert hold_events[-1]["payload"]["buffered_text"] == (
        "Let me ask why the sky blue."
    )
    policy_events = [event for event in events if event["type"] == "policy.decision"]
    assert policy_events[-1]["payload"]["action"] == "WAIT"
    assert "incomplete" in policy_events[-1]["payload"]["flags"]

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


@pytest.mark.asyncio
async def test_can_i_ask_why_then_the_sky_stays_held(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Should not answer yet.")
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Can I ask why?",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "The sky", "is_final": True, "confidence": 1.0},
    )

    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests == []
    pending = gateway.pending_assistant_turns["uuid-a"]
    assert pending.text == "Can I ask why the sky"

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "merged",
        "started",
    ]
    assert hold_events[-1]["payload"]["buffered_text"] == "Can I ask why the sky"
    policy_events = [event for event in events if event["type"] == "policy.decision"]
    assert policy_events[-1]["payload"]["action"] == "WAIT"

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


@pytest.mark.asyncio
async def test_progressive_question_preamble_waits_until_predicate_arrives(
    monkeypatch,
):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Bill answer.")
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Can I ask why?",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "my bill", "is_final": True, "confidence": 1.0},
    )

    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests == []
    assert gateway.pending_assistant_turns["uuid-a"].text == "Can I ask why my bill"

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "changed.", "is_final": True, "confidence": 1.0},
    )

    assert [request["text"] for request in gateway.llm_client.requests] == [
        "Can I ask why my bill changed."
    ]
    assert gateway.tts_client.requests[-1]["text"] == "Bill answer."

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "merged",
        "started",
        "merged",
    ]
    assert hold_events[-1]["payload"]["merged_text"] == (
        "Can I ask why my bill changed."
    )

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_can_you_tell_me_why_preamble_merges_next_final(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Merged answer.")
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "So can you tell me why?",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert gateway.llm_client.requests == []

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "The sky is blue.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert [request["text"] for request in gateway.llm_client.requests] == [
        "So can you tell me why the sky is blue."
    ]
    assert gateway.tts_client.requests[-1]["text"] == "Merged answer."

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "merged",
    ]
    assert (
        hold_events[-1]["payload"]["merged_text"]
        == "So can you tell me why the sky is blue."
    )

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_incomplete_wh_clause_merges_next_final(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Blue answer.")
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Why the sky is",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "blue.", "is_final": True, "confidence": 1.0},
    )

    assert [request["text"] for request in gateway.llm_client.requests] == [
        "Why the sky is blue."
    ]
    assert gateway.tts_client.requests[-1]["text"] == "Blue answer."

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "merged",
    ]
    assert hold_events[-1]["payload"]["merged_text"] == "Why the sky is blue."

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_why_is_the_noun_phrase_holds_and_merges_next_final(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Blue answer.")
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Why is the sky",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert gateway.llm_client.requests == []

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "blue.", "is_final": True, "confidence": 1.0},
    )

    assert [request["text"] for request in gateway.llm_client.requests] == [
        "Why is the sky blue."
    ]

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "merged",
    ]
    assert hold_events[-1]["payload"]["merged_text"] == "Why is the sky blue."

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_question_turn_hold_speaks_targeted_clarification(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_CLARIFY_MS", "1")
    monkeypatch.setenv("VOICE_TURN_HOLD_TTL_SECONDS", "10")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Can I ask why?",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await asyncio.sleep(0.02)

    assert gateway.llm_client.requests == []
    assert [request["text"] for request in gateway.tts_client.requests] == [
        "What are you asking why about?"
    ]
    assert session.history == []

    events = await drain_events(subscription)
    policy_events = [event for event in events if event["type"] == "policy.decision"]
    assert [event["payload"]["action"] for event in policy_events] == [
        "WAIT",
        "CLARIFY",
    ]
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "clarification_spoken",
    ]
    assert hold_events[-1]["payload"]["clarification_type"] == "bare_why"
    assert not any(
        event["type"] == "tts.started"
        and event["payload"].get("reason") == "turn_hold_filler"
        for event in events
    )
    clarification_started = next(
        event
        for event in events
        if event["type"] == "tts.started"
        and event["payload"].get("reason") == "turn_hold_clarification"
    )
    assert clarification_started["payload"]["text"] == (
        "What are you asking why about?"
    )

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )
    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_turn_hold_filler_speaks_without_history_or_llm(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "1")
    monkeypatch.setenv("VOICE_TURN_HOLD_TTL_SECONDS", "10")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    session.history.append(
        {"role": "assistant", "content": "Sure - go ahead and ask your question."}
    )
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "I need to",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await asyncio.sleep(0.02)

    assert gateway.llm_client.requests == []
    assert len(gateway.tts_client.requests) == 1
    request = dict(gateway.tts_client.requests[0])
    assert request.pop("event_uuid")
    assert request.pop("fs_host") == "freeswitch"
    assert request == {
        "fs_uuid": "uuid-a",
        "text": "Okay.",
        "language": None,
        "interruptible": True,
        "wait_complete": False,
    }
    assert session.history == [
        {"role": "assistant", "content": "Sure - go ahead and ask your question."}
    ]

    events = await drain_events(subscription)
    assert any(
        event["type"] == "policy.turn_hold"
        and event["payload"]["status"] == "filler_spoken"
        for event in events
    )
    filler_started = next(
        event
        for event in events
        if event["type"] == "tts.started"
        and event["payload"]["reason"] == "turn_hold_filler"
    )
    assert filler_started["payload"]["text"] == "Okay."

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )


@pytest.mark.asyncio
async def test_turn_hold_clarification_is_cancelled_when_user_speech_restarts(
    monkeypatch,
):
    monkeypatch.setenv("VOICE_TURN_HOLD_CLARIFY_MS", "50")
    monkeypatch.setenv("VOICE_TURN_HOLD_TTL_SECONDS", "10")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Can I ask why?",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_started",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-continuation",
            "audio_start_ms": 26964,
        },
    )
    await asyncio.sleep(0.08)

    assert gateway.tts_client.requests == []
    assert gateway.llm_client.requests == []
    assert "uuid-a" in gateway.pending_assistant_turns

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == ["started"]
    assert not any(
        event["type"] == "tts.started"
        and event["payload"].get("reason") == "turn_hold_clarification"
        for event in events
    )

    await gateway.cancel_pending_assistant_turn(
        "uuid-a", status="cancelled", reason="test_cleanup"
    )
    gateway.active_user_speech.discard("uuid-a")


@pytest.mark.asyncio
async def test_continuation_after_turn_hold_clarification_still_merges_original_text(
    monkeypatch,
):
    monkeypatch.setenv("VOICE_TURN_HOLD_CLARIFY_MS", "1")
    monkeypatch.setenv("VOICE_TURN_HOLD_TTL_SECONDS", "10")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Merged answer.")
    session = register_assistant_session(gateway)
    session.history.append(
        {"role": "assistant", "content": "Sure - go ahead and ask your question."}
    )

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Let me ask why.",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await asyncio.sleep(0.02)
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "The sky is blue.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert [request["text"] for request in gateway.llm_client.requests] == [
        "Let me ask why the sky is blue."
    ]
    assert [request["text"] for request in gateway.tts_client.requests] == [
        "What are you asking why about?",
        "Merged answer.",
    ]

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_complete_restatement_after_clarification_replaces_pending_fragment(
    monkeypatch,
):
    monkeypatch.setenv("VOICE_TURN_HOLD_CLARIFY_MS", "1")
    monkeypatch.setenv("VOICE_TURN_HOLD_TTL_SECONDS", "10")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Restated answer.")
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Can I ask why?",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await asyncio.sleep(0.02)
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Why is the sky blue?",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert [request["text"] for request in gateway.llm_client.requests] == [
        "Why is the sky blue?"
    ]
    assert [request["text"] for request in gateway.tts_client.requests] == [
        "What are you asking why about?",
        "Restated answer.",
    ]
    assert gateway.pending_assistant_turns == {}
    assert session.history[-1] == {"role": "user", "content": "Why is the sky blue?"}

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "clarification_spoken",
        "cancelled",
    ]
    assert hold_events[-1]["payload"]["reason"] == (
        "standalone_restatement_after_clarification"
    )

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_goodbye_while_turn_hold_pending_discards_pending_text(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_HOLD_ACK_MS", "100000")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    session = register_assistant_session(gateway)
    session.history.append(
        {"role": "assistant", "content": "Sure - go ahead and ask your question."}
    )
    gateway.esl_sessions["uuid-a"] = FakeFSSession()
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Let me ask why.",
            "is_final": True,
            "confidence": 1.0,
        },
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "Goodbye.", "is_final": True, "confidence": 1.0},
    )

    assert gateway.llm_client.requests == []
    assert [request["text"] for request in gateway.tts_client.requests] == ["Goodbye."]
    assert gateway.pending_assistant_turns == {}

    events = await drain_events(subscription)
    hold_events = [event for event in events if event["type"] == "policy.turn_hold"]
    assert [event["payload"]["status"] for event in hold_events] == [
        "started",
        "cancelled",
    ]
    assert hold_events[-1]["payload"]["reason"] == "goodbye"
    end_policy = [event for event in events if event["type"] == "policy.decision"][-1]
    assert end_policy["payload"]["action"] == "END_CALL"

    gateway.clear_active_speech("uuid-a")
