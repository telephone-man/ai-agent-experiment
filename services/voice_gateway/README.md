# Voice Gateway Responsibilities

`services/voice_gateway/main.py` is intentionally left as one orchestration file
for this local proof of concept. This pass documents the ownership boundaries
without splitting behavior, so the existing call flow and tests remain stable.

## Current Ownership Areas

- ESL and FastAPI adapters: outbound ESL call entry, media WebSocket ingress,
  health checks, and structured event WebSocket endpoints.
- Session registry: `CallSession` and `CallLeg` lookup by FreeSWITCH UUID,
  per-leg audio queues, ESL session references, and session cleanup.
- Media/STT loop: `uuid_audio_stream` startup, audio queue backpressure, STT
  WebSocket streaming, transcript accumulation, and STT lifecycle events.
- Policy and turn handling: semantic interpretation, deterministic policy
  decisions, turn holds, pause handling, barge-in decisions, and blocked action
  responses.
- LLM streaming: request metadata, progressive response chunking, tool progress
  events, cancellation of superseded turns, and latency accounting.
- TTS delivery and cancellation: speech enqueue, FreeSWITCH channel-event
  playback tracking with estimated fallback timers, active speech history,
  `uuid_break`, and interrupted delivery context.
- Local reliability gates: active-session admission control plus provider
  circuit events for STT, LLM, translation, and TTS dependencies.
- Structured events: normalized `VoiceEventBus` publication for call state,
  transcript, policy, tool, latency, TTS, and system diagnostics.
- Provider clients: STT, LLM, translation, TTS, and FreeSWITCH control clients
  live in `clients.py` so timeout/concurrency policy is outside core turn
  orchestration.

## Future Extraction Order

If this became team-owned production code, split by behavior after preserving
the current event contract and tests:

1. Move provider timeout/concurrency concerns into a client package.
2. Extract TTS delivery/cancellation into a speech delivery service.
3. Extract transcript-to-policy turn handling into a turn coordinator.
4. Extract ESL/FastAPI adapters so transport code only calls the coordinator.
5. Keep `VoiceEventBus` as the cross-cutting observability boundary.

The order keeps the riskiest voice behavior, especially barge-in, turn holds,
and delivery context, covered by existing tests while shrinking the gateway.
