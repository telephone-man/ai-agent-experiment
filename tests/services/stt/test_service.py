import asyncio
import json

import pytest

fastapi = pytest.importorskip("fastapi")
if not hasattr(fastapi, "FastAPI") or not getattr(fastapi, "__file__", None):
    pytest.skip("real fastapi is required", allow_module_level=True)

TestClient = pytest.importorskip("fastapi.testclient").TestClient

from services.stt_service import main as stt_main  # noqa: E402
from services.stt_service.main import (  # noqa: E402
    AudioActivityStats,
    LocalTurnBuffer,
    OpenAICompletionOrderer,
    _confidence_from_logprobs,
    _error_event,
    _openai_final_event,
    _openai_lifecycle_event,
    _openai_partial_event,
    _openai_transcription_session_update,
    _stt_suppressed_event,
    _upstream_failure_fallback_enabled,
)


def test_stt_upstream_failure_fallback_is_opt_in(monkeypatch):
    monkeypatch.delenv("STT_FALLBACK_ON_UPSTREAM_ERROR", raising=False)
    assert _upstream_failure_fallback_enabled() is False

    monkeypatch.setenv("STT_FALLBACK_ON_UPSTREAM_ERROR", "1")
    assert _upstream_failure_fallback_enabled() is True


def test_stt_error_event_sanitizes_exception_details():
    event = _error_event(OSError("dns failed"))

    assert event["type"] == "error"
    assert event["detail"]["type"] == "upstream_connection_error"
    assert event["detail"]["message"] == "OpenAI transcription failed"
    assert event["detail"]["error_type"] == "OSError"
    assert "dns failed" not in str(event)


def test_openai_realtime_transcription_session_update_shape():
    event = _openai_transcription_session_update("gpt-4o-transcribe", "en")

    assert event["type"] == "transcription_session.update"
    assert "type" not in event["session"]
    assert "audio" not in event["session"]
    assert event["session"]["input_audio_format"] == "pcm16"
    assert event["session"]["input_audio_transcription"] == {
        "model": "gpt-4o-transcribe",
        "language": "en",
    }
    assert event["session"]["input_audio_noise_reduction"] == {"type": "near_field"}
    assert event["session"]["turn_detection"]["type"] == "server_vad"


def test_openai_realtime_transcription_session_update_uses_vad_env(monkeypatch):
    monkeypatch.delenv("OPENAI_STT_TURN_DETECTION_TYPE", raising=False)
    monkeypatch.setenv("OPENAI_STT_NOISE_REDUCTION_TYPE", "far_field")
    monkeypatch.setenv("OPENAI_STT_VAD_THRESHOLD", "0.7")
    monkeypatch.setenv("OPENAI_STT_PREFIX_PADDING_MS", "120")
    monkeypatch.setenv("OPENAI_STT_SILENCE_DURATION_MS", "650")

    event = _openai_transcription_session_update("gpt-4o-transcribe", "en")

    assert event["session"]["input_audio_noise_reduction"] == {"type": "far_field"}
    assert event["session"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.7,
        "prefix_padding_ms": 120,
        "silence_duration_ms": 650,
    }


def test_openai_realtime_transcription_session_update_uses_semantic_vad(monkeypatch):
    monkeypatch.setenv("OPENAI_STT_TURN_DETECTION_TYPE", "semantic_vad")
    monkeypatch.setenv("OPENAI_STT_SEMANTIC_EAGERNESS", "low")

    event = _openai_transcription_session_update("gpt-4o-transcribe", "en")

    assert event["session"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "low",
    }


def test_openai_realtime_transcription_session_update_can_disable_vad(monkeypatch):
    monkeypatch.setenv("OPENAI_STT_TURN_DETECTION_TYPE", "none")

    event = _openai_transcription_session_update("gpt-4o-transcribe", "en")

    assert event["session"]["turn_detection"] is None


