# Browser Replay Fixtures

These fixtures are static inputs for browser mock replay. They avoid real STT,
LLM, TTS, SIP, and WebRTC dependencies so the browser demo can be reviewed from
static data.

They are not eval suites. Deterministic eval runners and scenario matrices live
under the top-level `evals/` directory; generated reports are written to `/tmp`
by default.

## Multilingual Trace Replay

Open:

```text
/call/call.html?auto_start=1&voice_events_mock=1&voice_events_mock_trace=multilingual_replay&enable_media_debug=1
```

The trace covers an English weather question that triggers the local mock
weather tool, a Spanish repair request, barge-in cancellation, mixed-language
response metadata, latency metrics, and audio-quality summary examples. It is
the canonical fixture for the browser replay; each event includes `seq`,
`at_ms`, `source`, `type`, and `payload`, with `session_id` and `call_id` stored
at the fixture top level.

Maintenance note: this is a static browser replay fixture, not proof that the
peer-to-peer translation chat path has been tested. When translation coverage
matures, update, rename, or replace this trace so it reflects the current demo
surface and keep the browser, smoke-script, and test references in sync.
It proves the browser can render a deterministic structured-event replay; it
does not prove live SIP/WebRTC media, STT, translation, or TTS quality.

## Related Evals

The actual eval assets are separate from these replay fixtures:

- `evals/voice_policy/` contains voice-policy scenario fixtures and a report
  runner.
- `evals/multilingual/` contains deterministic multilingual contract coverage.

## WebRTC Smoke Artifacts

The Playwright smoke runner can capture JSON summaries, structured event names,
browser console tails, log-pattern results, and screenshots:

```bash
AI_OFFLINE_FALLBACK=1 \
LOCAL_STT_FINAL_TEXT="Hello Bob, can you hear me?" \
LOCAL_STT_CONFIDENCE=1 \
docker compose up --build -d
uv run --with playwright python scripts/webrtc_smoke.py --scenario both --audio-source fake-file --start-mode auto --artifacts-dir /tmp/voice-ai-webrtc-smoke/latest
```

Treat those artifacts as local demo evidence for the current machine and stack
state, not as a production monitoring substitute.
