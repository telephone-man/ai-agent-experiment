"""Turn-taking and interruption classification for voice-agent calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TurnAction(str, Enum):
    IGNORE = "ignore"
    PARTIAL = "partial"
    BACKCHANNEL = "backchannel"
    USER_TURN = "user_turn"
    GOODBYE = "goodbye"


@dataclass(slots=True)
class TurnDecision:
    action: TurnAction
    text: str
    should_interrupt: bool = False


class SemanticTurnDetector:
    """Small deterministic gate before final transcripts reach the LLM."""

    _backchannels = {
        "ah",
        "aha",
        "bueno",
        "hmm",
        "hm",
        "mm",
        "mhmm",
        "mhm",
        "okay",
        "ok",
        "right",
        "si",
        "vale",
        "oui",
        "yeah",
        "yep",
        "yes",
    }
    _goodbye = re.compile(
        r"\b(bye|goodbye|hang up|end the call|that's all|that is all|"
        r"adios|au revoir)\b",
        re.I,
    )

    def decide(
        self,
        text: str,
        *,
        is_final: bool,
        confidence: float = 1.0,
        agent_is_speaking: bool = False,
    ) -> TurnDecision:
        cleaned = " ".join((text or "").strip().split())
        if not cleaned:
            return TurnDecision(TurnAction.IGNORE, cleaned)
        if not is_final:
            return TurnDecision(
                TurnAction.PARTIAL,
                cleaned,
                should_interrupt=agent_is_speaking and len(cleaned) > 12,
            )
        if confidence < 0.35:
            return TurnDecision(TurnAction.IGNORE, cleaned)

        normalized = re.sub(r"[^a-z0-9' ]+", "", cleaned.lower()).strip()
        if normalized in self._backchannels or len(normalized) <= 2:
            return TurnDecision(TurnAction.BACKCHANNEL, cleaned)
        if self._goodbye.search(cleaned):
            return TurnDecision(TurnAction.GOODBYE, cleaned, should_interrupt=True)
        return TurnDecision(
            TurnAction.USER_TURN, cleaned, should_interrupt=agent_is_speaking
        )
