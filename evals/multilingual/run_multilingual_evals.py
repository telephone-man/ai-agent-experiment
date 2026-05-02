#!/usr/bin/env python3
"""Run deterministic multilingual contract evals for the translation path."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.common import format_row, write_report_pair  # noqa: E402
from services.voice_gateway.turn_taking import SemanticTurnDetector  # noqa: E402


DEFAULT_MATRIX = Path(__file__).resolve().parent / "matrix.json"
DEFAULT_REPORTS_DIR = Path("/tmp/voice-ai-multilingual")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    args = parser.parse_args(argv)
    return run_matrix(args.matrix, args.reports_dir)


def run_matrix(
    matrix_path: Path = DEFAULT_MATRIX, reports_dir: Path = DEFAULT_REPORTS_DIR
) -> int:
    cases = load_cases(matrix_path)
    detector = SemanticTurnDetector()
    results = [evaluate_case(case, detector) for case in cases]
    metrics = {
        "case_count": len(results),
        "passed_count": sum(1 for result in results if result["passed"]),
    }
    metrics["pass_rate"] = (
        metrics["passed_count"] / metrics["case_count"]
        if metrics["case_count"]
        else 0.0
    )
    print_table(results)
    print()
    print(f"pass_rate: {metrics['pass_rate']:.2%}")
    write_reports(results, metrics, reports_dir)
    return 0 if all(result["passed"] for result in results) else 1


def load_cases(matrix_path: Path) -> list[dict[str, Any]]:
    body = json.loads(matrix_path.read_text(encoding="utf-8"))
    cases = body.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{matrix_path} must contain a non-empty cases list")
    return cases


def evaluate_case(
    case: dict[str, Any], detector: SemanticTurnDetector
) -> dict[str, Any]:
    transcript = str(case["transcript"])
    source_language = str(case["source_language"])
    target_language = str(case["target_language"])
    is_final = bool(case.get("is_final", True))
    confidence = float(case.get("confidence", 1.0))
    agent_is_speaking = bool(case.get("agent_is_speaking", False))
    decision = detector.decide(
        transcript,
        is_final=is_final,
        confidence=confidence,
        agent_is_speaking=agent_is_speaking,
    )
    actual = {
        "route": f"{source_language}->{target_language}",
        "stt_language": source_language,
        "tts_language": target_language,
        "is_final": is_final,
        "confidence": confidence,
        "agent_is_speaking": agent_is_speaking,
        "policy_outcome": decision.action.value,
        "should_interrupt": decision.should_interrupt,
        "translation_request": None,
        "translated_text": None,
    }
    if decision.action.value == "user_turn":
        actual["translation_request"] = {
            "source_language": source_language,
            "target_language": target_language,
            "text": decision.text,
        }
        actual["translated_text"] = f"[{target_language}] {decision.text}"
    expected = case.get("expected") or {}
    mismatches = compare_expected(actual, expected)
    return {
        "id": case["id"],
        "passed": not mismatches,
        "actual": actual,
        "expected": expected,
        "mismatches": mismatches,
    }


def compare_expected(
    actual: dict[str, Any], expected: dict[str, Any], prefix: str = ""
) -> list[str]:
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        field = f"{prefix}.{key}" if prefix else key
        actual_value = actual.get(key)
        if isinstance(expected_value, dict):
            if not isinstance(actual_value, dict):
                mismatches.append(f"{field}: expected mapping, got {actual_value!r}")
                continue
            mismatches.extend(compare_expected(actual_value, expected_value, field))
            continue
        if actual_value != expected_value:
            mismatches.append(
                f"{field}: expected {expected_value!r}, got {actual_value!r}"
            )
    return mismatches


def print_table(results: list[dict[str, Any]]) -> None:
    rows = [
        (
            result["id"],
            result["actual"]["route"],
            result["actual"]["policy_outcome"],
            "yes" if result["passed"] else "no",
        )
        for result in results
    ]
    widths = [
        max(
            len(str(row[index])) for row in [("Case", "Route", "Policy", "Pass"), *rows]
        )
        for index in range(4)
    ]
    print(format_row(("Case", "Route", "Policy", "Pass"), widths))
    print(format_row(tuple("-" * width for width in widths), widths))
    for row in rows:
        print(format_row(row, widths))
    for result in results:
        if result["passed"]:
            continue
        print(f"\n{result['id']} mismatches:")
        for mismatch in result["mismatches"]:
            print(f"- {mismatch}")


def write_reports(
    results: list[dict[str, Any]], metrics: dict[str, Any], reports_dir: Path
) -> None:
    write_report_pair(
        reports_dir,
        "multilingual_eval_report",
        {"metrics": metrics, "results": results},
        render_markdown(results, metrics),
    )


def render_markdown(results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    lines = [
        "# Multilingual Contract Eval Report",
        "",
        "These evals validate route metadata and service contracts, not acoustic quality.",
        "",
        f"- pass_rate: {metrics['pass_rate']:.2%}",
        f"- passed_count: {metrics['passed_count']}",
        f"- case_count: {metrics['case_count']}",
        "",
        "| Case | Route | STT | TTS | Policy | Interrupt | Pass |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        actual = result["actual"]
        lines.append(
            "| {id} | {route} | {stt} | {tts} | {policy} | {interrupt} | {passed} |".format(
                id=result["id"],
                route=actual["route"],
                stt=actual["stt_language"],
                tts=actual["tts_language"],
                policy=actual["policy_outcome"],
                interrupt="yes" if actual["should_interrupt"] else "no",
                passed="yes" if result["passed"] else "no",
            )
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
