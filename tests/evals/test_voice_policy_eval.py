import json
from pathlib import Path

from evals.voice_policy.run_voice_policy_evals import (
    DEFAULT_SCENARIOS_DIR,
    load_scenarios,
    run_scenarios,
)
from voice_policy import HeuristicSemanticInterpreter, PolicyAction, evaluate_policy
from voice_policy.trace_adapter import iter_policy_inputs_from_events, load_trace_events


RUNTIME_POLICY_ACTIONS = {
    PolicyAction.WAIT.value,
    PolicyAction.CLARIFY.value,
    PolicyAction.RESPOND.value,
    PolicyAction.SUPPRESS.value,
    PolicyAction.CANCEL_TTS_AND_LISTEN.value,
    PolicyAction.SOFT_INTERRUPT_CHECKIN.value,
    PolicyAction.CONFIRM_BEFORE_ACTION.value,
    PolicyAction.END_CALL.value,
}


def test_voice_policy_scenarios_cover_runtime_policy_actions():
    scenarios = load_scenarios(DEFAULT_SCENARIOS_DIR)
    expected_decisions = {
        scenario["expected_policy"]["decision"] for scenario in scenarios
    }

    assert RUNTIME_POLICY_ACTIONS.issubset(expected_decisions)


def test_voice_policy_eval_runner_writes_passing_reports(tmp_path):
    rc = run_scenarios(DEFAULT_SCENARIOS_DIR, tmp_path)

    assert rc == 0
    json_report = tmp_path / "voice_policy_eval_report.json"
    markdown_report = tmp_path / "voice_policy_eval_report.md"
    assert json_report.exists()
    assert markdown_report.exists()

    report = json.loads(json_report.read_text(encoding="utf-8"))
    metrics = report["metrics"]
    assert metrics["scenario_count"] == metrics["passed_count"]
    assert metrics["pass_rate"] == 1.0
    assert metrics["unsafe_tool_execution_count"] == 0
    assert metrics["premature_end_call_count"] == 0
    assert metrics["missed_interrupt_count"] == 0
    assert metrics["false_interrupt_count"] == 0


def test_multilingual_trace_fixture_replays_into_offline_policy_inputs():
    trace_path = Path("docs/replay-fixtures/multilingual_replay_trace.json")
    policy_inputs = list(iter_policy_inputs_from_events(load_trace_events(trace_path)))

    assert [
        (item.transcript, item.is_partial, item.agent_is_speaking)
        for item in policy_inputs
    ] == [
        ("What is the weather in Lisbon tomorrow?", False, False),
        ("perdona", True, True),
        ("Perdona, puedes repetirlo mas despacio?", False, False),
    ]

    interpreter = HeuristicSemanticInterpreter()
    decisions_by_transcript = {}
    for policy_input in policy_inputs:
        semantic_frame = interpreter.interpret(policy_input)
        policy_decision = evaluate_policy(policy_input, semantic_frame)
        decisions_by_transcript[policy_input.transcript] = policy_decision.decision

    assert (
        decisions_by_transcript["What is the weather in Lisbon tomorrow?"]
        == PolicyAction.RESPOND
    )
    assert (
        decisions_by_transcript["Perdona, puedes repetirlo mas despacio?"]
        == PolicyAction.RESPOND
    )


def test_trace_adapter_preserves_explicit_zero_confidence():
    policy_inputs = list(
        iter_policy_inputs_from_events(
            [
                {
                    "type": "stt.final",
                    "session_id": "session-zero",
                    "payload": {"text": "no confidence", "confidence": 0},
                },
                {
                    "type": "stt.final",
                    "session_id": "session-default",
                    "payload": {"text": "default confidence"},
                },
            ]
        )
    )

    assert [item.stt_confidence for item in policy_inputs] == [0.0, 1.0]
