import pytest

from services.voice_gateway.main import (
    POLICY_CONFIRMATION_PROMPT,
    VoiceGateway,
)

from services.voice_gateway.models import (
    CallLeg,
    CallSession,
    LegRole,
    SessionMode,
    SessionState,
)

from tests.support.voice_gateway import (
    FakeTTSClient,
    FakeLLMClient,
    drain_events,
)


@pytest.mark.asyncio
async def test_cancellation_scope_correction_asks_confirmation_without_llm():
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
        {
            "type": "final",
            "text": "I'd like to cancel my account actually just this add-on in my account",
            "is_final": True,
            "confidence": 0.94,
        },
    )

    events = await drain_events(subscription)
    semantic_event = next(
        event for event in events if event["type"] == "policy.semantic_frame"
    )
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert semantic_event["payload"]["correction_detected"] is True
    assert semantic_event["payload"]["slots"]["cancellation_scope"] == "add-on"
    assert policy_event["payload"]["action"] == "CONFIRM_BEFORE_ACTION"
    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests[0]["text"] == POLICY_CONFIRMATION_PROMPT
    assert session.history[-1] == {
        "role": "user",
        "content": "I'd like to cancel my account actually just this add-on in my account",
    }

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_risky_exploration_passes_policy_metadata_to_llm_and_blocks_action():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Nothing has been changed.")
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
        {
            "type": "final",
            "text": "I need to cancel my broadband actually what would the fee be",
            "is_final": True,
            "confidence": 0.98,
        },
    )

    events = await drain_events(subscription)
    blocked_event = next(
        event for event in events if event["type"] == "policy.blocked_action"
    )
    policy_metadata = gateway.llm_client.requests[0]["metadata"]["policy"]
    assert blocked_event["payload"]["action"] == "cancel_service"
    assert policy_metadata["decision"] == "RESPOND"
    assert "cancel_service" in policy_metadata["blocked_actions"]
    assert policy_metadata["safe_to_execute_tools"] is False

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_direct_risky_request_asks_confirmation_without_llm():
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
        {
            "type": "final",
            "text": "Can you change my direct debit to the fifteenth?",
            "is_final": True,
            "confidence": 0.96,
        },
    )

    events = await drain_events(subscription)
    semantic_event = next(
        event for event in events if event["type"] == "policy.semantic_frame"
    )
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    blocked_event = next(
        event for event in events if event["type"] == "policy.blocked_action"
    )
    assert semantic_event["payload"]["speech_act"] == "request"
    assert semantic_event["payload"]["intent"] == "change_direct_debit"
    assert policy_event["payload"]["action"] == "CONFIRM_BEFORE_ACTION"
    assert policy_event["payload"]["requires_confirmation"] is True
    assert blocked_event["payload"]["action"] == "change_direct_debit"
    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests[0]["text"] == POLICY_CONFIRMATION_PROMPT

    gateway.clear_active_speech("uuid-a")
