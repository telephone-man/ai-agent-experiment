import pytest

from services.common.freeswitch_commands import (
    build_audio_stream_start_command,
    build_originate_translation_leg_command,
    build_uuid_break_command,
    validate_translation_peer_aor,
)
from services.tts_service.freeswitch import (
    TTSConfig,
    build_sendmsg_payload,
    tts_variants,
)


def test_tts_variants_use_freeswitch_piper_defaults():
    assert tts_variants("Hello", TTSConfig()) == ["piper|en|Hello"]


def test_tts_config_supports_language_specific_voice(monkeypatch):
    monkeypatch.setenv("FREESWITCH_TTS_ENGINE_FR", "piper")
    monkeypatch.setenv("FREESWITCH_TTS_VOICE_FR", "fr")

    assert tts_variants("Bonjour", TTSConfig.from_env("fr")) == ["piper|fr|Bonjour"]


def test_tts_config_falls_back_to_base_language_for_regional_tag(monkeypatch):
    monkeypatch.delenv("FREESWITCH_TTS_ENGINE_FR_FR", raising=False)
    monkeypatch.delenv("FREESWITCH_TTS_VOICE_FR_FR", raising=False)
    monkeypatch.setenv("FREESWITCH_TTS_ENGINE_FR", "piper")
    monkeypatch.setenv("FREESWITCH_TTS_VOICE_FR", "fr")

    assert tts_variants("Bonjour", TTSConfig.from_env("fr-FR")) == [
        "piper|fr|Bonjour"
    ]


def test_tts_config_supports_flite_fallback(monkeypatch):
    monkeypatch.setenv("FREESWITCH_TTS_ALT_ENGINE", "flite")
    monkeypatch.setenv("FREESWITCH_TTS_ALT_VOICE", "kal")

    assert tts_variants("Hello", TTSConfig.from_env("en")) == [
        "piper|en|Hello",
        "flite|kal|Hello",
    ]


def test_tts_text_is_cleaned_for_freeswitch_separator():
    assert tts_variants("Hello | there\nfriend", TTSConfig()) == [
        "piper|en|Hello there friend"
    ]


def test_sendmsg_payload_includes_event_uuid_for_channel_event_correlation():
    payload = build_sendmsg_payload(
        "uuid-123",
        "speak",
        "piper|en|Hello",
        lock=True,
        event_uuid="tts-event-123",
    )

    assert "event-lock: true" in payload
    assert "Event-UUID: tts-event-123" in payload


def test_audio_stream_command_matches_mod_audio_stream_shape():
    command = build_audio_stream_start_command(
        "uuid-123",
        "ws://voice-gateway:8000/media/uuid-123",
        metadata='{"session_id":"uuid-123"}',
    )

    assert command == (
        "uuid_audio_stream uuid-123 start "
        'ws://voice-gateway:8000/media/uuid-123 mono 16000 {"session_id":"uuid-123"}'
    )


def test_audio_stream_command_normalizes_legacy_sample_rate_alias():
    command = build_audio_stream_start_command(
        "uuid-123",
        "ws://voice-gateway:8000/media/uuid-123",
        sample_rate="16k",
    )

    assert (
        command
        == "uuid_audio_stream uuid-123 start ws://voice-gateway:8000/media/uuid-123 mono 16000"
    )


def test_uuid_break_stops_queued_speech():
    assert build_uuid_break_command("uuid-123") == "uuid_break uuid-123 all"


def test_translation_originate_routes_registered_peer_through_kamailio():
    command = build_originate_translation_leg_command(
        peer_aor="sip:bob@voice.local",
        peer_uuid="uuid-b",
        fs_path="sip:kamailio:5060",
    )

    assert command.startswith("originate {")
    assert "origination_uuid=uuid-b" in command
    assert "sip_h_X-type=to_registered" in command
    assert "sofia/external/sip:bob@voice.local;fs_path=sip:kamailio:5060" in command
    assert command.endswith(" &park()")


@pytest.mark.parametrize(
    "peer_aor",
    [
        "sip:bob@voice.local\napi status",
        "sip:bob@voice.local;fs_path=sip:evil",
        "sip:bob@voice.local,hangup",
        "sip:bob @voice.local",
        "sip:bob@voice.local|api status",
        "{origination_uuid=evil}sip:bob@voice.local",
        "not-a-sip-aor",
        "sip:bob@voice.local:70000",
    ],
)
def test_translation_peer_validation_rejects_unsafe_or_invalid_aors(peer_aor):
    with pytest.raises(ValueError):
        validate_translation_peer_aor(peer_aor)


def test_translation_peer_validation_normalizes_registered_aor():
    assert validate_translation_peer_aor("bob@voice.local") == "sip:bob@voice.local"


