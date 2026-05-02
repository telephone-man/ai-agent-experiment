# Latency Budget

Voice assistant latency is judged by turn-taking, not by one end-to-end number.
The important question is whether the caller hears progress at a natural moment
after they stop speaking.

This page is a local tuning guide for the checked-in Docker Compose proof of
concept. The authoritative event contract lives in
[Voice Events](voice-events.md). This page explains which parts of the
conversation those events help measure and which parts they do not prove.

## Scope

The local stack exposes enough structured metadata to diagnose obvious latency
bottlenecks during demos and smoke tests:

- browser setup markers in exported debug bundles.
- `voice_gateway` call, media, STT, policy, LLM, tool, TTS, and turn events.
- aggregate counters such as `turn.latency` and `translation.latency`.

Raw audio stays on the SIP/WebRTC/RTP/media WebSocket path. The event stream is
for local diagnosis and demo observability; it is not production tracing,
metrics retention, audit logging, or SLO alerting.

## Target Budget

These targets are useful ranges for local tuning. They are not production SLOs,
and WAN links or real providers can move the numbers substantially.

| Segment | Local target | Primary evidence | Notes |
| --- | ---: | --- | --- |
| Browser capture and WebRTC readiness | 50-100 ms | Browser debug markers and WebRTC stats | Browser-local evidence only. |
| SIP/RTP/FreeSWITCH/gateway media path | 50-150 ms | Call events, media events, and `media.stream_start_ack` | Local Docker should stay low; external networks add jitter. |
| Speech endpointing | 300-700 ms | `user.speech_stopped`, `stt.endpoint`, `turn.latency` STT counters | Silence windows dominate this segment. |
| STT finalization | 100-500 ms | `stt.endpoint`, `stt.final`, `endpoint_to_final_ms` | Live mode is usually provider and network bound. |
| Semantic and policy decision | <20 ms | `policy.evaluation_*`, `semantic_ms`, `policy_decision_ms` | Local deterministic policy should be cheap. |
| First LLM text | 300-900 ms | `llm.request_started`, `llm.upstream_finished`, `first_llm_delta_ms` | Short prompts and streamable answers matter. |
| TTS enqueue and playback start | 100-500 ms | `tts.enqueue_started`, `tts.enqueued`, `agent.speaking_started` | FreeSWITCH can accept speech before playback completes. |
| Total perceived gap | 900-2500 ms | `turn.latency` plus event sequence review | Endpointing, STT, and first LLM text usually dominate. |

## Event Timeline

A normal assistant turn is easiest to inspect as a sequence:

1. Call setup becomes visible through `admission.accepted`, `call.created`,
   `call.answered`, `call.connected`, and media stream events.
2. Browser setup timing is available from exported browser markers such as
   `browser.call_started`, `webrtc.ice_connected`,
   `webrtc.first_outbound_rtp`, and `webrtc.first_inbound_rtp`.
3. User speech moves through `stt.speech_started`, `user.speech_started`,
   `stt.partial`, `user.speech_stopped`, `stt.endpoint`, and `stt.final`.
4. The local decision layer emits `policy.evaluation_started`,
   `policy.evaluation_finished`, `policy.semantic_frame`, and
   `policy.decision`.
5. The response path emits `turn.started`, `llm.request_started`,
   `llm.upstream_finished`, `llm.request_finished`, optional tool events,
   `tts.enqueue_started`, `tts.enqueued`, `agent.speaking_started`, and
   `tts.finished`.
6. `turn.latency` summarizes the assistant turn counters that the gateway can
   compute from owned timestamps.

The translation flow uses the same structured event channel but publishes
`translation.latency` instead of a per-leg assistant `turn.latency`.

## Reading The Counters

Prefer explicit `*_ms` fields when the gateway owns both timestamps. Use event
timestamp deltas for local sequence review, and use browser markers only inside
the same exported browser trace.

| Question | Useful signals |
| --- | --- |
| Is STT reacting quickly? | `speech_to_first_partial_ms`, `speech_to_endpoint_ms`, `endpoint_to_final_ms`, `speech_to_final_ms` |
| Is the local policy layer adding delay? | `semantic_ms`, `policy_decision_ms`, `policy_evaluation_ms` |
| Is the gateway slow to hand off to the LLM? | `final_to_llm_request_ms`, `policy_to_llm_request_ms` |
| Is the model slow to produce useful text? | `first_llm_delta_ms`, `llm_upstream_ms`, `llm.request_finished.latency_ms` |
| Is progressive speech starting quickly? | `first_delta_to_first_tts_enqueue_ms`, `tts_enqueue_ms`, `tts.enqueued.tts_command_latency_ms` |
| Is FreeSWITCH starting playback promptly? | `agent.speaking_started.playback_started_ms`, `tts.finished.playback_completed_ms`, `playback_timing_source` |
| Is barge-in cancellation responsive? | `user.barge_in_detected`, `tts.cancel_requested`, `tts.break_sent.command_latency_ms`, `tts.cancelled` |
| Is translation latency acceptable? | `translation.latency.translation_request_ms`, `tts_enqueue_ms`, `final_to_tts_enqueued_ms` |

## Interpreting Common Bottlenecks

- High endpointing time usually means silence or semantic-turn settings are too
  conservative.
- High STT finalization time points at the STT provider, provider network path,
  or gateway/provider client backpressure.
- High policy time should be investigated locally because the deterministic
  policy path is expected to be small.
- High first-token or upstream LLM time usually belongs to prompt size, model
  choice, provider latency, or tool-call planning.
- High first-TTS-enqueue time points at chunking strategy, progressive TTS
  handoff, or TTS capacity.
- High playback-start time is FreeSWITCH application timing evidence, not proof
  that the remote browser heard audio at that instant.

## Known Blind Spots

The current observability is intentionally local and bounded:

- There is no synchronized distributed trace across browser, Kamailio,
  FreeSWITCH, `voice_gateway`, and provider services.
- Browser markers and gateway events are not correlated with production-grade
  trace IDs or clock synchronization.
- STT-visible speech start is not the same as the caller's acoustic mouth
  start.
- FreeSWITCH `speak` start and completion events do not prove browser-side
  acoustic receipt or completion.
- Provider-side timing is not fully isolated from gateway queuing, streaming
  backpressure, or client work.
- Structured events can include sensitive transcripts, routing identifiers,
  tool payloads, and generated text. Production use would need authorization,
  redaction, retention rules, and aggregation outside this repo.

## Engineering Levers

- Reduce silence windows only while watching false endpointing and interruption
  behavior.
- Keep spoken responses short and optimized for streaming.
- Start TTS from stable response chunks rather than waiting for a full response
  when the content is safe to stream.
- Treat barge-in as a normal path with its own latency budget.
- Separate speed from correctness: a fast unsafe answer is still a failed voice
  product.

## Related Docs

- [Voice Events](voice-events.md) describes the structured event contract.
- [Architecture](architecture.md) shows the media path and observability
  boundary.
