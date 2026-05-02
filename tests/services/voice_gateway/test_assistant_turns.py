import pytest

from services.voice_gateway.main import VoiceGateway
from services.voice_gateway.reliability import ProviderCircuitBreaker, ProviderCircuitOpenError

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
    FakeChunkedLLMClient,
    drain_events,
    register_assistant_session,
)


@pytest.mark.asyncio
async def test_assistant_turn_emits_stage_latency_events():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeLLMClient("A concise spoken reply.")
    session = CallSession(
        session_id="session-a", mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid="uuid-a", role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "speech_started",
            "text": "",
            "is_final": False,
            "item_id": "item-1",
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "partial",
            "text": "Why is the sky",
            "is_final": False,
            "confidence": 1.0,
            "item_id": "item-1",
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "endpoint",
            "text": "",
            "is_final": False,
            "item_id": "item-1",
        },
    )
    await gateway.handle_transcript(
        "uuid-a",
        {
            "type": "final",
            "text": "Why is the sky blue?",
            "is_final": True,
            "confidence": 1.0,
            "item_id": "item-1",
        },
    )

    events = await drain_events(subscription)

    event_types = [event["type"] for event in events]
    assert "stt.speech_started" in event_types
    assert "policy.evaluation_started" in event_types
    assert "policy.evaluation_finished" in event_types
    assert "policy.semantic_frame" in event_types
    assert "policy.decision" in event_types
    assert "turn.started" in event_types
    assert "llm.request_started" in event_types
    assert "llm.upstream_finished" in event_types
    assert "llm.request_finished" in event_types
    assert "tts.enqueue_started" in event_types
    assert "tts.enqueued" in event_types
    assert "turn.latency" in event_types

    def event_index(event_type, predicate=lambda event: True):
        return next(
            index
            for index, event in enumerate(events)
            if event["type"] == event_type and predicate(event)
        )

    assert event_index("stt.speech_started") < event_index("user.speech_started")
    assert event_index("stt.final") < event_index(
        "policy.evaluation_started",
        lambda event: event["payload"].get("is_final") is True,
    )
    assert event_index(
        "policy.evaluation_started",
        lambda event: event["payload"].get("is_final") is True,
    ) < event_index(
        "policy.evaluation_finished",
        lambda event: event["payload"].get("action") == "RESPOND",
    )
    assert event_index(
        "policy.evaluation_finished",
        lambda event: event["payload"].get("action") == "RESPOND",
    ) < event_index("policy.semantic_frame")
    assert event_index("policy.semantic_frame") < event_index(
        "policy.decision",
        lambda event: event["payload"].get("action") == "RESPOND",
    )
    assert event_index(
        "policy.decision",
        lambda event: event["payload"].get("action") == "RESPOND",
    ) < event_index("llm.request_started")
    assert event_index("llm.request_started") < event_index("tts.enqueue_started")
    assert event_index("tts.enqueue_started") < event_index("tts.enqueued")

    latency_event = next(event for event in events if event["type"] == "turn.latency")
    assert latency_event["payload"]["turn_id"] == "session-a:turn:1"
    assert latency_event["payload"]["llm_request_ms"] >= 0
    assert latency_event["payload"]["llm_upstream_ms"] >= 0
    assert latency_event["payload"]["tts_enqueue_ms"] >= 0
    assert latency_event["payload"]["estimated_playback_ms"] > 0
    for key in (
        "speech_to_first_partial_ms",
        "speech_to_endpoint_ms",
        "endpoint_to_final_ms",
        "speech_to_final_ms",
        "semantic_ms",
        "policy_decision_ms",
        "policy_evaluation_ms",
        "final_to_llm_request_ms",
        "policy_to_llm_request_ms",
        "first_delta_to_first_tts_enqueue_ms",
    ):
        assert latency_event["payload"][key] >= 0

    assert gateway.llm_client.requests[0]["text"] == "Why is the sky blue?"
    policy_metadata = gateway.llm_client.requests[0]["metadata"]["policy"]
    assert policy_metadata["decision"] == "RESPOND"

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_assistant_turn_speaks_streamed_sentence_chunks_in_order():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeChunkedLLMClient(["First sentence. ", "Second sentence."])
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Tell me two things.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert [request["text"] for request in gateway.tts_client.requests] == [
        "First sentence.",
        "Second sentence.",
    ]
    active = gateway.active_speech["uuid-a"]
    assert active.history_text == "First sentence. Second sentence."

    events = await drain_events(subscription)
    partial_events = [event for event in events if event["type"] == "llm.partial_text"]
    assert [event["payload"]["text"] for event in partial_events] == [
        "First sentence. ",
        "Second sentence.",
    ]
    tts_started = [
        event
        for event in events
        if event["type"] == "tts.started" and event["payload"].get("progressive")
    ]
    assert [event["payload"]["chunk_index"] for event in tts_started] == [1, 2]
    assert [event["payload"]["is_final_chunk"] for event in tts_started] == [
        False,
        False,
    ]
    final_event = next(event for event in events if event["type"] == "llm.final_text")
    assert final_event["payload"]["text"] == "First sentence. Second sentence."
    latency_event = next(event for event in events if event["type"] == "turn.latency")
    assert latency_event["payload"]["progressive"] is True
    assert latency_event["payload"]["tts_chunks"] == 2
    assert latency_event["payload"]["first_tts_enqueue_ms"] >= 0

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_streamed_llm_error_records_failure_and_sanitizes_event():
    class StreamErrorLLMClient(FakeLLMClient):
        async def stream_respond(
            self,
            session_id: str,
            text: str,
            history: list[dict[str, str]],
            *,
            metadata: dict[str, object] | None = None,
        ):
            self.requests.append(
                {
                    "session_id": session_id,
                    "text": text,
                    "history": list(history),
                    "metadata": metadata or {},
                }
            )
            yield {
                "type": "started",
                "session_id": session_id,
                "model": "fake",
                "provider": "fake",
            }
            yield {"type": "error", "message": "upstream raw provider token secret"}

    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = StreamErrorLLMClient()
    gateway.provider_circuits["llm"] = ProviderCircuitBreaker(
        "llm", failure_threshold=2, reset_seconds=60
    )
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    for index in range(2):
        with pytest.raises(RuntimeError, match="LLM stream failed"):
            await gateway.stream_assistant_response(
                session,
                "uuid-a",
                turn_id=f"turn-{index}",
                turn_started_at=0.0,
                llm_started_at=0.0,
                user_text="private user utterance",
                history_for_llm=[],
                metadata={},
            )

    events = await drain_events(subscription)
    llm_errors = [event for event in events if event["type"] == "llm.error"]
    assert llm_errors
    assert all("text" not in event["payload"] for event in llm_errors)
    assert all(
        event["payload"]["error"] == {
            "type": "llm_upstream_error",
            "message": "LLM response failed",
        }
        for event in llm_errors
    )
    assert not any("upstream raw provider token secret" in str(event) for event in events)
    assert any(event["type"] == "provider.circuit_opened" for event in events)

    with pytest.raises(ProviderCircuitOpenError):
        await gateway.stream_assistant_response(
            session,
            "uuid-a",
            turn_id="turn-blocked",
            turn_started_at=0.0,
            llm_started_at=0.0,
            user_text="another private utterance",
            history_for_llm=[],
            metadata={},
        )


