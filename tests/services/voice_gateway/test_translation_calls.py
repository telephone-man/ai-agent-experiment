import asyncio

import pytest

from services.voice_gateway.main import VoiceGateway

from services.voice_gateway.models import (
    CallLeg,
    CallSession,
    LegRole,
    SessionMode,
)

from tests.support.voice_gateway import (
    FakeFSSession,
    FakeReply,
    FakeTTSClient,
    FakeTranslationClient,
    FakeControlClient,
    drain_events,
    register_translation_session,
)


@pytest.mark.asyncio
async def test_translation_originate_uses_control_socket(monkeypatch):
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    gateway.start_stt_task = lambda _: None

    async def prepare_peer(_fs_session, _peer_uuid, *, fs_host=None):
        return None

    monkeypatch.setattr(gateway, "_prepare_translation_peer", prepare_peer)
    fs_session = FakeFSSession()
    context = {
        "variable_sip_from_uri": "sip:demo-1001@voice.local",
        "variable_x_fs_host": "freeswitch",
        "variable_sip_h_X-Translate-Peer": "sip:bob@voice.local",
        "variable_sip_h_X-Source-Language": "fr",
        "variable_sip_h_X-Target-Language": "en",
    }

    await gateway.connect_translation_call(fs_session, "uuid-a", context)

    assert fs_session.answered is True
    assert not any(command.startswith("bgapi ") for command in fs_session.sent)
    [(audio_stream_command, audio_stream_host)] = gateway.fs_control.api_commands
    assert audio_stream_host == "freeswitch"
    assert audio_stream_command.startswith("uuid_audio_stream uuid-a start")
    [(command, fs_host)] = gateway.fs_control.bgapi_commands
    assert fs_host == "freeswitch"
    assert command.startswith("originate {")
    assert "sip_h_X-type=to_registered" in command
    assert "sofia/external/sip:bob@voice.local;fs_path=sip:kamailio:5060" in command
    assert gateway.tts_client.requests == []


@pytest.mark.asyncio
async def test_translation_originate_rejection_publishes_error_and_hangup():
    class RejectingBgapiControl(FakeControlClient):
        async def bgapi(self, command: str, *, fs_host: str | None = None):
            self.bgapi_commands.append((command, fs_host))
            return FakeReply("-ERR no route")

    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = RejectingBgapiControl()
    gateway.start_stt_task = lambda _: None
    fs_session = FakeFSSession()
    subscription = await gateway.event_bus.subscribe("uuid-a")
    context = {
        "variable_sip_from_uri": "sip:demo-1001@voice.local",
        "variable_x_fs_host": "freeswitch",
        "variable_sip_h_X-Translate-Peer": "sip:bob@voice.local",
    }

    await gateway.connect_translation_call(fs_session, "uuid-a", context)

    assert fs_session.hangups == ["NORMAL_CLEARING"]
    assert gateway.fs_control.bgapi_commands
    events = await drain_events(subscription)
    error_event = next(event for event in events if event["type"] == "system.error")
    assert error_event["payload"]["message"] == "translation originate rejected"
    assert error_event["payload"]["peer_uuid"]
    assert "-ERR no route" in error_event["payload"]["reply"]


@pytest.mark.asyncio
async def test_translation_invalid_peer_aor_is_rejected_before_originate():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.fs_control = FakeControlClient()
    gateway.start_stt_task = lambda _: None
    fs_session = FakeFSSession()
    subscription = await gateway.event_bus.subscribe("uuid-a")
    context = {
        "variable_sip_from_uri": "sip:demo-1001@voice.local",
        "variable_x_fs_host": "freeswitch",
        "variable_sip_h_X-Translate-Peer": "sip:bob@voice.local;fs_path=sip:evil",
    }

    await gateway.connect_translation_call(fs_session, "uuid-a", context)

    assert fs_session.hangups == ["NORMAL_CLEARING"]
    assert gateway.fs_control.bgapi_commands == []
    events = await drain_events(subscription)
    error_event = next(event for event in events if event["type"] == "system.error")
    assert error_event["payload"] == {
        "message": "invalid translation peer",
        "error": "invalid_translation_peer",
    }


