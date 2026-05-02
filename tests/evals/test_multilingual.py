import json

from evals.multilingual.run_multilingual_evals import (
    DEFAULT_MATRIX,
    load_cases,
    run_matrix,
)


def test_multilingual_matrix_cases_are_self_consistent_contracts():
    cases = load_cases(DEFAULT_MATRIX)
    routes = {f"{case['source_language']}->{case['target_language']}" for case in cases}
    outcomes = {case["expected"]["policy_outcome"] for case in cases}

    assert any(route.startswith("mixed-") for route in routes)
    assert any(case["source_language"] != case["target_language"] for case in cases)
    assert {"user_turn", "backchannel", "goodbye", "ignore", "partial"}.issubset(
        outcomes
    )
    assert any(
        case.get("is_final") is False
        and case.get("agent_is_speaking") is True
        and case["expected"].get("should_interrupt") is True
        for case in cases
    )
    for case in cases:
        expected = case["expected"]
        route = f"{case['source_language']}->{case['target_language']}"
        assert expected["route"] == route
        assert expected["stt_language"] == case["source_language"]
        assert expected["tts_language"] == case["target_language"]
        if expected["policy_outcome"] == "user_turn":
            assert expected["translation_request"] == {
                "source_language": case["source_language"],
                "target_language": case["target_language"],
                "text": case["transcript"],
            }
            assert expected["translated_text"] == (
                f"[{case['target_language']}] {case['transcript']}"
            )
        else:
            assert expected["translation_request"] is None
            assert expected["translated_text"] is None


def test_multilingual_eval_runner_writes_contract_reports(tmp_path):
    cases = load_cases(DEFAULT_MATRIX)
    rc = run_matrix(DEFAULT_MATRIX, tmp_path)

    assert rc == 0
    json_report = tmp_path / "multilingual_eval_report.json"
    markdown_report = tmp_path / "multilingual_eval_report.md"
    assert json_report.exists()
    assert markdown_report.exists()

    report = json.loads(json_report.read_text(encoding="utf-8"))
    assert report["metrics"] == {
        "case_count": len(cases),
        "passed_count": len(cases),
        "pass_rate": 1.0,
    }
    assert {result["actual"]["policy_outcome"] for result in report["results"]} >= {
        "user_turn",
        "backchannel",
        "goodbye",
        "ignore",
        "partial",
    }

    markdown = markdown_report.read_text(encoding="utf-8")
    assert (
        "These evals validate route metadata and service contracts, not acoustic quality."
        in markdown
    )
    assert "mixed-" in markdown
