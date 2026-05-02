import pytest

from services.voice_gateway.events import VOICE_EVENT_TYPES, VoiceEventBus


@pytest.mark.asyncio
async def test_voice_event_serializes_to_public_schema():
    bus = VoiceEventBus()

    event = await bus.publish(
        session_id="session-1",
        call_id="call-1",
        source="stt",
        type="stt.partial",
        payload={"text": "hello"},
    )

    assert event.to_dict() == {
        "seq": 1,
        "ts": event.ts,
        "session_id": "session-1",
        "call_id": "call-1",
        "source": "stt",
        "type": "stt.partial",
        "payload": {"text": "hello"},
    }
    assert event.ts.endswith("Z")


@pytest.mark.asyncio
async def test_voice_event_bus_publishes_ordered_events_per_session():
    bus = VoiceEventBus()
    subscription = await bus.subscribe("session-1")

    await bus.publish(
        session_id="session-1", call_id="call-1", source="esl", type="call.created"
    )
    await bus.publish(
        session_id="session-1", call_id="call-1", source="stt", type="stt.final"
    )

    first = await subscription.queue.get()
    second = await subscription.queue.get()

    assert [first.seq, second.seq] == [1, 2]
    assert [first.type, second.type] == ["call.created", "stt.final"]


@pytest.mark.asyncio
async def test_voice_event_bus_filters_by_session_or_call_id():
    bus = VoiceEventBus()
    session_subscription = await bus.subscribe("session-1")
    call_subscription = await bus.subscribe("call-2")

    await bus.publish(
        session_id="session-1", call_id="call-1", source="esl", type="call.created"
    )
    await bus.publish(
        session_id="session-2", call_id="call-2", source="stt", type="stt.final"
    )

    assert (await session_subscription.queue.get()).session_id == "session-1"
    assert (await call_subscription.queue.get()).call_id == "call-2"
    assert session_subscription.queue.empty()
    assert call_subscription.queue.empty()


@pytest.mark.asyncio
async def test_voice_event_bus_publish_without_subscribers_does_not_fail():
    bus = VoiceEventBus()

    event = await bus.publish(
        session_id="session-1",
        call_id="call-1",
        source="tts",
        type="tts.started",
        payload={"text": "hello"},
    )

    assert event.seq == 1
    assert event.payload["text"] == "hello"


def test_voice_events_websocket_streams_filtered_public_json(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    from services.voice_gateway import main as gateway_main

    monkeypatch.setenv("VOICE_GATEWAY_ENABLE_ESL", "0")
    monkeypatch.setattr(gateway_main.gateway, "event_bus", VoiceEventBus())

    async def publish_events() -> None:
        await gateway_main.gateway.event_bus.publish(
            session_id="other-session",
            call_id="other-call",
            source="stt",
            type="stt.final",
            payload={"text": "ignored"},
        )
        await gateway_main.gateway.event_bus.publish(
            session_id="session-1",
            call_id="call-1",
            source="stt",
            type="stt.final",
            payload={"text": "hello"},
        )

    with testclient.TestClient(gateway_main.app) as client:
        with client.websocket_connect("/events/session-1") as websocket:
            client.portal.call(publish_events)
            event = websocket.receive_json()

    assert event["session_id"] == "session-1"
    assert event["call_id"] == "call-1"
    assert event["source"] == "stt"
    assert event["type"] == "stt.final"
    assert event["payload"] == {"text": "hello"}


def test_voice_events_websocket_rejects_empty_stream_by_default(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    from starlette.websockets import WebSocketDisconnect

    from services.voice_gateway import main as gateway_main

    monkeypatch.setenv("VOICE_GATEWAY_ENABLE_ESL", "0")
    monkeypatch.delenv("VOICE_GATEWAY_ALLOW_WILDCARD_EVENTS", raising=False)
    monkeypatch.setattr(gateway_main.gateway, "event_bus", VoiceEventBus())

    with testclient.TestClient(gateway_main.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/events"):
                pass


def test_voice_events_websocket_allows_wildcard_only_with_debug_flag(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    from services.voice_gateway import main as gateway_main

    monkeypatch.setenv("VOICE_GATEWAY_ENABLE_ESL", "0")
    monkeypatch.setenv("VOICE_GATEWAY_ALLOW_WILDCARD_EVENTS", "1")
    monkeypatch.setattr(gateway_main.gateway, "event_bus", VoiceEventBus())

    async def publish_event() -> None:
        await gateway_main.gateway.event_bus.publish(
            session_id="session-1",
            call_id="call-1",
            source="system",
            type="system.debug",
            payload={"message": "debug"},
        )

    with testclient.TestClient(gateway_main.app) as client:
        with client.websocket_connect("/events") as websocket:
            client.portal.call(publish_event)
            event = websocket.receive_json()

    assert event["session_id"] == "session-1"
    assert event["payload"] == {"message": "debug"}


@pytest.mark.asyncio
async def test_voice_event_bus_rejects_unknown_event_type():
    bus = VoiceEventBus()

    with pytest.raises(ValueError):
        await bus.publish(
            session_id="session-1",
            call_id="call-1",
            source="system",
            type="unknown.event",
        )


def test_registered_voice_event_types_include_delivery_resume_event():
    assert "delivery.auto_resume" in VOICE_EVENT_TYPES