@pytest.mark.asyncio
async def test_assistant_turn_surfaces_mock_tool_progress_and_speaks_status():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()

    class ToolCallingLLMClient(FakeLLMClient):
        async def stream_respond(
            self,
            session_id: str,
            text: str,
            history: list[dict[str, str]],
            *,
            metadata: dict[str, object] | None = None,
        ):
            self.requests.append(
                {
                    "session_id": session_id,
                    "text": text,
                    "history": list(history),
                    "metadata": metadata or {},
                }
            )
            yield {
                "type": "started",
                "session_id": session_id,
                "model": "mock-weather-agent",
                "provider": "local",
            }
            yield {
                "type": "tool_call_started",
                "tool_name": "mock_weather_lookup",
                "location": "Lisbon",
                "speech_text": "I'll check the mock weather feed for Lisbon.",
            }
            yield {
                "type": "tool_call_progress",
                "tool_name": "mock_weather_lookup",
                "location": "Lisbon",
                "message": "Mock weather agent is composing a structured result.",
            }
            yield {
                "type": "tool_call_completed",
                "tool_name": "mock_weather_lookup",
                "location": "Lisbon",
                "latency_ms": 850.0,
                "result": {"condition": "clear"},
            }
            yield {
                "type": "delta",
                "text": "The mock forecast for Lisbon is clear and 21 degrees.",
            }
            yield {
                "type": "completed",
                "session_id": session_id,
                "text": "The mock forecast for Lisbon is clear and 21 degrees.",
                "model": "mock-weather-agent",
                "provider": "local",
            }

    gateway.llm_client = ToolCallingLLMClient()
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "What's the weather in Lisbon?",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert [request["text"] for request in gateway.tts_client.requests] == [
        "I'll check the mock weather feed for Lisbon.",
        "The mock forecast for Lisbon is clear and 21 degrees.",
    ]

    events = await drain_events(subscription)
    event_types = [event["type"] for event in events]
    assert event_types.count("tool.call_started") == 1
    assert event_types.count("tool.call_progress") == 1
    assert event_types.count("tool.call_completed") == 1
    latency_event = next(event for event in events if event["type"] == "turn.latency")
    assert latency_event["payload"]["tool_call_count"] == 1
    assert latency_event["payload"]["tool_names"] == ["mock_weather_lookup"]
    assert latency_event["payload"]["tool_wait_ms"] == 850.0
    assert latency_event["payload"]["tts_chunks"] == 2

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_assistant_turn_enqueues_first_chunk_before_stream_completion():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()

    class InspectingLLMClient(FakeLLMClient):
        def __init__(self) -> None:
            super().__init__("First sentence. Second sentence.")
            self.first_chunk_seen_before_completion = False

        async def stream_respond(
            self,
            session_id: str,
            text: str,
            history: list[dict[str, str]],
            *,
            metadata: dict[str, object] | None = None,
        ):
            self.requests.append(
                {
                    "session_id": session_id,
                    "text": text,
                    "history": list(history),
                    "metadata": metadata or {},
                }
            )
            yield {
                "type": "started",
                "session_id": session_id,
                "model": "fake",
                "provider": "fake",
            }
            yield {"type": "delta", "text": "First sentence. "}
            self.first_chunk_seen_before_completion = bool(gateway.tts_client.requests)
            yield {"type": "delta", "text": "Second sentence."}
            yield {
                "type": "completed",
                "session_id": session_id,
                "text": self.response,
                "model": "fake",
                "provider": "fake",
            }

    llm_client = InspectingLLMClient()
    gateway.llm_client = llm_client
    session = register_assistant_session(gateway)

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Tell me two things.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert llm_client.first_chunk_seen_before_completion is True
    assert [request["text"] for request in gateway.tts_client.requests] == [
        "First sentence.",
        "Second sentence.",
    ]

    gateway.clear_active_speech("uuid-a")


@pytest.mark.asyncio
async def test_assistant_turn_flushes_final_unpunctuated_stream_chunk():
    gateway = VoiceGateway()
    gateway.tts_client = FakeTTSClient()
    gateway.llm_client = FakeChunkedLLMClient(["A short reply"])
    session = register_assistant_session(gateway)
    subscription = await gateway.event_bus.subscribe("session-a")

    await gateway.handle_assistant_transcript(
        session,
        "uuid-a",
        {
            "type": "final",
            "text": "Keep it short.",
            "is_final": True,
            "confidence": 1.0,
        },
    )

    assert [request["text"] for request in gateway.tts_client.requests] == [
        "A short reply"
    ]

    events = await drain_events(subscription)
    tts_started = [
        event
        for event in events
        if event["type"] == "tts.started" and event["payload"].get("progressive")
    ]
    assert tts_started[-1]["payload"]["is_final_chunk"] is True

    gateway.clear_active_speech("uuid-a")
