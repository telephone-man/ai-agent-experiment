import asyncio

import pytest

from services.voice_gateway.main import (
    SOFT_INTERJECTION_CHECKIN_PROMPT,
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
    FakeFSSession,
    FakeTTSClient,
    FakeLLMClient,
    FakeControlClient,
    RejectingControlClient,
    drain_events,
    register_assistant_session,
)


@pytest.mark.asyncio
async def test_partial_barge_in_breaks_active_tts_playback():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response is still being spoken by FreeSWITCH even after the TTS API has queued it.",
        wait_complete=True,
        reason="assistant_response",
    )

    assert gateway.is_agent_speaking("uuid-a") is True
    assert session.state == SessionState.SPEAKING

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "partial",
            "text": "Actually I need to ask something else",
            "is_final": False,
            "confidence": 1.0,
        },
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert fs_session.sent == []
    assert gateway.is_agent_speaking("uuid-a") is False
    assert session.state == SessionState.LISTENING

    events = await drain_events(subscription)

    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert policy_event["payload"]["agent_is_speaking"] is True
    assert policy_event["payload"]["should_interrupt"] is True
    assert policy_event["payload"]["action"] == "CANCEL_TTS_AND_LISTEN"
    assert any(event["type"] == "user.barge_in_detected" for event in events)
    assert any(event["type"] == "tts.cancel_requested" for event in events)
    break_event = next(event for event in events if event["type"] == "tts.break_sent")
    assert break_event["payload"]["command_path"] == "inbound_control"
    assert break_event["payload"]["command_success"] is True
    assert any(event["type"] == "tts.cancelled" for event in events)


@pytest.mark.asyncio
async def test_speech_started_barge_in_breaks_active_tts_before_transcript(monkeypatch):
    monkeypatch.setenv("VOICE_GATEWAY_SPEECH_START_BARGE_IN_ENABLED", "1")
    monkeypatch.setenv("VOICE_GATEWAY_SPEECH_START_BARGE_IN_DEBOUNCE_MS", "1")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = register_assistant_session(gateway)
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response should stop as soon as sustained caller speech is detected.",
        wait_complete=True,
        reason="assistant_response",
    )

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_started",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-barge",
            "audio_start_ms": 8052,
        },
    )
    await asyncio.sleep(0.02)

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert fs_session.sent == []
    assert gateway.is_agent_speaking("uuid-a") is False
    assert session.state == SessionState.LISTENING

    events = await drain_events(subscription)
    event_types = [event["type"] for event in events]
    assert event_types.index("user.speech_started") < event_types.index(
        "tts.cancel_requested"
    )
    barge_event = next(
        event for event in events if event["type"] == "user.barge_in_detected"
    )
    assert barge_event["payload"]["trigger"] == "speech_started"
    assert barge_event["payload"]["item_id"] == "item-barge"
    assert any(event["type"] == "tts.cancelled" for event in events)
    assert not any(event["type"] == "policy.decision" for event in events)


