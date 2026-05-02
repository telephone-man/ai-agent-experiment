# Voice Policy Layer

Use the root [README](../README.md) as the primary reviewer path. This page is
the narrower reference for how the assistant call on extension `7000` separates
STT endpointing, semantic interpretation, and deterministic local routing.

The policy layer does not replace STT endpointing at the audio boundary. STT
decides whether audio has produced a partial or final transcript. The policy
layer treats a final transcript as a candidate conversation boundary, then uses
semantic meaning, current assistant state, and action risk to decide what should
happen next.

It decides whether to wait, respond, suppress the turn, hold and merge a
continuation, clarify an underspecified question, pause for a soft interruption,
cancel TTS, confirm before a risky action, block unsafe action execution,
escalate to safe fallback handling, or end the call.

## Responsibility Split

1. STT:
   - transcript text.
   - partial/final status.
   - confidence and timing metadata.

2. Semantic interpreter:
   - whether the utterance is addressed to the agent.
   - speech act, intent, and conversational completeness.
   - corrections, authorisation, risky action mentions, and slots.
   - clarification needs for incomplete questions.

3. Local policy:
   - final `PolicyDecision`.
   - safety invariants and confirmation requirements.
   - blocked actions and response instructions.
   - barge-in, soft interruption, and held-turn routing.

The normal final-transcript flow is:

```text
STT final transcript
  -> SemanticFrame
  -> evaluate_policy(...)
  -> PolicyDecision
  -> voice_gateway action router
  -> optional LLM/TTS/tool path
```

Partial transcripts use a shorter path. They build a `PolicyInput` with
`is_partial=true` and call `evaluate_policy(...)` without the full semantic
interpreter. This keeps lightweight turn-taking decisions cheap while the caller
or STT provider is still producing text.

## Repository Pieces

- `voice_policy/schema.py` defines the strict `SemanticFrame`, `PolicyInput`,
  `PolicyDecision`, and `PolicyAction` models.
- `voice_policy/heuristic_semantic_interpreter.py` provides deterministic
  offline semantics for tests and local demos.
- `voice_policy/policy.py` owns the final deterministic policy decision.
- `voice_policy/trace_adapter.py` converts exported voice-event traces into
  policy inputs for replay and analysis.
- `services/voice_gateway/main.py` routes policy decisions into live call
  behavior.
- `evals/voice_policy/scenarios/` contains YAML judgement fixtures.

The LLM does not own call-control or safety decisions. It should not decide to
hang up, cancel current TTS, run account/payment/subscription/service changes,
or bypass a local confirmation requirement. Local policy emits
`safe_to_execute_tools`, `requires_confirmation`, `blocked_actions`, and
`response_instruction` metadata; the gateway and LLM service must honor those
constraints.

The local mock weather path is different: it is an opt-in demo tool in
`services/llm_service/`, enabled with `LLM_ENABLE_MOCK_WEATHER_TOOL=1`, and is
used to make tool progress, latency, and filler speech visible. It is not proof
that arbitrary production tools or state-changing account actions are safe to
run.

## Live Assistant Flow

The `7000` assistant flow in `services/voice_gateway/main.py` routes STT events
through this layer before a normal LLM/TTS response:

- after `stt.partial`, the gateway evaluates partial policy without full
  semantic interpretation.
- VAD lifecycle events such as speech start/stop update observability state but
  do not by themselves trigger an LLM/TTS turn.
- after `stt.final`, the gateway runs the heuristic semantic interpreter,
  emits `policy.semantic_frame`, runs `evaluate_policy(...)`, emits
  `policy.decision`, and routes based on that decision.
- if a final transcript appears incomplete, the gateway emits
  `policy.turn_hold`, keeps the text pending, and can merge it with the next
  final before any LLM response.
- if the held transcript is an underspecified question, such as a question
  preamble or incomplete wh-clause, the gateway waits for the clarification
  delay and then emits a `CLARIFY` decision with a targeted prompt while keeping
  the held text available for a later continuation.
- if the assistant was interrupted, delivery context tracks what was generated,
  likely spoken, and likely unheard so the next LLM turn can recover naturally.

The `7100` two-party translation flow uses the separate compact turn-taking
gate, because it translates human-to-human speech rather than authorising
assistant actions.

## Policy Actions

| Action | Live behavior |
| --- | --- |
| `WAIT` | Keep listening and do not answer yet. |
| `SUPPRESS` | No-op the utterance in the current moment, such as side-talk or a backchannel while the assistant is speaking. |
| `CLARIFY` | Ask a short clarification question without sending the incomplete held turn to the LLM. |
| `RESPOND` | Continue into normal LLM response generation and TTS. |
| `CANCEL_TTS_AND_LISTEN` | Break interruptible TTS and give the floor to the user. |
| `SOFT_INTERRUPT_CHECKIN` | Pause with a brief check-in prompt for repeated or ambiguous short interjections. |
| `CONFIRM_BEFORE_ACTION` | Ask for explicit confirmation instead of allowing a risky action to run. |
| `REJECT_TOOL_EXECUTION` | Block unsafe action execution and speak the safe fallback response. |
| `ESCALATE` | Route to safe fallback handling when local policy cannot safely continue. |
| `END_CALL` | Speak a goodbye and hang up. |

Partial STT handling is intentionally conservative. Strong partials such as
`stop`, `wait`, `hang on`, corrections, or longer non-courtesy speech can stop
interruptible TTS. Short acknowledgements and fallback placeholder partials
usually wait for final STT. Repeated soft interjections can produce
`SOFT_INTERRUPT_CHECKIN` rather than treating every short sound as a full
interruption.

## Held Turns And Clarification

Incomplete final transcripts are held instead of being answered immediately. The
held text remains available for continuation merging, which lets a user say:

```text
Can I ask why
```

and then continue with:

```text
the sky is blue.
```

If the continuation does not arrive before the clarification delay and the
semantic frame says the question is underspecified, policy can route to
`CLARIFY`. That asks a targeted clarification prompt without handing the
fragment to the LLM as though it were complete.

## Scenario Evals

The YAML scenarios are regression tests for voice-agent judgement. They cover
messy conversation behaviors: incomplete thoughts, backchannels, interruptions,
self-corrections, side-talk, low-confidence account numbers, weak
confirmations, and thanks that do not mean goodbye.

Run the harness from the repo root:

```bash
uv run python evals/voice_policy/run_voice_policy_evals.py --reports-dir /tmp/voice-ai-evals
```

It prints a table and writes:

```text
/tmp/voice-ai-evals/voice_policy_eval_report.md
```

The same fixtures can be used to compare the current heuristic interpreter with
future local or cloud semantic interpreters while keeping deterministic policy
constant.

## Limits

- The heuristic semantic interpreter is deliberately basic.
- LLM semantic output, if added later, must still be schema-validated before
  local policy consumes it.
- Local policy is only as strong as the invariants encoded in code and tests.
- STT final events may still arrive too early or too late.
- The structured event stream is local demo observability, not production
  tracing, retention, audit logging, or SLO alerting.
- Production use would need richer state management, authorization, redaction,
  human review, and broader scenario coverage.
