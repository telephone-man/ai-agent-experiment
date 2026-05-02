# Tests

The suite is grouped by the subsystem or workflow it protects. Add new tests near
the behavior they cover instead of adding more flat files at the test root.

## Layout

- `config/`: static checks for deploy and runtime configuration files.
- `services/llm/`: LLM service contracts, fallbacks, prompts, and endpoints.
- `services/stt/`: STT service contracts, event mapping, and local fallback behavior.
- `services/tts/`: TTS command construction and HTTP-facing behavior.
- `services/voice_gateway/`: voice gateway models, clients, events, policy, turn
  handling, translation, playback, and interruption behavior.
- `evals/`: eval runner contracts and browser replay fixture coverage.
- `smoke/`: browser/WebRTC smoke-test harness behavior.
- `support/`: reusable test fakes and helpers. Files here are imported explicitly
  by tests and should not contain pytest test functions.

## Voice Gateway Tests

The voice gateway tests are split by behavior:

- `test_translation_calls.py`: translation call setup and peer preparation.
- `test_media_stream.py`: FreeSWITCH audio stream startup.
- `test_tts_playback.py`: TTS enqueue, playback completion, and cancellation state.
- `test_barge_in.py`: partial speech, interruptions, and false-positive handling.
- `test_stt_events.py`: STT event handling and observability-only events.
- `test_delivery_context.py`: interrupted assistant replies and next-turn context.
- `test_assistant_turns.py`: assistant response streaming, chunking, and tool progress.
- `test_turn_hold.py`: incomplete user turns and held-turn continuation.
- `test_risk_policy_integration.py`: policy confirmation and risky-action blocking.

Shared voice gateway fakes live in `tests/support/voice_gateway.py`.

## Running Tests

Run the full suite:

```sh
uv run pytest
```

Run a focused subset:

```sh
uv run pytest tests/services/voice_gateway
uv run pytest tests/config
```

Run one file or one test:

```sh
uv run pytest tests/services/voice_gateway/test_barge_in.py
uv run pytest tests/services/voice_gateway/test_barge_in.py::test_partial_barge_in_breaks_active_tts_playback
```

## Markers

A pytest marker is a tag on a test, for example `@pytest.mark.slow`, that lets you
select or exclude tests from the command line. This repo currently uses
`@pytest.mark.asyncio` for async tests but does not define custom grouping markers.
Prefer directories and clear filenames first. Add custom markers only when there
is a real command-line workflow, such as consistently skipping slow external
smoke tests.

## Comments

Do not comment every test. Prefer descriptive test names and small helper
functions. Add comments only when a test encodes a protocol quirk, regression, or
non-obvious timing/state assumption.
