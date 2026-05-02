# Voice Policy Evals

This directory contains offline regression scenarios for the semantic
interpreter and local policy adjudicator.

Run them with:

```bash
uv run python evals/voice_policy/run_voice_policy_evals.py
```

The runner uses `HeuristicSemanticInterpreter` by default, compares actual
semantic and policy fields against the YAML fixtures, prints a table, and writes
reports to `/tmp/voice-ai-evals/` by default. Use `--reports-dir` when you want
a different local output directory; generated reports are not committed.

Question-like incomplete turns can be evaluated in both phases: the first final
transcript should normally remain `WAIT`, while the same held turn with
`clarification_due: true` should become `CLARIFY` and set
`clarification_needed` metadata on the semantic frame.

Add a scenario by creating another `.yml` file in `scenarios/` with:

```yaml
id: my_case
description: What conversational behaviour this protects.
input:
  transcript: "user text"
  is_partial: false
  stt_confidence: 0.95
  current_flow: "account_support"
  agent_is_speaking: false
  tts_allow_interruptions: true
  pending_action: null
  pending_action_risk: "none"
expected_semantic:
  speech_act: "question"
expected_policy:
  decision: "RESPOND"
  safe_to_execute_tools: false
```

Only include expected fields that matter for the judgement being tested. Use
`forbidden_decisions` under `expected_policy` when the exact acceptable policy
may evolve but a dangerous decision must remain blocked.

Replay an exported voice event trace with:

```bash
uv run python evals/voice_policy/run_voice_policy_evals.py --trace path/to/session_events.json
```

The multilingual eval runner lives in `evals/multilingual/` and checks
deterministic route and turn-taking contracts. It is not a translation-quality
benchmark.
