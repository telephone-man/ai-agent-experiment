"""Deterministic local policy adjudication for voice-agent routing."""

from __future__ import annotations

import re

from voice_policy.schema import (
    Intent,
    PolicyAction,
    PolicyDecision,
    PolicyInput,
    RiskLevel,
    SemanticFrame,
    SpeechAct,
)
from voice_policy.semantic_interpreter import coerce_policy_input


_STRONG_INTERRUPT_RE = re.compile(
    r"\b(stop|wait|hang on|hold on|pause|interrupt|can i interrupt|"
    r"you(?:'re| are) still|no stop|that's wrong|that is wrong|actually that's wrong)\b",
    re.I,
)
_SUBSTANTIVE_PARTIAL_MIN_CHARS = 8
_WEAK_CONFIRMATIONS = {
    "yeah",
    "yep",
    "yes",
    "ok",
    "okay",
    "sure",
    "right",
    "mmhmm",
    "mhm",
}
_SHORT_BACKCHANNELS = _WEAK_CONFIRMATIONS | {"ah", "aha", "hm", "hmm", "mm", "mhmm"}
_COURTESY_BACKCHANNELS = {
    "appreciate it",
    "cheers",
    "got it thanks",
    "ok thanks",
    "okay thanks",
    "right thanks",
    "ta",
    "thank you",
    "thank you for that",
    "thank you so much",
    "thank you very much",
    "thanks",
    "thanks a lot",
    "thanks for that",
    "thanks so much",
    "thanks very much",
}
_PAUSE_CONTROL_PHRASES = {
    "wait",
    "wait a second",
    "wait one second",
    "hang on",
    "hold on",
    "pause",
    "one second",
    "just a sec",
    "just a second",
}
_RISKY_INTENTS = {
    Intent.PAY_INVOICE,
    Intent.CANCEL_SERVICE,
    Intent.CHANGE_DIRECT_DEBIT,
    Intent.UPDATE_DETAILS,
}


