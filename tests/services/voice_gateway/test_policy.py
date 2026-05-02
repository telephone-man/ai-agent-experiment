import pytest
from pydantic import ValidationError

from evals.voice_policy.run_voice_policy_evals import load_scenarios
from voice_policy import (
    HeuristicSemanticInterpreter,
    PolicyAction,
    PolicyDecision,
    SemanticFrame,
    SpeechAct,
    evaluate_policy,
)
from voice_policy.schema import Intent, PolicyInput


def test_semantic_frame_schema_parses_enums_and_slots():
    frame = SemanticFrame.model_validate(
        {
            "addressed_to_agent": True,
            "utterance_complete": True,
            "speech_act": "question",
            "intent": "ask_invoice",
            "risky_action_mentioned": False,
            "slots": {"invoice_id": "INV1042"},
            "confidence": 0.91,
            "rationale": "Invoice question.",
        }
    )

    assert frame.speech_act == SpeechAct.QUESTION
    assert frame.intent == Intent.ASK_INVOICE
    assert frame.slots["invoice_id"] == "INV1042"
    assert frame.clarification_needed is False
    assert frame.clarification_type is None


def test_semantic_frame_rejects_malformed_output():
    with pytest.raises(ValidationError):
        SemanticFrame.model_validate({"speech_act": "question", "unexpected": True})


def test_policy_decision_schema_parses_action():
    decision = PolicyDecision.model_validate(
        {
            "decision": "CONFIRM_BEFORE_ACTION",
            "confidence": 0.9,
            "reason": "Risky action needs confirmation.",
            "safe_to_execute_tools": False,
            "requires_confirmation": True,
        }
    )

    assert decision.decision == PolicyAction.CONFIRM_BEFORE_ACTION
    assert decision.requires_confirmation is True


def test_backchannel_while_agent_speaking_is_suppressed():
    state = PolicyInput(transcript="ok", agent_is_speaking=True)
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SUPPRESS


def test_single_hesitation_interjection_while_agent_speaking_is_suppressed():
    state = PolicyInput(transcript="ermmmm", agent_is_speaking=True)
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SUPPRESS
    assert "soft_interjection_suppressed" in decision.flags
    assert "hesitation_interjection" in decision.flags


