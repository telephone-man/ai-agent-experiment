#!/usr/bin/env python3
"""Run offline semantic-policy evaluation scenarios."""

from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.common import format_row, write_report_pair  # noqa: E402
from voice_policy import (
    HeuristicSemanticInterpreter,
    PolicyAction,
    PolicyInput,
    evaluate_policy,
)  # noqa: E402
from voice_policy.trace_adapter import iter_policy_inputs_from_events, load_trace_events  # noqa: E402


DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
DEFAULT_REPORTS_DIR = Path("/tmp/voice-ai-evals")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run voice policy semantic judgement evals."
    )
    parser.add_argument("--scenarios-dir", type=Path, default=DEFAULT_SCENARIOS_DIR)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument(
        "--trace",
        type=Path,
        help="Replay an exported voice event trace instead of scenario fixtures.",
    )
    args = parser.parse_args(argv)

    if args.trace:
        return run_trace(args.trace, args.reports_dir)
    return run_scenarios(args.scenarios_dir, args.reports_dir)


def run_scenarios(
    scenarios_dir: Path = DEFAULT_SCENARIOS_DIR, reports_dir: Path = DEFAULT_REPORTS_DIR
) -> int:
    scenarios = load_scenarios(scenarios_dir)
    interpreter = HeuristicSemanticInterpreter()
    results: list[dict[str, Any]] = []

    for scenario in scenarios:
        scenario_input = dict(scenario.get("input") or {})
        scenario_input.setdefault("scenario_id", scenario["id"])
        scenario_input.setdefault("session_id", "demo")
        scenario_input.setdefault("turn_id", scenario["id"])
        policy_input = PolicyInput.model_validate(scenario_input)
        semantic_frame = interpreter.interpret(policy_input)
        policy_decision = evaluate_policy(policy_input, semantic_frame)

        semantic_checks = compare_expected(
            semantic_frame.model_dump(mode="json"),
            scenario.get("expected_semantic") or {},
        )
        policy_checks = compare_expected(
            policy_decision.model_dump(mode="json"),
            scenario.get("expected_policy") or {},
        )
        passed = not semantic_checks and not policy_checks
        results.append(
            {
                "id": scenario["id"],
                "description": scenario.get("description", ""),
                "passed": passed,
                "expected_semantic": scenario.get("expected_semantic") or {},
                "actual_semantic": semantic_frame.model_dump(mode="json"),
                "semantic_mismatches": semantic_checks,
                "expected_policy": scenario.get("expected_policy") or {},
                "actual_policy": policy_decision.model_dump(mode="json"),
                "policy_mismatches": policy_checks,
            }
        )

    metrics = calculate_metrics(results)
    print_scenario_table(results)
    print()
    print(f"pass_rate: {metrics['pass_rate']:.2%}")
    print(f"unsafe_tool_execution_count: {metrics['unsafe_tool_execution_count']}")
    print(f"premature_end_call_count: {metrics['premature_end_call_count']}")
    print(f"missed_interrupt_count: {metrics['missed_interrupt_count']}")
    print(f"false_interrupt_count: {metrics['false_interrupt_count']}")
    print(f"confirmation_required_count: {metrics['confirmation_required_count']}")

    write_reports(results, metrics, reports_dir, stem="voice_policy_eval_report")
    return 0 if all(result["passed"] for result in results) else 1


def run_trace(trace_path: Path, reports_dir: Path) -> int:
    events = load_trace_events(trace_path)
    interpreter = HeuristicSemanticInterpreter()
    rows: list[dict[str, Any]] = []

    for policy_input in iter_policy_inputs_from_events(events):
        semantic_frame = interpreter.interpret(policy_input)
        policy_decision = evaluate_policy(policy_input, semantic_frame)
        row = {
            "turn_id": policy_input.turn_id,
            "transcript": policy_input.transcript,
            "is_partial": policy_input.is_partial,
            "semantic_frame": semantic_frame.model_dump(mode="json"),
            "policy_decision": policy_decision.model_dump(mode="json"),
        }
        rows.append(row)

    print_trace_table(rows)
    write_report_pair(
        reports_dir,
        "voice_policy_trace_report",
        {"trace": str(trace_path), "turns": rows},
        render_trace_markdown(trace_path, rows),
    )
    return 0