def evaluate_policy(
    policy_input: PolicyInput | dict, semantic_frame: SemanticFrame | dict | None = None
) -> PolicyDecision:
    """Return the final local routing decision.

    This layer enforces safety and control invariants. It is deliberately not a
    general chatbot and does not try to understand every possible utterance.
    """

    state = coerce_policy_input(policy_input)
    frame = _coerce_frame(semantic_frame)

    if state.is_partial:
        transcript = " ".join(state.transcript.strip().split())
        if _is_vad_only_partial(state, transcript):
            return _decision(
                PolicyAction.WAIT,
                0.94,
                "Partial event is VAD-only and does not contain transcript text.",
                flags=["partial_wait", "vad_only_partial"],
            )
        if state.delivery_resume_pending and _is_pause_control(transcript):
            return _decision(
                PolicyAction.SUPPRESS,
                0.88,
                "Pause control was already acknowledged for the interrupted delivery.",
                flags=["pause_control", "delivery_resume_pending"],
            )
        if (
            state.agent_is_speaking
            and state.tts_allow_interruptions
            and _is_pause_control(transcript)
        ):
            return _decision(
                PolicyAction.SOFT_INTERRUPT_CHECKIN,
                0.9,
                "Caller asked the assistant to pause while it was speaking.",
                response_instruction="Briefly acknowledge the pause and give the user a moment to continue.",
                flags=["partial_interrupt", "pause_control"],
            )
        is_substantive_barge_in = (
            len(transcript) > _SUBSTANTIVE_PARTIAL_MIN_CHARS
            and not _is_short_backchannel(transcript)
            and not _is_courtesy_backchannel(transcript)
        )
        if (
            state.agent_is_speaking
            and state.tts_allow_interruptions
            and (_STRONG_INTERRUPT_RE.search(transcript) or is_substantive_barge_in)
        ):
            return _decision(
                PolicyAction.CANCEL_TTS_AND_LISTEN,
                0.9,
                "Partial transcript indicates a substantive barge-in while the agent is speaking.",
                flags=["partial_interrupt"],
            )
        if (
            state.agent_is_speaking
            and not state.tts_allow_interruptions
            and (_STRONG_INTERRUPT_RE.search(transcript) or is_substantive_barge_in)
        ):
            return _decision(
                PolicyAction.SUPPRESS,
                0.82,
                "Agent is in a non-interruptible section, so the partial interruption is suppressed.",
                flags=["partial_interrupt_suppressed", "non_interruptible_tts"],
            )
        return _decision(
            PolicyAction.WAIT,
            0.92,
            "Partial transcript is not final.",
            flags=["partial_wait"],
        )

    if state.delivery_resume_pending and _is_pause_control(state.transcript):
        return _decision(
            PolicyAction.SUPPRESS,
            0.88,
            "Pause control was already acknowledged for the interrupted delivery.",
            flags=["pause_control", "delivery_resume_pending"],
        )

    if state.agent_is_speaking and _is_courtesy_backchannel(state.transcript):
        return _decision(
            PolicyAction.SUPPRESS,
            0.9,
            "Standalone courtesy while the agent is speaking.",
            flags=["courtesy_backchannel"],
        )

    if _is_pause_control(state.transcript):
        if (
            state.tts_allow_interruptions
            and (state.agent_is_speaking or state.previous_assistant_delivery_pending)
        ):
            return _decision(
                PolicyAction.SOFT_INTERRUPT_CHECKIN,
                0.9,
                "Caller asked the assistant to pause an interrupted delivery.",
                response_instruction="Briefly acknowledge the pause and give the user a moment to continue.",
                flags=["pause_control"],
            )
        return _decision(
            PolicyAction.WAIT,
            0.86,
            "Standalone pause control does not need a response yet.",
            flags=["pause_control_wait"],
        )

    if not state.agent_is_speaking and _is_standalone_hesitation(state.transcript):
        return _decision(
            PolicyAction.WAIT,
            0.86,
            "Standalone hesitation does not need a response yet.",
            flags=["hesitation_wait"],
        )

    if state.agent_is_speaking and _is_soft_interjection(state.transcript, frame):
        soft_flags = _soft_interjection_flags(
            state.transcript, state.prior_soft_interjections_during_tts
        )
        if state.tts_allow_interruptions and _should_check_in_for_soft_interjection(
            state
        ):
            return _decision(
                PolicyAction.SOFT_INTERRUPT_CHECKIN,
                0.86,
                "Short interjection while the agent is speaking suggests the user may want the floor.",
                response_instruction="Offer a brief low-pressure pause for the user to continue.",
                flags=soft_flags,
            )
        flags = ["soft_interjection_suppressed", *soft_flags]
        if not state.tts_allow_interruptions:
            flags.append("non_interruptible_tts")
        return _decision(
            PolicyAction.SUPPRESS,
            0.88,
            "Short backchannel while agent is speaking.",
            flags=flags,
        )

    if state.agent_is_speaking and frame.speech_act == SpeechAct.BACKCHANNEL:
        return _decision(
            PolicyAction.SUPPRESS,
            0.92,
            "Backchannel while agent is speaking.",
            flags=["backchannel"],
        )

    if state.agent_is_speaking and frame.speech_act == SpeechAct.INTERRUPTION:
        if state.tts_allow_interruptions:
            return _decision(
                PolicyAction.CANCEL_TTS_AND_LISTEN,
                0.92,
                "User interruption is allowed to stop current TTS.",
                flags=["interruption"],
            )
        return _decision(
            PolicyAction.SUPPRESS,
            0.82,
            "Agent is in a non-interruptible section, so the interruption is suppressed.",
            flags=["interruption_suppressed", "non_interruptible_tts"],
        )

    if not frame.addressed_to_agent or frame.speech_act == SpeechAct.SIDE_TALK:
        action = PolicyAction.SUPPRESS if state.agent_is_speaking else PolicyAction.WAIT
        return _decision(
            action,
            0.86,
            "Utterance appears not to be addressed to the agent.",
            flags=["side_talk"],
        )

    if not frame.utterance_complete:
        if frame.clarification_needed and state.clarification_due:
            flags = ["clarification_needed"]
            if frame.clarification_type:
                flags.append(frame.clarification_type)
            return _decision(
                PolicyAction.CLARIFY,
                0.86,
                "Held question is still underspecified.",
                response_instruction="Ask a concise clarification question before sending the turn to the LLM.",
                flags=flags,
            )
        return _decision(
            PolicyAction.WAIT,
            0.88,
            "Utterance appears incomplete.",
            flags=["incomplete"],
        )

    if _low_confidence_for_critical_workflow(state, frame):
        return _decision(
            PolicyAction.CONFIRM_BEFORE_ACTION,
            0.8,
            "Low confidence in a workflow that may affect account or payment state.",
            requires_confirmation=True,
            response_instruction="Ask the user to confirm the uncertain detail before continuing.",
            blocked_actions=_blocked_actions_for(frame),
            flags=["low_confidence", "critical_workflow"],
        )

    if state.pending_action_risk in {RiskLevel.HIGH, RiskLevel.IRREVERSIBLE}:
        if not _clear_authorisation(state, frame):
            return _decision(
                PolicyAction.CONFIRM_BEFORE_ACTION,
                0.9,
                "High-risk pending action requires explicit authorisation.",
                requires_confirmation=True,
                response_instruction="Ask for explicit confirmation before performing the pending action.",
                blocked_actions=[state.pending_action]
                if state.pending_action
                else _blocked_actions_for(frame),
                flags=["high_risk_pending_action"],
            )

    if frame.correction_detected:
        return _decision(
            PolicyAction.CONFIRM_BEFORE_ACTION
            if frame.requires_confirmation
            else PolicyAction.RESPOND,
            0.86,
            "User corrected earlier slot values; use corrected slots and discard superseded values.",
            requires_confirmation=frame.requires_confirmation,
            safe_to_execute_tools=False,
            response_instruction="Use the corrected information and confirm before any account or payment change.",
            blocked_actions=_blocked_actions_for(frame),
            flags=[
                "self_correction",
                *[f"discarded_{key}" for key in frame.discarded_slots],
            ],
        )

    if frame.requires_confirmation and frame.risky_action_mentioned:
        blocked = _blocked_actions_for(frame)
        return _decision(
            PolicyAction.CONFIRM_BEFORE_ACTION,
            0.88,
            "Risky account or payment request requires explicit confirmation before execution.",
            requires_confirmation=True,
            safe_to_execute_tools=False,
            response_instruction="Ask the user to confirm the requested account or payment change before any tool is run.",
            blocked_actions=blocked,
            flags=["risky_action_requires_confirmation", *_risk_flags(frame, blocked)],
        )

    if frame.risky_action_mentioned and not frame.explicit_authorisation:
        blocked = _blocked_actions_for(frame)
        return _decision(
            PolicyAction.RESPOND,
            0.88,
            "User mentioned a risky action without explicit authorisation.",
            safe_to_execute_tools=False,
            response_instruction=(
                "Answer the user's question or acknowledge the topic, but do not perform the risky action. "
                "Make clear that nothing will be changed without explicit confirmation."
            ),
            blocked_actions=blocked,
            flags=["risky_action_without_authorisation", *_risk_flags(frame, blocked)],
        )

    if frame.goodbye_detected and frame.continue_conversation:
        return _decision(
            PolicyAction.RESPOND,
            0.82,
            "Thanks or goodbye-like language is followed by continued conversation.",
            response_instruction="Continue the conversation and answer the new request.",
            flags=["thanks_not_goodbye"],
        )

    if frame.goodbye_detected and not frame.continue_conversation:
        return _decision(
            PolicyAction.END_CALL,
            0.9,
            "User clearly ended the call.",
            flags=["goodbye"],
        )

    return _decision(
        PolicyAction.RESPOND,
        0.82,
        "No blocking policy condition matched.",
        response_instruction="Generate a normal concise voice response.",
    )