@pytest.mark.asyncio
async def test_short_speech_started_does_not_break_active_tts(monkeypatch):
    monkeypatch.setenv("VOICE_GATEWAY_SPEECH_START_BARGE_IN_ENABLED", "1")
    monkeypatch.setenv("VOICE_GATEWAY_SPEECH_START_BARGE_IN_DEBOUNCE_MS", "50")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response should survive a short VAD blip.",
        wait_complete=True,
        reason="assistant_response",
    )

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_started",
            "provider": "openai",
            "language": "en",
            "vad_mode": "server_vad",
            "item_id": "item-short",
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
            "item_id": "item-short",
            "audio_end_ms": 240,
        },
    )
    await asyncio.sleep(0.08)

    assert gateway.fs_control.api_commands == []
    assert gateway.is_agent_speaking("uuid-a") is True
    assert gateway.speech_start_barge_tasks == {}

    events = await drain_events(subscription)
    assert [event["type"] for event in events] == [
        "tts.started",
        "tts.enqueue_started",
        "tts.enqueued",
        "stt.speech_started",
        "user.speech_started",
        "stt.speech_stopped",
        "user.speech_stopped",
    ]
    assert not any(event["type"] == "user.barge_in_detected" for event in events)
    assert not any(event["type"] == "tts.cancel_requested" for event in events)

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_courtesy_false_positive_does_not_cancel_active_tts():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = register_assistant_session(gateway)
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response has been queued and should survive a cough-like courtesy transcript.",
        wait_complete=True,
        reason="assistant_response",
    )

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "partial", "text": "Thank", "is_final": False, "confidence": None},
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "partial", "text": " you", "is_final": False, "confidence": None},
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "Thank you", "is_final": True, "confidence": None},
    )

    assert gateway.fs_control.api_commands == []
    assert fs_session.sent == []
    assert gateway.is_agent_speaking("uuid-a") is True
    assert len(gateway.tts_client.requests) == 1

    events = await drain_events(subscription)
    policy_events = [event for event in events if event["type"] == "policy.decision"]
    assert [event["payload"]["action"] for event in policy_events] == [
        "WAIT",
        "WAIT",
        "SUPPRESS",
    ]
    assert all(event["payload"]["should_interrupt"] is False for event in policy_events)
    assert not any(event["type"] == "user.barge_in_detected" for event in events)
    assert not any(event["type"] == "tts.cancel_requested" for event in events)

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_single_hesitation_during_active_tts_is_suppressed():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = register_assistant_session(gateway)
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response is still being spoken when the caller hesitates.",
        wait_complete=True,
        reason="assistant_response",
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "ermmmm", "is_final": True, "confidence": 1.0},
    )

    assert gateway.fs_control.api_commands == []
    assert fs_session.sent == []
    assert gateway.llm_client.requests == []
    assert len(gateway.tts_client.requests) == 1
    assert gateway.soft_interjections_during_tts["uuid-a"] == 1
    assert session.history == []

    events = await drain_events(subscription)
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert policy_event["payload"]["action"] == "SUPPRESS"
    assert policy_event["payload"]["should_interrupt"] is False
    assert "soft_interjection_suppressed" in policy_event["payload"]["flags"]
    assert not any(event["type"] == "user.barge_in_detected" for event in events)
    assert not any(event["type"] == "tts.break_sent" for event in events)
    assert not any(
        event["type"] == "tts.started"
        and event["payload"]["reason"] == "soft_interjection_checkin"
        for event in events
    )

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_repeated_backchannel_during_same_tts_speaks_checkin():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response is long enough for two short acknowledgements.",
        wait_complete=True,
        reason="assistant_response",
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "ok", "is_final": True, "confidence": 1.0},
    )

    assert gateway.soft_interjections_during_tts["uuid-a"] == 1
    assert gateway.fs_control.api_commands == []
    assert len(gateway.tts_client.requests) == 1

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "ok", "is_final": True, "confidence": 1.0},
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests[-1]["text"] == SOFT_INTERJECTION_CHECKIN_PROMPT

    events = await drain_events(subscription)
    policy_events = [event for event in events if event["type"] == "policy.decision"]
    assert [event["payload"]["action"] for event in policy_events] == [
        "SUPPRESS",
        "SOFT_INTERRUPT_CHECKIN",
    ]
    assert policy_events[0]["payload"]["should_interrupt"] is False
    assert policy_events[1]["payload"]["should_interrupt"] is True
    assert any(event["type"] == "user.barge_in_detected" for event in events)
    assert any(
        event["type"] == "tts.started"
        and event["payload"]["reason"] == "soft_interjection_checkin"
        for event in events
    )

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_repeated_hesitation_during_same_tts_speaks_checkin():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient()
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response is long enough for two hesitation interjections.",
        wait_complete=True,
        reason="assistant_response",
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "ermmmm", "is_final": True, "confidence": 1.0},
    )

    assert gateway.soft_interjections_during_tts["uuid-a"] == 1
    assert gateway.fs_control.api_commands == []
    assert len(gateway.tts_client.requests) == 1

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "ermmmm", "is_final": True, "confidence": 1.0},
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert gateway.llm_client.requests == []
    assert gateway.tts_client.requests[-1]["text"] == SOFT_INTERJECTION_CHECKIN_PROMPT

    events = await drain_events(subscription)
    policy_events = [event for event in events if event["type"] == "policy.decision"]
    assert [event["payload"]["action"] for event in policy_events] == [
        "SUPPRESS",
        "SOFT_INTERRUPT_CHECKIN",
    ]
    assert policy_events[0]["payload"]["should_interrupt"] is False
    assert policy_events[1]["payload"]["should_interrupt"] is True
    assert any(event["type"] == "user.barge_in_detected" for event in events)
    assert any(
        event["type"] == "tts.started"
        and event["payload"]["reason"] == "soft_interjection_checkin"
        for event in events
    )

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_rejected_barge_in_break_does_not_mark_tts_cancelled():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = RejectingControlClient()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response is still being spoken by FreeSWITCH.",
        wait_complete=True,
        reason="assistant_response",
    )
    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "partial",
            "text": "Actually I need to ask something else",
            "is_final": False,
            "confidence": 1.0,
        },
    )

    events = await drain_events(subscription)
    break_event = next(event for event in events if event["type"] == "tts.break_sent")
    assert break_event["payload"]["command_success"] is False
    assert break_event["payload"]["reply"] == "-ERR no such channel"
    assert any(event["type"] == "tts.error" for event in events)
    assert not any(event["type"] == "tts.cancelled" for event in events)
    assert gateway.is_agent_speaking("uuid-a") is True

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_final_user_turn_breaks_non_wait_complete_greeting_before_response():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("Hi, how can I help?")
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This is an AI voice assistant. Ask me a question after the tone.",
        wait_complete=False,
        reason="assistant_greeting",
    )

    assert gateway.is_agent_speaking("uuid-a") is True

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {"type": "final", "text": "Hello", "is_final": True, "confidence": 1.0},
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert fs_session.sent == []
    assert gateway.llm_client.requests[0]["text"] == "Hello"
    assert gateway.tts_client.requests[-1]["text"] == "Hi, how can I help?"

    events = await drain_events(subscription)
    policy_event = next(
        event
        for event in events
        if event["type"] == "policy.decision" and event["payload"]["is_final"]
    )
    assert policy_event["payload"]["action"] == "RESPOND"
    assert policy_event["payload"]["agent_is_speaking"] is True
    assert policy_event["payload"]["should_interrupt"] is True
    cancel_event = next(
        event for event in events if event["type"] == "tts.cancel_requested"
    )
    assert cancel_event["payload"]["active_speech"] is True
    assert cancel_event["payload"]["active_reason"] == "assistant_greeting"
    break_index = next(
        index for index, event in enumerate(events) if event["type"] == "tts.break_sent"
    )
    llm_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "llm.request_started"
    )
    assert break_index < llm_index

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_changed_mind_final_turn_breaks_current_tts_before_new_answer():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("A fast car is built for high speed.")
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "The sky answer is still active and must be broken before a replacement answer is queued.",
        wait_complete=True,
        reason="assistant_response",
    )

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Actually, I've changed my mind, what is fast car?",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert fs_session.sent == []
    assert (
        gateway.tts_client.requests[-1]["text"] == "A fast car is built for high speed."
    )

    events = await drain_events(subscription)
    policy_event = next(
        event
        for event in events
        if event["type"] == "policy.decision" and event["payload"]["is_final"]
    )
    assert policy_event["payload"]["action"] == "RESPOND"
    assert policy_event["payload"]["should_interrupt"] is True
    break_index = next(
        index for index, event in enumerate(events) if event["type"] == "tts.break_sent"
    )
    new_tts_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "tts.enqueue_started"
        and event["payload"].get("text") == "A fast car is built for high speed."
    )
    assert break_index < new_tts_index

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_delta_partial_barge_in_accumulates_before_policy():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response is still being spoken.",
        wait_complete=True,
        reason="assistant_response",
    )

    await gateway.handle_transcript(
        "uuid-a",
        {"type": "partial", "text": "I'm", "is_final": False, "confidence": 1.0},
    )
    await gateway.handle_transcript(
        "uuid-a",
        {"type": "partial", "text": " going", "is_final": False, "confidence": 1.0},
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert fs_session.sent == []
    assert gateway.is_agent_speaking("uuid-a") is False

    events = await drain_events(subscription)
    partial_events = [event for event in events if event["type"] == "stt.partial"]
    assert partial_events[-1]["payload"]["text"] == "I'm going"
    assert partial_events[-1]["payload"]["raw_text"] == " going"
    policy_event = [event for event in events if event["type"] == "policy.decision"][-1]
    assert policy_event["payload"]["action"] == "CANCEL_TTS_AND_LISTEN"
    assert policy_event["payload"]["should_interrupt"] is True


@pytest.mark.asyncio
async def test_local_fallback_partial_placeholder_does_not_barge_in():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    register_assistant_session(gateway)
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This response should survive VAD-only offline fallback activity.",
        wait_complete=True,
        reason="assistant_response",
    )

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "partial",
            "text": "Listening...",
            "is_final": False,
            "confidence": None,
            "provider": "local_fallback",
            "fallback": True,
            "fallback_reason": "offline speech activity detector",
        },
    )

    assert gateway.fs_control.api_commands == []
    assert fs_session.sent == []
    assert gateway.is_agent_speaking("uuid-a") is True

    events = await drain_events(subscription)
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert policy_event["payload"]["action"] == "WAIT"
    assert policy_event["payload"]["should_interrupt"] is False
    assert "vad_only_partial" in policy_event["payload"]["flags"]
    assert not any(event["type"] == "user.barge_in_detected" for event in events)
    assert not any(event["type"] == "tts.cancel_requested" for event in events)

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_final_response_breaks_active_tts_before_new_turn():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("New answer.")
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This answer is still being spoken.",
        wait_complete=True,
        reason="assistant_response",
    )

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

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-a all", "freeswitch")
    ]
    assert fs_session.sent == []
    assert gateway.llm_client.requests[0]["text"] == "Why is the sky blue?"
    assert gateway.tts_client.requests[-1]["text"] == "New answer."

    events = await drain_events(subscription)
    policy_event = next(
        event
        for event in events
        if event["type"] == "policy.decision" and event["payload"]["is_final"]
    )
    assert policy_event["payload"]["action"] == "RESPOND"
    assert policy_event["payload"]["should_interrupt"] is True
    break_index = next(
        index for index, event in enumerate(events) if event["type"] == "tts.break_sent"
    )
    llm_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "llm.request_started"
    )
    assert break_index < llm_index