def load_scenarios(scenarios_dir: Path = DEFAULT_SCENARIOS_DIR) -> list[dict[str, Any]]:
    scenarios = []
    paths = sorted({*scenarios_dir.glob("*.yml"), *scenarios_dir.glob("*.yaml")})
    for path in paths:
        scenario = load_simple_yaml(path.read_text(encoding="utf-8"))
        scenario["_path"] = str(path)
        if "id" not in scenario:
            raise ValueError(f"{path} is missing id")
        scenarios.append(scenario)
    if not scenarios:
        raise ValueError(f"no .yml scenarios found in {scenarios_dir}")
    return scenarios


def compare_expected(
    actual: dict[str, Any], expected: dict[str, Any], prefix: str = ""
) -> list[str]:
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        field = f"{prefix}.{key}" if prefix else key
        if key == "forbidden_decisions":
            forbidden = set(expected_value or [])
            if actual.get("decision") in forbidden:
                mismatches.append(
                    f"{field}: actual decision {actual.get('decision')} is forbidden"
                )
            continue
        actual_value = actual.get(key)
        if isinstance(expected_value, dict):
            if not isinstance(actual_value, dict):
                mismatches.append(f"{field}: expected mapping, got {actual_value!r}")
                continue
            mismatches.extend(compare_expected(actual_value, expected_value, field))
            continue
        if isinstance(expected_value, list):
            if field.endswith("decision"):
                if actual_value not in expected_value:
                    mismatches.append(
                        f"{field}: expected one of {expected_value!r}, got {actual_value!r}"
                    )
                continue
            if not isinstance(actual_value, list):
                mismatches.append(
                    f"{field}: expected list containing {expected_value!r}, got {actual_value!r}"
                )
                continue
            missing = [item for item in expected_value if item not in actual_value]
            if missing:
                mismatches.append(f"{field}: missing expected list values {missing!r}")
            continue
        if actual_value != expected_value:
            mismatches.append(
                f"{field}: expected {expected_value!r}, got {actual_value!r}"
            )
    return mismatches


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    unsafe_tool_execution_count = 0
    premature_end_call_count = 0
    missed_interrupt_count = 0
    false_interrupt_count = 0
    confirmation_required_count = 0

    for result in results:
        actual = result["actual_policy"]
        expected = result["expected_policy"]
        semantic = result["actual_semantic"]
        decision = actual["decision"]
        expected_decision = expected.get("decision")
        forbidden = set(expected.get("forbidden_decisions") or [])

        if actual.get("safe_to_execute_tools") and (
            expected.get("safe_to_execute_tools") is False
            or (
                semantic.get("risky_action_mentioned")
                and not semantic.get("explicit_authorisation")
            )
        ):
            unsafe_tool_execution_count += 1
        if decision == PolicyAction.END_CALL.value and (
            expected_decision != PolicyAction.END_CALL.value or decision in forbidden
        ):
            premature_end_call_count += 1
        if (
            expected_decision == PolicyAction.CANCEL_TTS_AND_LISTEN.value
            and decision != expected_decision
        ):
            missed_interrupt_count += 1
        if (
            decision == PolicyAction.CANCEL_TTS_AND_LISTEN.value
            and expected_decision != decision
        ):
            false_interrupt_count += 1
        if (
            actual.get("requires_confirmation")
            or decision == PolicyAction.CONFIRM_BEFORE_ACTION.value
        ):
            confirmation_required_count += 1

    return {
        "scenario_count": total,
        "passed_count": sum(1 for result in results if result["passed"]),
        "pass_rate": (sum(1 for result in results if result["passed"]) / total)
        if total
        else 0.0,
        "unsafe_tool_execution_count": unsafe_tool_execution_count,
        "premature_end_call_count": premature_end_call_count,
        "missed_interrupt_count": missed_interrupt_count,
        "false_interrupt_count": false_interrupt_count,
        "confirmation_required_count": confirmation_required_count,
    }


def print_scenario_table(results: list[dict[str, Any]]) -> None:
    rows = [
        (
            result["id"],
            str(result["expected_policy"].get("decision") or "see constraints"),
            result["actual_policy"]["decision"],
            "yes" if result["passed"] else "no",
        )
        for result in results
    ]
    widths = [
        max(
            len(str(row[index]))
            for row in [("Scenario", "Expected Policy", "Actual Policy", "Pass"), *rows]
        )
        for index in range(4)
    ]
    header = ("Scenario", "Expected Policy", "Actual Policy", "Pass")
    print(format_row(header, widths))
    print(format_row(tuple("-" * width for width in widths), widths))
    for row in rows:
        print(format_row(row, widths))
    for result in results:
        if result["passed"]:
            continue
        print(f"\n{result['id']} mismatches:")
        for mismatch in [*result["semantic_mismatches"], *result["policy_mismatches"]]:
            print(f"- {mismatch}")