def _coerce_frame(semantic_frame: SemanticFrame | dict | None) -> SemanticFrame:
    if semantic_frame is None:
        return SemanticFrame.unknown(
            rationale="No semantic frame supplied.", flags=["semantic_missing"]
        )
    if isinstance(semantic_frame, SemanticFrame):
        return semantic_frame
    return SemanticFrame.model_validate(semantic_frame)


def _decision(
    action: PolicyAction,
    confidence: float,
    reason: str,
    *,
    safe_to_execute_tools: bool = False,
    requires_confirmation: bool = False,
    response_instruction: str | None = None,
    blocked_actions: list[str] | None = None,
    flags: list[str] | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        decision=action,
        confidence=confidence,
        reason=reason,
        safe_to_execute_tools=safe_to_execute_tools,
        requires_confirmation=requires_confirmation,
        response_instruction=response_instruction,
        blocked_actions=blocked_actions or [],
        flags=flags or [],
    )


def _is_vad_only_partial(state: PolicyInput, transcript: str) -> bool:
    normalized = transcript.lower().strip(". ")
    if normalized == "listening":
        return True
    return (
        bool(state.stt_fallback)
        and state.stt_provider == "local_fallback"
        and state.stt_fallback_reason == "offline speech activity detector"
    )


def _low_confidence_for_critical_workflow(
    state: PolicyInput, frame: SemanticFrame
) -> bool:
    critical = (
        state.pending_action_risk
        in {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.IRREVERSIBLE}
        or frame.intent in _RISKY_INTENTS
        or frame.requires_confirmation
    )
    return critical and (frame.confidence < 0.55 or state.stt_confidence < 0.75)