@pytest.mark.asyncio
async def test_prepare_translation_peer_does_not_start_media_when_uuid_missing(
    monkeypatch,
):
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.sessions["session-a"] = CallSession(
        session_id="session-a",
        mode=SessionMode.TRANSLATION,
    )
    gateway.sessions["session-a"].add_leg(
        CallLeg(
            leg_id="a",
            fs_uuid="uuid-a",
            role=LegRole.CALLER,
            peer_leg_id="b",
        )
    )
    gateway.sessions["session-a"].add_leg(
        CallLeg(
            leg_id="b",
            fs_uuid="uuid-b",
            role=LegRole.PEER,
            peer_leg_id="a",
        )
    )
    gateway.sessions_by_uuid["uuid-a"] = "session-a"
    gateway.sessions_by_uuid["uuid-b"] = "session-a"
    fs_session = FakeFSSession()
    gateway.esl_sessions["uuid-a"] = fs_session
    subscription = await gateway.event_bus.subscribe("session-a")
    starts: list[str] = []
    stt_starts: list[str] = []

    async def uuid_missing(_fs_uuid, *, fs_host=None):
        return False

    async def start_audio_stream(_fs_session, fs_uuid, leg_id="a"):
        starts.append(f"{fs_uuid}:{leg_id}")
        return True

    monkeypatch.setattr(gateway, "uuid_exists", uuid_missing)
    monkeypatch.setattr(gateway, "start_audio_stream", start_audio_stream)
    monkeypatch.setattr(gateway, "start_stt_task", stt_starts.append)
    monkeypatch.setenv("TRANSLATION_PEER_STREAM_ATTEMPTS", "1")
    monkeypatch.setenv("TRANSLATION_PEER_STREAM_RETRY_SECONDS", "0")

    await gateway._prepare_translation_peer(fs_session, "uuid-b")

    assert starts == []
    assert stt_starts == []
    assert fs_session.hangups == ["NORMAL_CLEARING"]
    assert gateway.tts_client.requests == []
    events = await drain_events(subscription)
    error_event = next(event for event in events if event["type"] == "system.error")
    assert error_event["payload"]["message"] == "translation peer did not become available"
    assert error_event["payload"]["peer_uuid"] == "uuid-b"


@pytest.mark.asyncio
async def test_translation_turn_emits_request_finished_and_latency():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.translation_client = FakeTranslationClient(
        "Hello from the translated side."
    )
    register_translation_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_started",
            "text": "",
            "is_final": False,
            "item_id": "translation-item-1",
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "final",
            "text": "Bonjour je voudrais parler",
            "is_final": True,
            "confidence": 1.0,
            "item_id": "translation-item-1",
        },
    )

    events = await drain_events(subscription)
    event_types = [event["type"] for event in events]
    assert "llm.upstream_finished" in event_types
    assert "llm.request_finished" in event_types
    assert "translation.latency" in event_types
    latency_event = next(
        event for event in events if event["type"] == "translation.latency"
    )
    assert latency_event["payload"]["translation_request_ms"] >= 0
    assert latency_event["payload"]["tts_enqueue_ms"] >= 0
    assert latency_event["payload"]["final_to_tts_enqueued_ms"] >= 0
    assert latency_event["payload"]["source_leg_id"] == "a"
    assert latency_event["payload"]["target_leg_id"] == "b"
    assert latency_event["payload"]["translation_model"] == "fake-translation"
    assert latency_event["payload"]["translation_provider"] == "fake"
    assert gateway.tts_client.requests[-1]["fs_uuid"] == "uuid-b"

    gateway.clear_active_speech("uuid-b")


@pytest.mark.asyncio
async def test_translation_failure_event_is_sanitized():
    class FailingTranslationClient:
        async def translate(
            self,
            session_id: str,
            text: str,
            *,
            source_language: str,
            target_language: str,
        ) -> str:
            raise RuntimeError("upstream leaked raw translation detail")

    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.translation_client = FailingTranslationClient()
    register_translation_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    with pytest.raises(RuntimeError, match="upstream leaked raw translation detail"):
        await gateway.handle_transcript(
            "uuid-a",
            {
                "type": "final",
                "text": "Bonjour secret appointment details",
                "is_final": True,
                "confidence": 1.0,
                "item_id": "translation-item-1",
            },
        )

    events = await drain_events(subscription)
    llm_error = next(event for event in events if event["type"] == "llm.error")
    assert llm_error["payload"] == {
        "turn_id": "session-a:turn:1",
        "provider": "translation",
        "source_language": "fr",
        "target_language": "en",
        "error": {
            "type": "translation_upstream_error",
            "message": "Translation failed",
        },
    }
    error_events = [
        event
        for event in events
        if event["type"] in {"llm.error", "provider.circuit_opened"}
    ]
    serialized_error_events = repr(error_events)
    assert "upstream leaked raw translation detail" not in serialized_error_events
    assert "Bonjour secret appointment details" not in serialized_error_events


