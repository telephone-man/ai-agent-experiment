"""Validated schemas for voice semantic interpretation and local policy."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SpeechAct(str, Enum):
    QUESTION = "question"
    REQUEST = "request"
    CORRECTION = "correction"
    CONFIRMATION = "confirmation"
    REJECTION = "rejection"
    BACKCHANNEL = "backchannel"
    INTERRUPTION = "interruption"
    SIDE_TALK = "side_talk"
    GOODBYE = "goodbye"
    EXPLORATION = "exploration"
    UNKNOWN = "unknown"


class Intent(str, Enum):
    ASK_INVOICE = "ask_invoice"
    PAY_INVOICE = "pay_invoice"
    ASK_CANCELLATION_FEE = "ask_cancellation_fee"
    CANCEL_SERVICE = "cancel_service"
    CHANGE_DIRECT_DEBIT = "change_direct_debit"
    UPDATE_DETAILS = "update_details"
    COMPLAINT = "complaint"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    IRREVERSIBLE = "irreversible"


class PolicyAction(str, Enum):
    WAIT = "WAIT"
    CLARIFY = "CLARIFY"
    RESPOND = "RESPOND"
    SUPPRESS = "SUPPRESS"
    CANCEL_TTS_AND_LISTEN = "CANCEL_TTS_AND_LISTEN"
    SOFT_INTERRUPT_CHECKIN = "SOFT_INTERRUPT_CHECKIN"
    CONFIRM_BEFORE_ACTION = "CONFIRM_BEFORE_ACTION"
    REJECT_TOOL_EXECUTION = "REJECT_TOOL_EXECUTION"
    ESCALATE = "ESCALATE"
    END_CALL = "END_CALL"


class SemanticFrame(BaseModel):
    """The semantic meaning proposed by an interpreter.

    This model is intentionally strict because LLM output must be validated
    before local policy uses it.
    """

    model_config = ConfigDict(extra="forbid")

    addressed_to_agent: bool = True
    utterance_complete: bool = True
    speech_act: SpeechAct = SpeechAct.UNKNOWN
    intent: Intent = Intent.UNKNOWN
    risky_action_mentioned: bool = False
    requested_action: str | None = None
    explicit_authorisation: bool = False
    requires_confirmation: bool = False
    slots: dict[str, Any] = Field(default_factory=dict)
    discarded_slots: dict[str, Any] = Field(default_factory=dict)
    correction_detected: bool = False
    goodbye_detected: bool = False
    continue_conversation: bool = True
    clarification_needed: bool = False
    clarification_type: str | None = Field(default=None, max_length=80)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=280)
    flags: list[str] = Field(default_factory=list)

    @field_validator("rationale", "clarification_type")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(str(value or "").split())

    @field_validator("flags")
    @classmethod
    def normalize_flags(cls, value: list[str]) -> list[str]:
        return [str(flag).strip() for flag in value if str(flag).strip()]

    @classmethod
    def unknown(
        cls,
        *,
        rationale: str = "Unable to classify utterance.",
        flags: list[str] | None = None,
    ) -> "SemanticFrame":
        return cls(
            addressed_to_agent=True,
            utterance_complete=True,
            speech_act=SpeechAct.UNKNOWN,
            intent=Intent.UNKNOWN,
            confidence=0.0,
            rationale=rationale,
            flags=flags or ["semantic_unknown"],
        )


class PolicyInput(BaseModel):
    """Call state used by the local policy adjudicator."""

    model_config = ConfigDict(extra="allow")

    scenario_id: str | None = None
    session_id: str = "demo"
    turn_id: str = "turn_001"
    transcript: str = ""
    is_partial: bool = False
    stt_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    stt_provider: str | None = None
    stt_type: str | None = None
    stt_fallback: bool = False
    stt_fallback_reason: str | None = None
    current_flow: str | None = None
    last_agent_message: str | None = None
    agent_is_speaking: bool = False
    tts_allow_interruptions: bool = True
    prior_soft_interjections_during_tts: int = Field(default=0, ge=0)
    previous_assistant_delivery_pending: bool = False
    delivery_resume_pending: bool = False
    clarification_due: bool = False
    turn_hold_elapsed_ms: float | None = Field(default=None, ge=0.0)
    pending_action: str | None = None
    pending_action_risk: RiskLevel = RiskLevel.NONE
    known_slots: dict[str, Any] = Field(default_factory=dict)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("transcript", "session_id", "turn_id", mode="before")
    @classmethod
    def coerce_text(cls, value: Any) -> str:
        return str(value or "")


class PolicyDecision(BaseModel):
    """Final local routing/control decision.

    This object is owned by deterministic local policy code. LLM output may
    inform a SemanticFrame, but it does not own this decision.
    """

    model_config = ConfigDict(extra="forbid")

    decision: PolicyAction
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=320)
    safe_to_execute_tools: bool = False
    requires_confirmation: bool = False
    response_instruction: str | None = Field(default=None, max_length=600)
    blocked_actions: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    @field_validator("reason", "response_instruction")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(str(value or "").split())

    @field_validator("blocked_actions", "flags")
    @classmethod
    def normalize_string_list(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]