def _clear_authorisation(state: PolicyInput, frame: SemanticFrame) -> bool:
    if not frame.explicit_authorisation:
        return False
    normalized = re.sub(r"[^a-z0-9' ]+", "", state.transcript.lower()).strip()
    if normalized in _WEAK_CONFIRMATIONS:
        return False
    return True


def _is_short_backchannel(transcript: str) -> bool:
    return _normalized_transcript(transcript) in _SHORT_BACKCHANNELS


def _is_pause_control(transcript: str) -> bool:
    return _normalized_transcript(transcript) in _PAUSE_CONTROL_PHRASES


def _is_courtesy_backchannel(transcript: str) -> bool:
    return _normalized_transcript(transcript) in _COURTESY_BACKCHANNELS


def _is_soft_interjection(transcript: str, frame: SemanticFrame) -> bool:
    tokens = _soft_interjection_tokens(transcript)
    if not tokens or len(tokens) > 3:
        return False
    if all(_is_soft_interjection_token(token) for token in tokens):
        return True
    return frame.speech_act == SpeechAct.BACKCHANNEL


def _should_check_in_for_soft_interjection(state: PolicyInput) -> bool:
    tokens = _soft_interjection_tokens(state.transcript)
    if state.prior_soft_interjections_during_tts > 0:
        return True
    has_hesitation_or_elongated_backchannel = any(
        _is_hesitation_token(token) or _is_elongated_backchannel(token)
        for token in tokens
    )
    if not has_hesitation_or_elongated_backchannel:
        return False
    return len(tokens) > 1


def _soft_interjection_flags(
    transcript: str, prior_soft_interjections: int
) -> list[str]:
    tokens = _soft_interjection_tokens(transcript)
    flags = ["soft_interjection"]
    if prior_soft_interjections > 0:
        flags.append("repeated_backchannel")
    if any(_is_hesitation_token(token) for token in tokens):
        flags.append("hesitation_interjection")
    if any(_is_elongated_backchannel(token) for token in tokens):
        flags.append("elongated_backchannel")
    if any(_is_short_backchannel(token) for token in tokens):
        flags.append("backchannel")
    return flags


def _soft_interjection_tokens(transcript: str) -> list[str]:
    return _normalized_transcript(transcript).split()


def _is_soft_interjection_token(token: str) -> bool:
    return (
        _is_short_backchannel(token)
        or _is_hesitation_token(token)
        or _is_elongated_backchannel(token)
    )


def _is_standalone_hesitation(transcript: str) -> bool:
    tokens = _soft_interjection_tokens(transcript)
    return len(tokens) == 1 and _is_hesitation_token(tokens[0])


def _is_hesitation_token(token: str) -> bool:
    return bool(re.fullmatch(r"(?:u+h+|u+m+|e+r+m*|h+m{3,}|m{3,})", token))


def _is_elongated_backchannel(token: str) -> bool:
    if token in _SHORT_BACKCHANNELS:
        return False
    collapsed = re.sub(r"(.)\1+", r"\1", token)
    return collapsed in _SHORT_BACKCHANNELS


def _normalized_transcript(transcript: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", "", transcript.lower()).strip()


def _blocked_actions_for(frame: SemanticFrame) -> list[str]:
    if frame.requested_action:
        return [frame.requested_action]
    if frame.intent == Intent.ASK_CANCELLATION_FEE:
        return ["cancel_service"]
    if frame.intent in _RISKY_INTENTS:
        return [frame.intent.value]
    if "mentions_cancellation" in frame.flags:
        return ["cancel_service"]
    return []


def _risk_flags(frame: SemanticFrame, blocked_actions: list[str]) -> list[str]:
    flags: list[str] = []
    if "cancel_service" in blocked_actions:
        flags.append("mentions_cancellation")
    if frame.speech_act == SpeechAct.EXPLORATION:
        flags.append("exploratory_question")
    return flags
