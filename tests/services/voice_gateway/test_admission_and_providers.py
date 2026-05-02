import asyncio
import json

import pytest

from services.voice_gateway import main as gateway_main
from services.voice_gateway.main import (
    AdmissionController,
    ProviderCircuitBreaker,
    ProviderCircuitOpenError,
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
    FakeReply,
    drain_events,
    register_assistant_session,
)


class FakeOutboundSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.context = None
        self.is_lingering = False

    async def send(self, command: str):
        self.sent.append(command)
        if command == "connect":
            return {"Unique-ID": "uuid-a"}
        return FakeReply()


class EmptyAsyncReader:
    async def readline(self) -> bytes:
        return b""


@pytest.mark.asyncio
async def test_admission_accepts_rejects_and_releases_capacity():
    gateway = VoiceGateway()
    gateway.admission = AdmissionController(max_active_sessions=1)
    accepted: list[str] = []

    async def connect_assistant_call(fs_session, fs_uuid, context):
        accepted.append(fs_uuid)
        session_id = context.get("variable_sip_h_X-Voice-Events-Session") or fs_uuid
        session = CallSession(
            session_id=session_id,
            mode=SessionMode.ASSISTANT,
            state=SessionState.LISTENING,
        )
        session.add_leg(CallLeg(leg_id="a", fs_uuid=fs_uuid, role=LegRole.CALLER))
        gateway.register_session(session, fs_session)

    gateway.connect_assistant_call = connect_assistant_call
    first = FakeFSSession()
    first.context = {
        "Unique-ID": "uuid-a",
        "Caller-Destination-Number": "7000",
        "variable_sip_h_X-Voice-Events-Session": "browser-session-a",
    }
    second = FakeFSSession()
    second.context = {
        "Unique-ID": "uuid-b",
        "Caller-Destination-Number": "7000",
        "variable_sip_h_X-Voice-Events-Session": "browser-session-b",
    }
    third = FakeFSSession()
    third.context = {
        "Unique-ID": "uuid-c",
        "Caller-Destination-Number": "7000",
        "variable_sip_h_X-Voice-Events-Session": "browser-session-c",
    }
    sub_a = await gateway.event_bus.subscribe("browser-session-a")
    sub_b = await gateway.event_bus.subscribe("browser-session-b")

    await gateway.connect_call(first)
    await gateway.connect_call(second)

    assert accepted == ["uuid-a"]
    assert first.hangups == []
    assert second.hangups == ["NORMAL_TEMPORARY_FAILURE"]
    assert [event["type"] for event in await drain_events(sub_a)] == [
        "admission.accepted"
    ]
    rejected_event = (await drain_events(sub_b))[0]
    assert rejected_event["type"] == "admission.rejected"
    assert rejected_event["payload"]["reason"] == "max_active_sessions_reached"

    await gateway.close_call("uuid-a")
    release_events = [
        event
        for event in await drain_events(sub_a)
        if event["type"] == "admission.released"
    ]
    assert release_events
    assert release_events[-1]["payload"]["active_sessions"] == 0
    assert release_events[-1]["payload"]["session_id"] == "browser-session-a"

    await gateway.connect_call(third)

    assert accepted == ["uuid-a", "uuid-c"]
    assert third.hangups == []


@pytest.mark.asyncio
async def test_provider_circuit_opens_blocks_and_resets():
    gateway = VoiceGateway()
    session = register_assistant_session(gateway)
    gateway.provider_circuits["tts"] = ProviderCircuitBreaker(
        "tts", failure_threshold=2, reset_seconds=0.01
    )
    subscription = await gateway.event_bus.subscribe(session.session_id)

    await gateway.record_provider_failure("tts", "uuid-a", RuntimeError("first"))
    assert not await drain_events(subscription)

    await gateway.record_provider_failure("tts", "uuid-a", RuntimeError("second"))
    events = await drain_events(subscription)
    opened = next(
        event for event in events if event["type"] == "provider.circuit_opened"
    )
    assert opened["payload"]["provider"] == "tts"
    assert opened["payload"]["failure_count"] == 2
    assert opened["payload"]["error"]["type"] == "RuntimeError"

    with pytest.raises(ProviderCircuitOpenError):
        await gateway.ensure_provider_available("tts", "uuid-a")
    blocked = (await drain_events(subscription))[0]
    assert blocked["type"] == "provider.circuit_blocked"
    assert blocked["payload"]["state"] == "open"

    await asyncio.sleep(0.02)
    await gateway.ensure_provider_available("tts", "uuid-a")
    closed = (await drain_events(subscription))[0]
    assert closed["type"] == "provider.circuit_closed"
    assert closed["payload"]["state"] == "closed"
    assert closed["payload"]["failure_count"] == 0


@pytest.mark.asyncio
async def test_outbound_esl_session_setup_is_bounded_and_ordered():
    fs_session = FakeOutboundSession()

    await gateway_main._prepare_outbound_esl_session(fs_session, timeout=0.1)

    assert fs_session.sent == ["connect", "myevents", "linger"]
    assert fs_session.context == {"Unique-ID": "uuid-a"}
    assert fs_session.is_lingering is True


@pytest.mark.asyncio
async def test_outbound_esl_reader_raises_on_empty_readline():
    reader = gateway_main._EOFGuardedStreamReader(EmptyAsyncReader())

    with pytest.raises(EOFError):
        await reader.readline()


@pytest.mark.asyncio
async def test_health_reports_degraded_when_esl_listener_is_not_running(monkeypatch):
    monkeypatch.delenv("VOICE_GATEWAY_ENABLE_ESL", raising=False)
    monkeypatch.setattr(gateway_main, "esl_task", None)
    monkeypatch.setattr(gateway_main, "esl_server", None)

    response = await gateway_main.health()
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["status"] == "degraded"
    assert payload["esl_listener"]["enabled"] is True
    assert payload["esl_listener"]["listening"] is False


@pytest.mark.asyncio
async def test_health_allows_explicitly_disabled_esl_listener(monkeypatch):
    monkeypatch.setenv("VOICE_GATEWAY_ENABLE_ESL", "0")
    monkeypatch.setattr(gateway_main, "esl_task", None)
    monkeypatch.setattr(gateway_main, "esl_server", None)

    response = await gateway_main.health()
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["esl_listener"]["enabled"] is False


def test_outbound_esl_host_defaults_to_gateway_local_ip(monkeypatch):
    monkeypatch.delenv("VOICE_GATEWAY_ESL_HOST", raising=False)
    monkeypatch.setenv("VOICE_GATEWAY_LOCAL_IP", "192.168.144.2")

    assert gateway_main._outbound_esl_host() == "192.168.144.2"


def test_outbound_esl_host_prefers_explicit_bind(monkeypatch):
    monkeypatch.setenv("VOICE_GATEWAY_ESL_HOST", "127.0.0.1")
    monkeypatch.setenv("VOICE_GATEWAY_LOCAL_IP", "192.168.144.2")

    assert gateway_main._outbound_esl_host() == "127.0.0.1"
