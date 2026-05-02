import pytest

from services.voice_gateway.main import VoiceGateway

from tests.support.voice_gateway import (
    FakeFSSession,
    FakeControlClient,
    drain_events,
    register_assistant_session,
)


@pytest.mark.asyncio
async def test_start_audio_stream_emits_control_latency_events():
    gateway = VoiceGateway()
    gateway.fs_control = FakeControlClient()
    register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    started = await gateway.start_audio_stream(FakeFSSession(), "uuid-a", leg_id="a")

    assert started is True
    events = await drain_events(subscription)
    requested = next(
        event for event in events if event["type"] == "media.stream_start_requested"
    )
    ack = next(event for event in events if event["type"] == "media.stream_start_ack")
    assert requested["payload"]["transport"] == "mod_audio_stream"
    assert requested["payload"]["leg_id"] == "a"
    assert ack["payload"]["command_success"] is True
    assert ack["payload"]["command_latency_ms"] >= 0