def test_openai_realtime_transcription_session_update_uses_prompt_and_logprobs(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_STT_PROMPT", "Invoice IDs sound like INV1042.")
    monkeypatch.setenv("OPENAI_STT_INCLUDE_LOGPROBS", "1")

    event = _openai_transcription_session_update("gpt-4o-transcribe", "en")

    assert (
        event["session"]["input_audio_transcription"]["prompt"]
        == "Invoice IDs sound like INV1042."
    )
    assert event["session"]["include"] == ["item.input_audio_transcription.logprobs"]


def test_openai_stt_lifecycle_event_maps_metadata():
    event = _openai_lifecycle_event(
        "speech_started",
        {
            "event_id": "event-1",
            "item_id": "item-1",
            "audio_start_ms": 120,
        },
        language="en",
        vad_mode="server_vad",
    )

    assert event["type"] == "speech_started"
    assert event["provider"] == "openai"
    assert event["language"] == "en"
    assert event["vad_mode"] == "server_vad"
    assert event["item_id"] == "item-1"
    assert event["audio_start_ms"] == 120


def test_openai_stt_partial_event_maps_delta_metadata():
    event = _openai_partial_event(
        {
            "event_id": "event-2",
            "item_id": "item-1",
            "content_index": 0,
            "delta": "Hel",
        },
        language="en",
        vad_mode="semantic_vad",
    )

    assert event["type"] == "partial"
    assert event["text"] == "Hel"
    assert event["provider"] == "openai"
    assert event["vad_mode"] == "semantic_vad"
    assert event["item_id"] == "item-1"


def test_audio_activity_stats_suppresses_short_or_low_energy_finals(monkeypatch):
    monkeypatch.setenv("STT_SUPPRESS_LOW_AUDIO_FINALS", "1")
    stats = AudioActivityStats(sample_rate=16000)

    stats.add(b"\x00\x00" * 160)

    assert stats.suppression_reason("Okay") == "speech_too_short"


def test_audio_activity_stats_allows_sufficient_speech(monkeypatch):
    monkeypatch.setenv("STT_SUPPRESS_LOW_AUDIO_FINALS", "1")
    stats = AudioActivityStats(sample_rate=16000)
    loud_sample = (1200).to_bytes(2, "little", signed=True)

    stats.add(loud_sample * 4000)

    assert stats.suppression_reason("Blue.") is None


def test_stt_suppressed_event_shape():
    event = _stt_suppressed_event(
        "Okay",
        "speech_too_short",
        {"audio_ms": 30.0, "speech_ms": 20.0, "avg_rms": 200.0, "peak_rms": 300.0},
        "en",
    )

    assert event["type"] == "suppressed"
    assert event["text"] == "Okay"
    assert event["reason"] == "speech_too_short"
    assert event["provider"] == "openai"


def test_openai_final_event_does_not_invent_confidence():
    event = _openai_final_event(
        "Thank you",
        {"audio_ms": 220.0, "speech_ms": 180.0, "avg_rms": 900.0, "peak_rms": 2400.0},
        "en",
    )

    assert event["type"] == "final"
    assert event["confidence"] is None
    assert event["confidence_source"] == "not_provided"
    assert event["speech_ms"] == 180.0
    assert event["peak_rms"] == 2400.0


def test_openai_final_event_can_use_logprobs_for_confidence():
    assert _confidence_from_logprobs([{"logprob": -0.2}, {"logprob": -0.4}]) == 0.7408

    event = _openai_final_event(
        "Thank you",
        {"audio_ms": 220.0, "speech_ms": 180.0, "avg_rms": 900.0, "peak_rms": 2400.0},
        "en",
        metadata={"item_id": "item-1", "previous_item_id": "item-0"},
        logprobs=[
            {"token": "Thank", "logprob": -0.2},
            {"token": " you", "logprob": -0.4},
        ],
    )

    assert event["confidence"] == 0.7408
    assert event["confidence_source"] == "openai_logprobs"
    assert event["item_id"] == "item-1"
    assert event["previous_item_id"] == "item-0"
    assert event["logprobs_token_count"] == 2


def test_openai_completion_orderer_flushes_committed_order():
    orderer = OpenAICompletionOrderer()
    orderer.commit("item-1")
    orderer.commit("item-2")

    assert orderer.add_completed({"item_id": "item-2", "text": "blue."}) == []
    ready = orderer.add_completed({"item_id": "item-1", "text": "Why is the sky"})

    assert [event["item_id"] for event in ready] == ["item-1", "item-2"]


def test_local_fallback_final_transcript_is_explicit_placeholder():
    buffer = LocalTurnBuffer(session_id="s1", language="en")
    buffer.speech_bytes = 1

    event = buffer.commit()

    assert event is not None
    assert event["type"] == "final"
    assert (
        event["text"]
        == "Speech detected by offline STT fallback; no transcript available."
    )
    assert event["confidence"] == 0.0
    assert event["provider"] == "local_fallback"
    assert event["fallback"] is True


def test_local_fallback_can_be_configured_for_offline_smoke(monkeypatch):
    monkeypatch.setenv("LOCAL_STT_FINAL_TEXT", "Bonjour je voudrais parler.")
    monkeypatch.setenv("LOCAL_STT_CONFIDENCE", "0.92")
    buffer = LocalTurnBuffer(session_id="s1", language="fr")
    buffer.speech_bytes = 1

    event = buffer.commit()

    assert event is not None
    assert event["text"] == "Bonjour je voudrais parler."
    assert event["confidence"] == 0.92
    assert event["language"] == "fr"


def test_local_fallback_emits_one_partial_per_speech_segment():
    buffer = LocalTurnBuffer(session_id="s1", language="en")
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 160

    first = buffer.add(loud_frame)
    second = buffer.add(loud_frame)
    final = buffer.commit()
    third = buffer.add(loud_frame)

    assert first is not None
    assert first["type"] == "partial"
    assert second is None
    assert final is not None
    assert final["type"] == "final"
    assert third is not None
    assert third["type"] == "partial"


def test_stt_offline_fallback_preferred_even_with_api_key(monkeypatch):
    monkeypatch.setenv("AI_OFFLINE_FALLBACK", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def local_transcribe(websocket, session_id, language):
        await websocket.send_json(
            {"provider": "local", "session_id": session_id, "language": language}
        )

    async def openai_transcribe(*_args, **_kwargs):
        raise AssertionError(
            "OpenAI STT should not be used when offline fallback is enabled"
        )

    monkeypatch.setattr(stt_main, "_local_transcribe", local_transcribe)
    monkeypatch.setattr(stt_main, "_openai_transcribe", openai_transcribe)

    client = TestClient(stt_main.app)
    with client.websocket_connect("/v1/audio/s1?language=en") as websocket:
        assert websocket.receive_json() == {
            "provider": "local",
            "session_id": "s1",
            "language": "en",
        }


@pytest.mark.asyncio
async def test_openai_transcribe_treats_client_disconnect_as_normal(monkeypatch):
    websockets = pytest.importorskip("websockets")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    upstream_messages = []
    downstream_messages = []

    class ClosedClientWebSocket:
        async def receive(self):
            raise RuntimeError(
                'Cannot call "receive" once a disconnect message has been received.'
            )

        async def send_json(self, event):
            downstream_messages.append(event)

    class FakeUpstream:
        async def send(self, message):
            upstream_messages.append(json.loads(message))

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(60)
            raise StopAsyncIteration

    class FakeConnection:
        async def __aenter__(self):
            return FakeUpstream()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: FakeConnection())

    await stt_main._openai_transcribe(ClosedClientWebSocket(), "s1", "en")

    assert upstream_messages[0]["type"] == "transcription_session.update"
    assert downstream_messages == []
