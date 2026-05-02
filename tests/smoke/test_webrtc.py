from pathlib import Path
from urllib.parse import parse_qs, urlparse
import wave

import pytest

from scripts.webrtc_smoke import (
    Capture,
    ScenarioResult,
    assistant_url,
    demo_trace_url,
    parse_args,
    scenario_artifact_summary,
    translation_demo_url,
    translation_urls,
    write_scenario_artifacts,
    write_tone_wav,
)


def query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_assistant_url_sets_headless_harness_defaults():
    url = assistant_url(
        "http://127.0.0.1:8080", audio_source="tone", auto_hangup_ms=1234
    )
    parsed = urlparse(url)
    params = query(url)

    assert parsed.path == "/call/call.html"
    assert params["auto_start"] == ["1"]
    assert params["make_call"] == ["1"]
    assert params["close_on_complete"] == ["1"]
    assert params["number_to_call"] == ["7000"]
    assert params["audio_source"] == ["tone"]
    assert params["auto_hangup_ms"] == ["1234"]


def test_translation_urls_configure_callee_and_caller():
    callee, caller = translation_urls("http://127.0.0.1:8080", audio_source="fake-file")
    callee_params = query(callee)
    caller_params = query(caller)

    assert urlparse(callee).path == "/call/call.html"
    assert callee_params["auto_answer"] == ["1"]
    assert callee_params["make_call"] == ["0"]
    assert callee_params["aor"] == ["sip:bob@voice.local"]
    assert callee_params["audio_source"] == ["silence"]
    assert callee_params["remote_audio_muted"] == ["1"]

    assert urlparse(caller).path == "/call/call.html"
    assert caller_params["number_to_call"] == ["7100"]
    assert caller_params["translate_peer"] == ["sip:bob@voice.local"]
    assert caller_params["source_language"] == ["en"]
    assert caller_params["target_language"] == ["fr"]
    assert caller_params["audio_source"] == ["default"]


def test_translation_demo_url_sets_parent_passthrough_defaults():
    url = translation_demo_url(
        "http://127.0.0.1:8080",
        audio_source="fake-file",
        source_language="es",
        target_language="en",
        translate_peer="sip:alice@voice.local",
    )
    params = query(url)

    assert urlparse(url).path == "/translation-demo/translation_demo.html"
    assert params["ws_url"] == ["ws://127.0.0.1:5066"]
    assert params["audio_source"] == ["default"]
    assert params["source_language"] == ["es"]
    assert params["target_language"] == ["en"]
    assert params["translate_peer"] == ["sip:alice@voice.local"]
    assert params["enable_media_debug"] == ["1"]


def test_translation_demo_embeds_directional_start_controls():
    html = Path("web_client/translation-demo/translation_demo.html").read_text()
    js = Path("web_client/translation-demo/translation_demo.js").read_text()

    assert 'id="caller-frame"' in html
    assert 'id="bob-frame"' in html
    assert 'id="start-demo-en-fr"' in html
    assert 'id="start-demo-fr-en"' in html
    assert "Start English to French" in html
    assert "Start French to English" in html
    assert js.count('buildClientUrl("../call/call.html"') == 2
    assert 'auto_start: "0"' in js
    assert 'make_call: "1"' in js
    assert 'number_to_call: "7100"' in js
    assert 'dial_number: "7100"' in js
    assert 'contact_number: "7100"' in js
    assert 'auto_answer: "1"' in js
    assert 'make_call: "0"' in js
    assert '"ws_url"' in js
    assert '"voice_events_url"' in js
    assert '"audio_source"' in js
    assert 'audio_source: "silence"' in js
    assert 'remote_audio_muted: "0"' in js
    assert 'queryFlag("mute_receiver_audio")' in js
    assert '"enable_media_debug"' in js
    assert '"source_language"' in js
    assert '"target_language"' in js
    assert '"translate_peer"' in js
    assert "webrtc.first_inbound_rtp" in js
    assert "const translatePeer = ()" in js
    assert "const routeFromButton = (button)" in js
    assert "aor: translatePeer()" in js
    assert "translate_peer: translatePeer()" in js


def test_home_page_links_to_reorganized_demo_pages():
    html = Path("web_client/index.html").read_text()

    assert 'href="call/call.html"' in html
    assert 'href="translation-demo/translation_demo.html"' in html
    assert "Each demo has a Back button" in html
    assert not Path("web_client/call.html").exists()
    assert not Path("web_client/call2.html").exists()
    assert not Path("web_client/translation_demo.html").exists()
    assert not Path("web_client/call/peer.html").exists()


def test_demo_pages_expose_back_cleanup_controls():
    call_html = Path("web_client/call/call.html").read_text()
    call_js = Path("web_client/call/call.js").read_text()
    translation_html = Path("web_client/translation-demo/translation_demo.html").read_text()
    translation_js = Path("web_client/translation-demo/translation_demo.js").read_text()

    assert 'id="page-back-button"' in call_html
    assert 'id="demo-back"' in translation_html
    assert "terminateOngoingActivity" in call_js
    assert "stop: () => terminateOngoingActivity" in call_js
    assert "terminateDemo" in translation_js
    assert 'embedded: "1"' in translation_js


def test_demo_trace_url_enables_mock_replay():
    url = demo_trace_url("http://127.0.0.1:8080", trace="multilingual_replay")
    params = query(url)

    assert urlparse(url).path == "/call/call.html"
    assert params["auto_start"] == ["1"]
    assert params["voice_events_mock"] == ["1"]
    assert params["voice_events_mock_trace"] == ["multilingual_replay"]
    assert params["enable_media_debug"] == ["1"]