def test_speak_endpoint_echoes_event_uuid_in_response(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    if not hasattr(fastapi, "HTTPException") or not getattr(fastapi, "__file__", None):
        pytest.skip("real fastapi is required")
    from fastapi.testclient import TestClient

    from services.tts_service.main import app

    monkeypatch.setenv("TTS_DRY_RUN", "1")
    client = TestClient(app)

    response = client.post(
        "/v1/speak",
        json={
            "fs_uuid": "uuid-123",
            "text": "Hello",
            "event_uuid": "tts-event-123",
            "event_lock": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["event_uuid"] == "tts-event-123"
    assert body["command_latency_ms"] is None
    assert body["playback_started_ms"] is None
    assert body["playback_completed_ms"] is None
    assert body["event_lock_requested"] is True


def test_speak_request_schema_uses_event_lock_name():
    from services.tts_service.main import SpeakRequest

    schema = SpeakRequest.model_json_schema()

    assert "event_lock" in schema["properties"]
    assert "wait_complete" not in schema["properties"]
    assert (
        SpeakRequest.model_validate(
            {"fs_uuid": "uuid-123", "text": "Hello", "wait_complete": True}
        ).event_lock
        is True
    )


def test_tts_health_rejects_missing_esl_config(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    if not hasattr(fastapi, "HTTPException") or not getattr(fastapi, "__file__", None):
        pytest.skip("real fastapi is required")
    from fastapi.testclient import TestClient

    from services.tts_service.main import app

    monkeypatch.delenv("FREESWITCH_HOST", raising=False)
    monkeypatch.delenv("FREESWITCH_ESL_PORT", raising=False)
    monkeypatch.delenv("FREESWITCH_ESL_PASSWORD", raising=False)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "FREESWITCH_HOST is required" in body["errors"]
    assert "FREESWITCH_ESL_PORT is required" in body["errors"]
    assert "FREESWITCH_ESL_PASSWORD is required" in body["errors"]


def test_tts_health_accepts_required_esl_config(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    if not hasattr(fastapi, "HTTPException") or not getattr(fastapi, "__file__", None):
        pytest.skip("real fastapi is required")
    from fastapi.testclient import TestClient

    from services.tts_service.main import app

    monkeypatch.setenv("FREESWITCH_HOST", "freeswitch")
    monkeypatch.setenv("FREESWITCH_ESL_PORT", "8021")
    monkeypatch.setenv("FREESWITCH_ESL_PASSWORD", "test-secret")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_speak_endpoint_reports_control_connection_failure(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    if not hasattr(fastapi, "HTTPException") or not getattr(fastapi, "__file__", None):
        pytest.skip("real fastapi is required")
    from fastapi.testclient import TestClient

    from services.tts_service import main as tts_main

    async def failing_inbound(_fs_host):
        raise OSError("connection refused")

    monkeypatch.delenv("TTS_DRY_RUN", raising=False)
    monkeypatch.setattr(tts_main, "_inbound", failing_inbound)
    client = TestClient(tts_main.app)

    response = client.post("/v1/speak", json={"fs_uuid": "uuid-123", "text": "Hello"})

    assert response.status_code == 502
    assert response.json()["detail"] == "FreeSWITCH control connection failed"


def test_speak_endpoint_reports_rejected_speak_command(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    if not hasattr(fastapi, "HTTPException") or not getattr(fastapi, "__file__", None):
        pytest.skip("real fastapi is required")
    from fastapi.testclient import TestClient

    from services.tts_service import main as tts_main

    class RejectingControl:
        async def send(self, _payload):
            return {"Reply-Text": "-ERR no such channel"}

    async def rejecting_inbound(_fs_host):
        return RejectingControl()

    monkeypatch.delenv("TTS_DRY_RUN", raising=False)
    monkeypatch.setattr(tts_main, "_inbound", rejecting_inbound)
    client = TestClient(tts_main.app)

    response = client.post("/v1/speak", json={"fs_uuid": "uuid-123", "text": "Hello"})

    assert response.status_code == 502
    assert response.json()["detail"] == "FreeSWITCH speak command failed"


def test_speak_endpoint_uses_requested_freeswitch_host(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    if not hasattr(fastapi, "HTTPException") or not getattr(fastapi, "__file__", None):
        pytest.skip("real fastapi is required")
    from fastapi.testclient import TestClient

    from services.tts_service import main as tts_main

    seen_hosts = []

    class AcceptingControl:
        async def send(self, _payload):
            return {"Reply-Text": "+OK"}

    async def accepting_inbound(fs_host):
        seen_hosts.append(fs_host)
        return AcceptingControl()

    monkeypatch.delenv("TTS_DRY_RUN", raising=False)
    monkeypatch.setattr(tts_main, "_inbound", accepting_inbound)
    client = TestClient(tts_main.app)

    response = client.post(
        "/v1/speak",
        json={"fs_uuid": "uuid-123", "text": "Hello", "fs_host": "freeswitch-b"},
    )

    assert response.status_code == 200
    assert seen_hosts == ["freeswitch-b"]