def print_trace_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No STT final or agent-speaking partial events found in trace.")
        return
    table_rows = [
        (
            row["turn_id"],
            row["policy_decision"]["decision"],
            row["semantic_frame"]["speech_act"],
            row["transcript"][:56],
        )
        for row in rows
    ]
    widths = [
        max(
            len(str(row[index]))
            for row in [("Turn", "Policy", "Speech Act", "Transcript"), *table_rows]
        )
        for index in range(4)
    ]
    print(format_row(("Turn", "Policy", "Speech Act", "Transcript"), widths))
    print(format_row(tuple("-" * width for width in widths), widths))
    for row in table_rows:
        print(format_row(row, widths))


def write_reports(
    results: list[dict[str, Any]],
    metrics: dict[str, Any],
    reports_dir: Path,
    *,
    stem: str,
) -> None:
    write_report_pair(
        reports_dir,
        stem,
        {"metrics": metrics, "results": results},
        render_markdown(results, metrics),
    )


def render_markdown(results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    lines = [
        "# Voice Policy Eval Report",
        "",
        f"- scenario_count: {metrics['scenario_count']}",
        f"- passed_count: {metrics['passed_count']}",
        f"- pass_rate: {metrics['pass_rate']:.2%}",
        f"- unsafe_tool_execution_count: {metrics['unsafe_tool_execution_count']}",
        f"- premature_end_call_count: {metrics['premature_end_call_count']}",
        f"- missed_interrupt_count: {metrics['missed_interrupt_count']}",
        f"- false_interrupt_count: {metrics['false_interrupt_count']}",
        f"- confirmation_required_count: {metrics['confirmation_required_count']}",
        "",
        "| Scenario | Description | Expected Policy | Actual Policy | Pass |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            "| {id} | {description} | {expected} | {actual} | {passed} |".format(
                id=_markdown_cell(result["id"]),
                description=_markdown_cell(result["description"]),
                expected=_markdown_cell(
                    result["expected_policy"].get("decision", "see constraints")
                ),
                actual=_markdown_cell(result["actual_policy"]["decision"]),
                passed="yes" if result["passed"] else "no",
            )
        )
    failures = [result for result in results if not result["passed"]]
    if failures:
        lines.extend(["", "## Failures", ""])
        for result in failures:
            lines.append(f"### {_markdown_cell(result['id'])}")
            for mismatch in [
                *result["semantic_mismatches"],
                *result["policy_mismatches"],
            ]:
                lines.append(f"- `{_markdown_cell(mismatch)}`")
            lines.append("")
    return "\n".join(lines) + "\n"


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_trace_markdown(trace_path: Path, rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Voice Policy Trace Replay",
        "",
        f"Trace: `{trace_path}`",
        "",
        "| Turn | Policy | Speech Act | Transcript |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {turn} | {policy} | {speech_act} | {transcript} |".format(
                turn=row["turn_id"],
                policy=row["policy_decision"]["decision"],
                speech_act=row["semantic_frame"]["speech_act"],
                transcript=str(row["transcript"]).replace("|", "\\|"),
            )
        )
    return "\n".join(lines) + "\n"


def load_simple_yaml(text: str) -> dict[str, Any]:
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))
    if not lines:
        return {}
    parsed, next_index = _parse_block(lines, 0, lines[0][0])
    if next_index != len(lines):
        raise ValueError("could not parse complete YAML document")
    if not isinstance(parsed, dict):
        raise ValueError("top-level YAML document must be a mapping")
    return parsed


def _parse_block(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[Any, int]:
    if lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    data: dict[str, Any] = {}
    while index < len(lines):
        current_indent, stripped = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"unexpected indentation near: {stripped}")
        if stripped.startswith("- "):
            break
        if ":" not in stripped:
            raise ValueError(f"expected key/value line: {stripped}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        index += 1
        if value:
            data[key] = _parse_scalar(value)
        elif index < len(lines) and lines[index][0] > current_indent:
            data[key], index = _parse_block(lines, index, lines[index][0])
        else:
            data[key] = None
    return data, index


def _parse_list(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    data: list[Any] = []
    while index < len(lines):
        current_indent, stripped = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent or not stripped.startswith("- "):
            break
        value = stripped[2:].strip()
        index += 1
        if value:
            data.append(_parse_scalar(value))
        elif index < len(lines) and lines[index][0] > current_indent:
            item, index = _parse_block(lines, index, lines[index][0])
            data.append(item)
        else:
            data.append(None)
    return data, index


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


if __name__ == "__main__":
    raise SystemExit(main())
