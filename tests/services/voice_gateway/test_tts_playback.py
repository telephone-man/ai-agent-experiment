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
    FakeTTSClient,
    MeasuredFakeTTSClient,
    FakeControlClient,
    drain_events,
    register_assistant_session,
)


@pytest.mark.asyncio
async def test_speak_publishes_measured_freeswitch_control_timing():
    gateway = VoiceGateway()
    gateway.tts_client = MeasuredFakeTTSClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe(session.session_id)

    timing = await gateway.speak(
        "uuid-a",
        "Measured control-plane timing is useful even while acoustic playback remains estimated.",
        wait_complete=True,
        reason="assistant_response",
    )

    events = await drain_events(subscription)
    enqueued = next(event for event in events if event["type"] == "tts.enqueued")
    assert enqueued["payload"]["tts_command_latency_ms"] == 12.5
    assert (
        enqueued["payload"]["tts_control_timing_source"]
        == "freeswitch_sendmsg_round_trip"
    )
    assert enqueued["payload"]["tts_event_lock_requested"] is True
    assert enqueued["payload"]["playback_timing_source"] == "estimated"
    assert timing["tts_command_latency_ms"] == 12.5
    assert timing["tts_event_lock_requested"] is True

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_speak_routes_tts_to_call_specific_freeswitch_host():
    gateway = VoiceGateway()
    gateway.tts_client = MeasuredFakeTTSClient()
    session = register_assistant_session(gateway)
    fs_session = type(
        "FSSession",
        (),
        {"context": {"variable_x_fs_host": "freeswitch-b"}},
    )()
    gateway.esl_sessions["uuid-a"] = fs_session

    await gateway.speak("uuid-a", "Use the call owner FreeSWITCH host.")

    assert gateway.tts_client.requests[0]["fs_host"] == "freeswitch-b"
    gateway.clear_active_speech("uuid-a")
    assert session.session_id == "session-a"


@pytest.mark.asyncio
async def test_tts_failure_event_is_sanitized():
    class FailingTTSClient:
        async def speak(self, *args, **kwargs):
            raise RuntimeError("raw upstream FreeSWITCH/TTS detail")

    gateway = VoiceGateway()
    gateway.tts_client = FailingTTSClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe(session.session_id)

    with pytest.raises(RuntimeError, match="raw upstream FreeSWITCH/TTS detail"):
        await gateway.speak("uuid-a", "Hello from the assistant.")

    events = await drain_events(subscription)
    tts_error = next(event for event in events if event["type"] == "tts.error")
    assert tts_error["payload"]["error"] == {
        "type": "tts_upstream_error",
        "message": "TTS request failed",
    }
    serialized_error_events = repr(
        [
            event
            for event in events
            if event["type"] in {"tts.error", "provider.circuit_opened"}
        ]
    )
    assert "raw upstream FreeSWITCH/TTS detail" not in serialized_error_events


@pytest.mark.asyncio
async def test_freeswitch_execute_events_measure_tts_playback_completion():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe(session.session_id)

    await gateway.speak(
        "uuid-a",
        "Measured channel events should commit this full answer.",
        wait_complete=True,
        reason="assistant_response",
        history_session=session,
    )
    await drain_events(subscription)
    event_uuid = str(gateway.tts_client.requests[0]["event_uuid"])

    started = await gateway.handle_freeswitch_execute_event(
        "uuid-a",
        {
            "Event-Name": "CHANNEL_EXECUTE",
            "Unique-ID": "uuid-a",
            "Application": "speak",
            "Application-UUID": event_uuid,
        },
        completed=False,
    )
    assert started is True
    assert gateway.is_agent_speaking("uuid-a") is True

    completed = await gateway.handle_freeswitch_execute_event(
        "uuid-a",
        {
            "Event-Name": "CHANNEL_EXECUTE_COMPLETE",
            "Unique-ID": "uuid-a",
            "Application": "speak",
            "Application-UUID": event_uuid,
            "Application-Response": "+OK",
        },
        completed=True,
    )

    assert completed is True
    assert gateway.is_agent_speaking("uuid-a") is False
    assert "uuid-a" not in gateway.active_speech
    assert event_uuid not in gateway.active_speech_by_event_uuid
    assert session.history[-1] == {
        "role": "assistant",
        "content": "Measured channel events should commit this full answer.",
    }

    events = await drain_events(subscription)
    started_event = next(
        event for event in events if event["type"] == "agent.speaking_started"
    )
    assert (
        started_event["payload"]["playback_timing_source"] == "freeswitch_channel_event"
    )
    assert (
        started_event["payload"]["playback_start_timing"]
        == "freeswitch_channel_execute"
    )
    assert started_event["payload"]["tts_event_uuid"] == event_uuid

    finished_event = next(event for event in events if event["type"] == "tts.finished")
    assert (
        finished_event["payload"]["playback_timing_source"]
        == "freeswitch_channel_event"
    )
    assert (
        finished_event["payload"]["playback_completion_timing"]
        == "freeswitch_channel_execute_complete"
    )
    assert finished_event["payload"]["history_committed"] is True
    assert finished_event["payload"]["application_response"] == "+OK"
    assert finished_event["payload"]["playback_completed_ms"] >= 0
    assert any(event["type"] == "agent.speaking_stopped" for event in events)