def test_write_tone_wav_creates_browser_fake_mic_audio(tmp_path):
    path = tmp_path / "tone.wav"

    write_tone_wav(path, seconds=1.0, sample_rate=8000)

    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 8000
        assert wav.getnframes() == 8000


def test_browser_mic_constraints_enable_noise_controls():
    call_js = Path("web_client/call/call.js").read_text()

    assert "echoCancellation: true" in call_js
    assert "noiseSuppression: true" in call_js
    assert "autoGainControl: true" in call_js
    assert "REAL_MIC_AUDIO_CONSTRAINTS" in call_js


def test_browser_supports_silent_receiver_media():
    call_js = Path("web_client/call/call.js").read_text()

    assert 'const AUDIO_SOURCE_SILENCE = "silence"' in call_js
    assert "createSilenceStream" in call_js
    assert '"remote-audio-muted": "remoteAudioMuted"' in call_js
    assert '{ query: "remote_audio_muted", id: "remote-audio-muted" }' in call_js


def test_browser_mock_replay_uses_fixture_fetch():
    call_js = Path("web_client/call/call.js").read_text()

    assert "/replay-fixtures/multilingual_replay_trace.json" in call_js
    assert "docs/replay-fixtures/multilingual_replay_trace.json" in call_js
    assert "fetchVoiceEventMockTrace" in call_js
    assert "VOICE_EVENT_MOCK_TRACES" not in call_js
    assert '[3000, "tool", "tool.call_started"' not in call_js


def test_browser_records_local_call_and_webrtc_latency_markers():
    call_js = Path("web_client/call/call.js").read_text()

    for event_type in (
        "browser.call_started",
        "browser.sip_invite_sent",
        "browser.call_established",
        "webrtc.get_user_media_started",
        "webrtc.get_user_media_ready",
        "webrtc.ice_connected",
        "webrtc.first_outbound_rtp",
        "webrtc.first_inbound_rtp",
    ):
        assert event_type in call_js


def test_browser_callback_url_is_harness_only():
    call_js = Path("web_client/call/call.js").read_text()

    assert "harnessCallbackEnabled" in call_js
    assert 'getQueryFlag("harness")' in call_js
    assert 'getQueryFlag("smoke_test")' in call_js
    assert "callbackUrl: harnessCallbackEnabled() ? getQueryValue(\"callback_url\")" in call_js


def test_webrtc_smoke_script_matches_documented_python_invocation():
    smoke_script = Path("scripts/webrtc_smoke.py").read_text()
    replay_docs = Path("docs/replay-fixtures/README.md").read_text()

    assert not smoke_script.startswith("#!")
    assert "uv run --with playwright python scripts/webrtc_smoke.py" in replay_docs


def test_browser_exposes_normalized_voice_events_to_parent_pages():
    call_js = Path("web_client/call/call.js").read_text()

    assert "Company:voice-event" in call_js
    assert "detail: normalized" in call_js


def test_parse_args_accepts_artifacts_dir():
    args = parse_args(
        ["--scenario", "assistant", "--artifacts-dir", "/tmp/webrtc-smoke"]
    )

    assert args.scenario == "assistant"
    assert args.artifacts_dir == "/tmp/webrtc-smoke"


def test_scenario_artifact_summary_includes_events_console_and_log_patterns():
    capture = Capture(
        events=[{"page": "assistant", "detail": {"event": "registered"}}],
        console=["first", "second"],
        errors=["page error"],
    )
    result = ScenarioResult(
        "assistant", False, ["assistant:registered"], ["queued speak"], "boom"
    )

    summary = scenario_artifact_summary(result, capture)

    assert summary["name"] == "assistant"
    assert summary["ok"] is False
    assert summary["event_count"] == 1
    assert summary["events"] == ["assistant:registered"]
    assert summary["console_tail"] == ["first", "second"]
    assert summary["missing_log_patterns"] == ["queued speak"]
    assert summary["errors"] == ["page error"]
    assert summary["error"] == "boom"


@pytest.mark.asyncio
async def test_write_scenario_artifacts_writes_summary_events_console_and_screenshots(
    tmp_path,
):
    class FakePage:
        async def screenshot(self, *, path: str, full_page: bool) -> None:
            assert full_page is True
            Path(path).write_bytes(b"png")

    capture = Capture(
        events=[{"page": "assistant", "detail": {"event": "registered"}}],
        console=["line"],
        pages={"assistant": FakePage()},
    )
    result = ScenarioResult("assistant", True, ["assistant:registered"], [])

    summary = await write_scenario_artifacts(tmp_path, result, capture)

    assert summary is not None
    summary_path = tmp_path / "assistant-summary.json"
    events_path = tmp_path / "assistant-events.json"
    console_path = tmp_path / "assistant-console.txt"
    screenshot_path = tmp_path / "assistant-assistant.png"
    assert summary_path.exists()
    assert events_path.exists()
    assert console_path.read_text(encoding="utf-8") == "line\n"
    assert screenshot_path.read_bytes() == b"png"
    assert summary["artifacts"]["summary"] == str(summary_path)
    assert summary["artifacts"]["events"] == str(events_path)
    assert summary["artifacts"]["console"] == str(console_path)
    assert summary["artifacts"]["screenshots"]["assistant"] == str(screenshot_path)
