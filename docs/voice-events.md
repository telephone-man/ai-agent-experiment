# Voice Events

`voice_gateway` publishes local structured metadata events through
`VoiceEventBus`. Browser clients subscribe over:

```text
ws://127.0.0.1:8000/events?session_id={session_or_call_id}
ws://127.0.0.1:8000/events/{session_or_call_id}
```

Raw audio is not published through this channel. Audio stays on the WebRTC/RTP
and FreeSWITCH media WebSocket path.

An empty `/events` subscription is rejected by default because it would receive
all sessions. Wildcard subscriptions are only available for explicit local
debugging with `VOICE_GATEWAY_ALLOW_WILDCARD_EVENTS=1`.

## Envelope

Each event has this shape:

```json
{
  "seq": 1,
  "ts": "2026-05-01T12:00:00.000Z",
  "session_id": "session-or-primary-call-id",
  "call_id": "freeswitch-leg-id",
  "source": "stt",
  "type": "stt.final",
  "payload": {}
}
```

`seq` is scoped to the session. `source` names the subsystem that emitted the
event. `type` is a stable event name used by the browser panel and tests.

## Sensitive Payload Fields

The local demo intentionally exposes full payloads for review. Production
handling would treat these fields as sensitive:

- transcript and prompt text: `text`, `raw_text`, `accumulated_text`,
  `spoken_text`, `undelivered_text`, and generated assistant responses.
- identity/routing data: caller AORs, peer AORs, destination numbers, call IDs,
  session IDs, and SIP-derived headers.
- tool payloads: tool arguments, tool results, and any provider error body that
  may include user input.
- delivery context: interrupted assistant text and previous-turn recovery
  metadata.

## Safer Metric Fields

These fields are generally safer to retain after redaction because they describe
system behavior instead of conversation content:

- latency counters such as `llm_request_ms`, `first_tts_enqueue_ms`,
  `tts_enqueue_ms`, `tool_wait_ms`, and `estimated_playback_ms`.
- counts and sizes such as `text_chars`, `text_words`, `history_size`,
  `tts_chunks`, and `tool_call_count`.
- categorical policy data such as `decision`, `action`, `speech_act`,
  `should_interrupt`, and blocked action names.
- media-quality measurements such as jitter, loss, RTT, packet counts, and
  connection state.

## TTS Timing Fields

TTS events include local control-plane and playback timing metadata:

- `tts_event_uuid`: correlation ID sent as FreeSWITCH `Event-UUID` and observed
  as `Application-UUID` on channel execution events.
- `event_lock_requested` or `tts_event_lock_requested`: FreeSWITCH event-lock
  command sequencing was requested; this is not an acoustic playback wait.
- `tts_command_latency_ms`: FreeSWITCH `sendmsg` round-trip time when returned
  by `tts_service`.
- `playback_timing_source`: `freeswitch_channel_event` when
  `CHANNEL_EXECUTE`/`CHANNEL_EXECUTE_COMPLETE` correlation is observed, or
  `estimated`/fallback metadata when it is not.
- `playback_started_ms` and `playback_completed_ms`: elapsed time from gateway
  enqueue to observed or estimated playback start/completion.

FreeSWITCH channel completion means the `speak` application finished on that
channel. It is better evidence than a timer, but it is still not browser-side
acoustic receipt.

## Turn Latency Fields

`turn.latency` now carries explicit per-step counters in addition to event
timestamps:

- `speech_to_first_partial_ms`, `speech_to_endpoint_ms`,
  `endpoint_to_final_ms`, and `speech_to_final_ms` describe STT turn timing.
- `semantic_ms`, `policy_decision_ms`, and `policy_evaluation_ms` describe the
  local semantic/policy gate.
- `final_to_llm_request_ms` and `policy_to_llm_request_ms` describe gateway
  handoff time after final STT and policy.
- `llm_upstream_ms`, `first_llm_delta_ms`, and
  `first_delta_to_first_tts_enqueue_ms` describe LLM streaming and first speech
  chunk handoff.

The LLM upstream value is gateway-observed. In the current streaming
implementation, synchronous progressive TTS enqueue work can still delay later
stream reads, so this is better than final response timing but not a fully
isolated provider-side model metric.

The translation flow emits `llm.upstream_finished`, `llm.request_finished`, and
`translation.latency` for the same review path.

## Browser And Media Setup Timing

The browser observability panel records local-only setup markers into exported
debug bundles:

- `browser.call_started`, `browser.sip_invite_sent`, and
  `browser.call_established`.
- `webrtc.get_user_media_started`, `webrtc.get_user_media_ready`,
  `webrtc.ice_connected`, `webrtc.first_outbound_rtp`, and
  `webrtc.first_inbound_rtp`.

These browser markers are generated in the page, not by `voice_gateway`, so they
are useful for one-browser local review but are not distributed traces.

`voice_gateway` also emits `media.stream_start_requested` and
`media.stream_start_ack` around the FreeSWITCH `uuid_audio_stream` command. The
ack event includes `command_latency_ms` and `command_success`.

## Reliability Signals

The gateway publishes lightweight local reliability events:

- `admission.accepted`, `admission.rejected`, and `admission.released` expose
  the active-session gate.
- `provider.circuit_opened`, `provider.circuit_blocked`, and
  `provider.circuit_closed` expose local circuit state for STT, LLM,
  translation, and TTS provider calls.

These are local hardening signals for the demo stack. They are not a full
production admission-control, tracing, dashboard, or SLO-alerting system.

## Production Redaction Direction

A production event sink should support at least two modes:

- `full`: local/debug mode with transcript text, tool details, and delivery
  context visible to trusted developers.
- `redacted`: default production mode that strips or hashes sensitive text and
  identifiers while preserving timings, categories, counts, and correlation IDs.

The browser observability panel should be gated behind operator authorization in
production, and exported debug bundles should inherit the same redaction mode as
the event stream.