@pytest.mark.asyncio
async def test_freeswitch_execute_event_ignores_stale_or_unknown_tts_event_uuid():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe(session.session_id)

    await gateway.speak(
        "uuid-a", "Queued text.", wait_complete=True, reason="assistant_response"
    )
    await drain_events(subscription)

    handled = await gateway.handle_freeswitch_execute_event(
        "uuid-a",
        {
            "Event-Name": "CHANNEL_EXECUTE_COMPLETE",
            "Unique-ID": "uuid-a",
            "Application": "speak",
            "Application-UUID": "stale-event-uuid",
        },
        completed=True,
    )

    assert handled is False
    assert gateway.is_agent_speaking("uuid-a") is True
    assert not await drain_events(subscription)

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_freeswitch_completion_after_cancellation_is_ignored():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe(session.session_id)

    await gateway.speak(
        "uuid-a",
        "Cancelled before completion.",
        wait_complete=True,
        reason="assistant_response",
    )
    event_uuid = str(gateway.tts_client.requests[0]["event_uuid"])
    await gateway.break_speech("uuid-a", reason="test_cancel", publish_events=True)
    await drain_events(subscription)

    handled = await gateway.handle_freeswitch_execute_event(
        "uuid-a",
        {
            "Event-Name": "CHANNEL_EXECUTE_COMPLETE",
            "Unique-ID": "uuid-a",
            "Application": "speak",
            "Application-UUID": event_uuid,
        },
        completed=True,
    )

    assert handled is False
    assert "uuid-a" not in gateway.active_speech
    assert event_uuid not in gateway.active_speech_by_event_uuid
    assert not await drain_events(subscription)


@pytest.mark.asyncio
async def test_estimated_tts_completion_remains_fallback(monkeypatch):
    monkeypatch.setenv("VOICE_GATEWAY_TTS_START_FIXED_SECONDS", "0")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_START_REALTIME_FACTOR", "0")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_START_MIN_SECONDS", "0")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_START_MAX_SECONDS", "0")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_WPM", "60000")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_FINISH_PADDING_SECONDS", "0")
    monkeypatch.setenv("VOICE_GATEWAY_TTS_MIN_SECONDS", "0")
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe(session.session_id)

    await gateway.speak(
        "uuid-a",
        "Fallback.",
        wait_complete=True,
        reason="assistant_response",
        history_session=session,
    )
    await asyncio.sleep(0.05)

    assert "uuid-a" not in gateway.active_speech
    assert session.history[-1] == {"role": "assistant", "content": "Fallback."}

    events = await drain_events(subscription)
    started_event = next(
        event for event in events if event["type"] == "agent.speaking_started"
    )
    assert started_event["payload"]["playback_start_timing"] == "estimated_fallback"
    finished_event = next(event for event in events if event["type"] == "tts.finished")
    assert (
        finished_event["payload"]["playback_completion_timing"] == "estimated_fallback"
    )
    assert finished_event["payload"]["history_committed"] is True


@pytest.mark.asyncio
async def test_speaking_started_is_not_emitted_at_enqueue_time():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-a",
        "This speech has been accepted by FreeSWITCH but has not reached audible playback yet.",
        wait_complete=True,
        reason="assistant_response",
    )

    events = await drain_events(subscription)
    event_types = [event["type"] for event in events]
    assert "tts.enqueued" in event_types
    assert "agent.speaking_started" not in event_types

    enqueued = next(event for event in events if event["type"] == "tts.enqueued")
    assert enqueued["payload"]["estimated_start_delay_ms"] > 0
    assert gateway.is_agent_speaking("uuid-a") is True

    gateway.clear_active_speech("uuid-a")
