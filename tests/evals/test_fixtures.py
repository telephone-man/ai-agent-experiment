import json
from pathlib import Path


def load_fixture(name: str) -> dict:
    return json.loads(Path("docs/replay-fixtures", name).read_text())


def test_multilingual_trace_fixture_is_offline_demo():
    fixture = load_fixture("multilingual_replay_trace.json")

    assert fixture["id"] == "multilingual_replay"
    assert fixture["session_id"] == "mock-multilingual-replay"
    assert fixture["call_id"] == "mock-call-replay"
    assert "voice_events_mock=1" in fixture["demo_url"]
    assert "voice_events_mock_trace=multilingual_replay" in fixture["demo_url"]
    assert {"en", "es", "mixed"}.issubset(set(fixture["languages"]))
    assert "audio_quality_summary" in fixture["expected_capabilities"]
    assert "tool_call_progress" in fixture["expected_capabilities"]


def test_multilingual_trace_fixture_covers_repair_and_barge_in():
    fixture = load_fixture("multilingual_replay_trace.json")
    events = fixture["events"]
    event_types = {event["type"] for event in events}
    languages = {
        event["payload"]["language"]
        for event in events
        if "language" in event.get("payload", {})
    }

    assert "user.barge_in_detected" in event_types
    assert "tool.call_started" in event_types
    assert "tool.call_completed" in event_types
    assert "policy.semantic_frame" in event_types
    assert "turn.latency" in event_types
    assert {"en", "es", "mixed"}.issubset(languages)


def test_multilingual_trace_fixture_has_replay_schema():
    fixture = load_fixture("multilingual_replay_trace.json")
    events = fixture["events"]

    assert events
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert [event["at_ms"] for event in events] == sorted(
        event["at_ms"] for event in events
    )
    for event in events:
        assert set(event) == {"seq", "at_ms", "source", "type", "payload"}
        assert isinstance(event["at_ms"], int)
        assert isinstance(event["payload"], dict)


def test_audio_quality_examples_have_expected_levels():
    fixture = load_fixture("multilingual_replay_trace.json")
    examples = {item["label"]: item for item in fixture["audio_quality_examples"]}

    assert examples["good"]["expected_level"] == "good"
    assert examples["good"]["snapshot"]["inbound"]["jitterMs"] < 30
    assert examples["degraded"]["expected_level"] == "degraded"
    assert examples["degraded"]["snapshot"]["remoteInbound"]["roundTripTimeMs"] >= 400