def test_standalone_hesitation_while_agent_is_not_speaking_waits():
    state = PolicyInput(transcript="Um.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.WAIT
    assert "hesitation_wait" in decision.flags


def test_repeated_backchannel_while_agent_speaking_checks_in():
    state = PolicyInput(
        transcript="ok",
        agent_is_speaking=True,
        prior_soft_interjections_during_tts=1,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SOFT_INTERRUPT_CHECKIN
    assert "repeated_backchannel" in decision.flags


def test_pause_control_while_agent_speaking_checks_in():
    state = PolicyInput(
        transcript="Hang on.",
        agent_is_speaking=True,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SOFT_INTERRUPT_CHECKIN
    assert "pause_control" in decision.flags


def test_pause_control_after_interruption_checks_in():
    state = PolicyInput(
        transcript="Wait.",
        previous_assistant_delivery_pending=True,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SOFT_INTERRUPT_CHECKIN
    assert "pause_control" in decision.flags


def test_pause_control_final_is_suppressed_when_resume_is_pending():
    state = PolicyInput(
        transcript="Wait.",
        delivery_resume_pending=True,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SUPPRESS
    assert "delivery_resume_pending" in decision.flags


def test_repeated_hesitation_while_agent_speaking_checks_in():
    state = PolicyInput(
        transcript="ermmmm",
        agent_is_speaking=True,
        prior_soft_interjections_during_tts=1,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SOFT_INTERRUPT_CHECKIN
    assert "repeated_backchannel" in decision.flags
    assert "hesitation_interjection" in decision.flags


def test_soft_interjection_during_non_interruptible_tts_is_suppressed():
    state = PolicyInput(
        transcript="ermmmm",
        agent_is_speaking=True,
        tts_allow_interruptions=False,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SUPPRESS
    assert "non_interruptible_tts" in decision.flags


def test_real_interruption_while_agent_speaking_cancels_tts():
    state = PolicyInput(transcript="hang on that's wrong", agent_is_speaking=True)
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.CANCEL_TTS_AND_LISTEN


def test_question_preamble_after_question_invite_waits():
    state = PolicyInput(
        transcript="Let me ask why.",
        last_agent_message="Sure - go ahead and ask your question.",
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert frame.clarification_needed is True
    assert frame.clarification_type == "bare_why"
    assert decision.decision == PolicyAction.WAIT


def test_empty_question_preamble_waits():
    state = PolicyInput(transcript="Let me ask.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert frame.clarification_needed is True
    assert frame.clarification_type == "empty_question_preamble"
    assert decision.decision == PolicyAction.WAIT


def test_can_you_tell_me_bare_why_preamble_waits():
    state = PolicyInput(transcript="So can you tell me why?")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert frame.clarification_needed is True
    assert frame.clarification_type == "bare_why"
    assert decision.decision == PolicyAction.WAIT


def test_held_bare_why_clarifies_when_clarification_is_due():
    state = PolicyInput(
        transcript="Can I ask why?",
        clarification_due=True,
        turn_hold_elapsed_ms=1500,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert frame.clarification_needed is True
    assert frame.clarification_type == "bare_why"
    assert decision.decision == PolicyAction.CLARIFY
    assert "clarification_needed" in decision.flags
    assert "bare_why" in decision.flags


def test_generic_incomplete_turn_does_not_clarify_when_due():
    state = PolicyInput(
        transcript="I need to",
        clarification_due=True,
        turn_hold_elapsed_ms=1500,
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert frame.clarification_needed is False
    assert decision.decision == PolicyAction.WAIT


def test_incomplete_why_is_the_noun_phrase_waits_without_question_mark():
    state = PolicyInput(transcript="Why is the sky")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert frame.clarification_needed is True
    assert frame.clarification_type == "incomplete_wh_clause"
    assert decision.decision == PolicyAction.WAIT


def test_explicit_why_is_the_noun_phrase_question_can_respond():
    state = PolicyInput(transcript="Why is the sky?")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is True
    assert frame.speech_act == SpeechAct.QUESTION
    assert decision.decision == PolicyAction.RESPOND


def test_incomplete_wh_clause_waits():
    state = PolicyInput(transcript="Why the sky is")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert decision.decision == PolicyAction.WAIT


def test_clear_question_still_responds_immediately():
    state = PolicyInput(transcript="Why is the sky blue?")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is True
    assert decision.decision == PolicyAction.RESPOND


def test_preamble_with_question_body_is_complete():
    state = PolicyInput(transcript="Let me ask why the sky is blue.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is True
    assert decision.decision == PolicyAction.RESPOND


def test_preamble_with_missing_copula_wh_body_waits():
    state = PolicyInput(transcript="Let me ask why the sky blue.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert "question_preamble" in frame.flags
    assert "incomplete_wh_clause" in frame.flags
    assert decision.decision == PolicyAction.WAIT


def test_preamble_with_progressive_subject_fragment_waits():
    state = PolicyInput(transcript="Can I ask why my bill.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is False
    assert "question_preamble" in frame.flags
    assert "incomplete_wh_clause" in frame.flags
    assert decision.decision == PolicyAction.WAIT


def test_preamble_with_progressive_question_predicate_is_complete():
    state = PolicyInput(transcript="Can I ask why my bill changed.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is True
    assert decision.decision == PolicyAction.RESPOND


def test_embedded_question_ending_in_copula_can_be_complete():
    state = PolicyInput(transcript="Can I ask what the issue is.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is True
    assert decision.decision == PolicyAction.RESPOND


def test_preamble_with_fronted_wh_question_body_is_complete():
    state = PolicyInput(transcript="Can I ask why is the sky blue?")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is True
    assert decision.decision == PolicyAction.RESPOND


def test_can_you_tell_me_question_without_question_mark_is_complete():
    state = PolicyInput(transcript="Can you tell me what invoice INV1042 is for.")
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.utterance_complete is True
    assert decision.decision == PolicyAction.RESPOND


def test_exploration_not_authorisation_blocks_cancellation_tool():
    state = PolicyInput(
        transcript="I need to cancel my broadband actually what would the fee be"
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.intent == Intent.ASK_CANCELLATION_FEE
    assert frame.explicit_authorisation is False
    assert decision.decision == PolicyAction.RESPOND
    assert decision.safe_to_execute_tools is False
    assert "cancel_service" in decision.blocked_actions


def test_thanks_not_goodbye_continues_conversation():
    state = PolicyInput(
        transcript="Thanks, can you also tell me when invoice INV1042 is due?"
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.goodbye_detected is True
    assert frame.continue_conversation is True
    assert decision.decision == PolicyAction.RESPOND


def test_self_correction_preserves_corrected_slot_and_discards_old_value():
    state = PolicyInput(
        transcript="Please pay invoice INV1042 on Monday no sorry Tuesday morning",
        pending_action="pay_invoice",
        pending_action_risk="high",
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.slots["payment_date"] == "Tuesday morning"
    assert frame.discarded_slots["payment_date"] == "Monday"
    assert decision.safe_to_execute_tools is False
    assert decision.decision == PolicyAction.CONFIRM_BEFORE_ACTION


def test_cancellation_scope_self_correction_requires_confirmation():
    state = PolicyInput(
        transcript="I'd like to cancel my account actually just this add-on in my account"
    )
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert frame.intent == Intent.CANCEL_SERVICE
    assert frame.correction_detected is True
    assert frame.slots["cancellation_scope"] == "add-on"
    assert frame.discarded_slots["cancellation_scope"] == "account"
    assert decision.safe_to_execute_tools is False
    assert decision.decision == PolicyAction.CONFIRM_BEFORE_ACTION
    assert "cancel_service" in decision.blocked_actions


def test_substantive_partial_barge_in_cancels_interruptible_tts():
    state = PolicyInput(
        transcript="Actually I need to ask something else",
        is_partial=True,
        agent_is_speaking=True,
        tts_allow_interruptions=True,
    )

    decision = evaluate_policy(state)

    assert decision.decision == PolicyAction.CANCEL_TTS_AND_LISTEN


def test_courtesy_partial_does_not_barge_in_while_agent_speaking():
    state = PolicyInput(
        transcript="Thank you",
        is_partial=True,
        agent_is_speaking=True,
        tts_allow_interruptions=True,
    )

    decision = evaluate_policy(state)

    assert decision.decision == PolicyAction.WAIT
    assert "partial_wait" in decision.flags


def test_local_fallback_placeholder_partial_does_not_barge_in():
    state = PolicyInput(
        transcript="Listening...",
        is_partial=True,
        agent_is_speaking=True,
        tts_allow_interruptions=True,
        stt_provider="local_fallback",
        stt_fallback=True,
        stt_fallback_reason="offline speech activity detector",
    )

    decision = evaluate_policy(state)

    assert decision.decision == PolicyAction.WAIT
    assert "vad_only_partial" in decision.flags


def test_standalone_courtesy_while_agent_speaking_is_suppressed():
    state = PolicyInput(transcript="Thank you", agent_is_speaking=True)
    frame = HeuristicSemanticInterpreter().interpret(state)

    decision = evaluate_policy(state, frame)

    assert decision.decision == PolicyAction.SUPPRESS
    assert "courtesy_backchannel" in decision.flags


def test_eval_runner_loads_scenarios():
    scenarios = load_scenarios()

    assert len(scenarios) >= 11
    assert {scenario["id"] for scenario in scenarios} >= {
        "clean_question",
        "real_interrupt",
        "exploration_not_authorisation",
        "cancellation_scope_correction",
    }


def test_eval_runner_loads_yml_and_yaml_extensions(tmp_path):
    (tmp_path / "01_case.yml").write_text(
        'id: yml_case\ninput:\n  transcript: "hello"\n', encoding="utf-8"
    )
    (tmp_path / "02_case.yaml").write_text(
        'id: yaml_case\ninput:\n  transcript: "hello"\n', encoding="utf-8"
    )

    scenarios = load_scenarios(tmp_path)

    assert [scenario["id"] for scenario in scenarios] == ["yml_case", "yaml_case"]