@pytest.mark.asyncio
async def test_translation_partial_barge_in_breaks_peer_audio_without_translation():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.translation_client = FakeTranslationClient("Should not be used.")
    gateway.fs_control = FakeControlClient()
    register_translation_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-b",
        "This translated playback is still active.",
        wait_complete=True,
        reason="translation_response",
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "partial",
            "text": "Attends je dois corriger le rendez-vous",
            "is_final": False,
            "confidence": 1.0,
        },
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-b all", "freeswitch")
    ]
    assert gateway.translation_client.requests == []
    assert [request["text"] for request in gateway.tts_client.requests] == [
        "This translated playback is still active."
    ]

    events = await drain_events(subscription)
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert policy_event["payload"]["action"] == "partial"
    assert policy_event["payload"]["should_interrupt"] is True
    assert policy_event["payload"]["target_leg_is_speaking"] is True
    barge_event = next(
        event for event in events if event["type"] == "user.barge_in_detected"
    )
    assert barge_event["payload"]["source_leg_id"] == "a"
    assert barge_event["payload"]["target_leg_id"] == "b"
    assert any(event["type"] == "tts.break_sent" for event in events)
    assert any(event["type"] == "tts.cancelled" for event in events)


@pytest.mark.asyncio
async def test_translation_zero_confidence_final_is_ignored():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.translation_client = FakeTranslationClient("Should not be used.")
    register_translation_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "final",
            "text": "Speech detected by offline STT fallback; no transcript available.",
            "is_final": True,
            "confidence": 0.0,
        },
    )

    assert gateway.translation_client.requests == []
    assert gateway.tts_client.requests == []

    events = await drain_events(subscription)
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert policy_event["payload"]["action"] == "ignore"
    assert policy_event["payload"]["should_interrupt"] is False


@pytest.mark.asyncio
async def test_translation_close_hangs_up_parked_peer_leg():
    gateway = VoiceGateway()
    gateway.fs_control = FakeControlClient()
    fs_session = FakeFSSession()
    register_translation_session(gateway)
    gateway.esl_sessions["uuid-a"] = fs_session
    gateway.audio_queues["uuid-a"] = asyncio.Queue(maxsize=1)
    gateway.audio_queues["uuid-b"] = asyncio.Queue(maxsize=1)

    await gateway.close_call("uuid-a")

    assert gateway.fs_control.api_commands[0] == (
        "uuid_kill uuid-b NORMAL_CLEARING",
        "freeswitch",
    )
    assert "session-a" not in gateway.sessions
    assert "uuid-a" not in gateway.sessions_by_uuid
    assert "uuid-b" not in gateway.sessions_by_uuid


@pytest.mark.asyncio
async def test_translation_final_event_type_breaks_peer_audio_before_replacement():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.translation_client = FakeTranslationClient("Corrected translation.")
    gateway.fs_control = FakeControlClient()
    register_translation_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.speak(
        "uuid-b",
        "This earlier translated playback should be interrupted.",
        wait_complete=True,
        reason="translation_response",
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "final",
            "text": "Bonjour je dois corriger",
            "confidence": 1.0,
        },
    )

    assert gateway.fs_control.api_commands == [
        ("uuid_break uuid-b all", "freeswitch")
    ]
    assert gateway.translation_client.requests == [
        {
            "session_id": "session-a",
            "text": "Bonjour je dois corriger",
            "source_language": "fr",
            "target_language": "en",
        }
    ]
    assert gateway.tts_client.requests[-1]["text"] == "Corrected translation."

    events = await drain_events(subscription)
    policy_event = next(event for event in events if event["type"] == "policy.decision")
    assert policy_event["payload"]["action"] == "user_turn"
    assert policy_event["payload"]["is_final"] is True
    assert policy_event["payload"]["should_interrupt"] is True
    break_index = next(
        index for index, event in enumerate(events) if event["type"] == "tts.break_sent"
    )
    request_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "llm.request_started"
    )
    assert break_index < request_index

    gateway.clear_active_speech("uuid-b")
