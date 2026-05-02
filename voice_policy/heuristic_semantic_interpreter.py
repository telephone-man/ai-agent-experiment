"""Deterministic offline semantic interpreter for demos and regression evals."""

from __future__ import annotations

import re

from voice_policy.schema import Intent, SemanticFrame, SpeechAct
from voice_policy.semantic_interpreter import coerce_policy_input


_BACKCHANNELS = {
    "ah",
    "aha",
    "hmm",
    "hm",
    "mm",
    "mhmm",
    "mhm",
    "okay",
    "ok",
    "right",
    "yeah",
    "yep",
    "yes",
    "sure",
}
_GOODBYE_RE = re.compile(
    r"\b(bye|goodbye|hang up|end the call|that's all|that is all|thanks bye)\b", re.I
)
_THANKS_CONTINUE_RE = re.compile(r"\b(thanks|thank you|cheers)\b.+\?", re.I)
_INTERRUPT_RE = re.compile(
    r"\b(stop|wait|hang on|hold on|interrupt|can i interrupt|you(?:'re| are) still|"
    r"no that's wrong|that's wrong|actually stop|pause)\b",
    re.I,
)
_SIDE_TALK_RE = re.compile(
    r"\b(what's our|what is our|ask (him|her|them)|does anyone know|can you check)\b",
    re.I,
)
_INCOMPLETE_RE = re.compile(
    r"\b(um|uh|let me think|one second|hang on|just a sec|actually)$", re.I
)
_QUESTION_INVITE_RE = re.compile(
    r"\b(go ahead and ask|ask (?:me )?(?:your )?question|what(?:'s| is) your question|how can i help)\b",
    re.I,
)
_BARE_QUESTION_WORDS = {"why", "what", "how", "when", "where", "who", "which"}
_INCOMPLETE_TRAILING_WORDS = {
    "a",
    "an",
    "the",
    "my",
    "your",
    "our",
    "their",
    "is",
    "are",
    "was",
    "were",
    "am",
    "be",
    "been",
    "being",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "will",
    "shall",
    "may",
    "might",
    "must",
    "has",
    "have",
    "had",
    "to",
    "of",
    "for",
    "with",
    "about",
    "at",
    "in",
    "on",
    "from",
    "by",
}
_DETERMINERS = {
    "a",
    "an",
    "the",
    "my",
    "your",
    "our",
    "their",
}
_COPULA_WORDS = {"am", "are", "be", "been", "being", "is", "was", "were"}
_TRAILING_PREPOSITIONS = {
    "about",
    "at",
    "by",
    "for",
    "from",
    "in",
    "on",
    "to",
    "with",
}
_SHORT_COMPLETE_WH_PHRASES = {
    ("why", "not"),
    ("what", "now"),
    ("what", "else"),
    ("how", "so"),
}
_QUESTION_PREAMBLE_PREFIXES = (
    ("let", "me", "ask"),
    ("lemme", "ask"),
    ("can", "i", "ask"),
    ("could", "i", "ask"),
    ("can", "you", "tell", "me"),
    ("could", "you", "tell", "me"),
    ("would", "you", "tell", "me"),
    ("can", "you", "explain"),
    ("could", "you", "explain"),
    ("i", "want", "to", "ask"),
    ("i", "need", "to", "ask"),
    ("i", "wanted", "to", "ask"),
    ("i", "was", "going", "to", "ask"),
    ("i", "am", "going", "to", "ask"),
    ("i'm", "going", "to", "ask"),
    ("i", "would", "like", "to", "ask"),
    ("i'd", "like", "to", "ask"),
    ("my", "question", "is"),
    ("the", "question", "is"),
)
_QUESTION_PREAMBLE_LEAD_INS = {"actually", "ok", "okay", "right", "so", "well"}
_STATE_CHANGING_INTENTS = {
    Intent.PAY_INVOICE,
    Intent.CANCEL_SERVICE,
    Intent.CHANGE_DIRECT_DEBIT,
    Intent.UPDATE_DETAILS,
}
_ACTION_REQUEST_RE = re.compile(
    r"\b(can you|could you|please|go ahead and|i need to|i want to|"
    r"i'd like to|i would like to|i want you to)\b",
    re.I,
)


class HeuristicSemanticInterpreter:
    """Small rule-based interpreter used where no LLM should be required."""

    def interpret(self, policy_input) -> SemanticFrame:
        state = coerce_policy_input(policy_input)
        text = " ".join(state.transcript.strip().split())
        normalized = re.sub(r"[^a-z0-9' ]+", "", text.lower()).strip()
        flags: list[str] = ["heuristic"]

        if not text:
            return SemanticFrame(
                addressed_to_agent=True,
                utterance_complete=False,
                speech_act=SpeechAct.UNKNOWN,
                intent=Intent.UNKNOWN,
                confidence=0.3,
                rationale="No transcript text was available.",
                flags=flags,
            )

        if state.is_partial:
            return SemanticFrame(
                addressed_to_agent=True,
                utterance_complete=False,
                speech_act=SpeechAct.INTERRUPTION
                if _INTERRUPT_RE.search(text)
                else SpeechAct.UNKNOWN,
                intent=Intent.UNKNOWN,
                confidence=0.6,
                rationale="Partial transcript is not sent to full semantic judgement.",
                flags=[*flags, "partial"],
            )

        if normalized in _BACKCHANNELS:
            return SemanticFrame(
                addressed_to_agent=True,
                utterance_complete=True,
                speech_act=SpeechAct.BACKCHANNEL,
                intent=Intent.UNKNOWN,
                confidence=0.92,
                rationale="Short acknowledgement without a request.",
                flags=[*flags, "backchannel"],
            )

        if _SIDE_TALK_RE.search(text):
            return SemanticFrame(
                addressed_to_agent=False,
                utterance_complete=True,
                speech_act=SpeechAct.SIDE_TALK,
                intent=Intent.UNKNOWN,
                confidence=0.78,
                rationale="Utterance appears directed to someone nearby.",
                flags=[*flags, "side_talk"],
            )

        incomplete_flags = self._incomplete_flags(state, text, normalized)
        if incomplete_flags:
            clarification_type = self._clarification_type_for_incomplete(
                normalized, incomplete_flags
            )
            return SemanticFrame(
                addressed_to_agent=True,
                utterance_complete=False,
                speech_act=SpeechAct.UNKNOWN,
                intent=Intent.UNKNOWN,
                clarification_needed=clarification_type is not None,
                clarification_type=clarification_type,
                confidence=0.74,
                rationale="Utterance appears mid-thought.",
                flags=[*flags, *incomplete_flags],
            )

        if _INTERRUPT_RE.search(text):
            return SemanticFrame(
                addressed_to_agent=True,
                utterance_complete=True,
                speech_act=SpeechAct.INTERRUPTION,
                intent=self._intent_for(text),
                confidence=0.86,
                rationale="User is interrupting or objecting to current speech.",
                flags=[*flags, "interruption"],
            )

        cancellation_scope_correction = self._detect_cancellation_scope_correction(text)
        if cancellation_scope_correction:
            corrected, discarded = cancellation_scope_correction
            return SemanticFrame(
                addressed_to_agent=True,
                utterance_complete=True,
                speech_act=SpeechAct.CORRECTION,
                intent=Intent.CANCEL_SERVICE,
                risky_action_mentioned=True,
                explicit_authorisation=False,
                requires_confirmation=True,
                slots={"cancellation_scope": corrected},
                discarded_slots={"cancellation_scope": discarded},
                correction_detected=True,
                confidence=0.84,
                rationale=f"User corrected cancellation scope from {discarded} to {corrected}.",
                flags=[*flags, "self_correction", "mentions_cancellation"],
            )

        correction = self._detect_payment_date_correction(text)
        if correction:
            corrected, discarded = correction
            return SemanticFrame(
                addressed_to_agent=True,
                utterance_complete=True,
                speech_act=SpeechAct.CORRECTION,
                intent=Intent.PAY_INVOICE
                if "invoice" in normalized or "pay" in normalized
                else self._intent_for(text),
                risky_action_mentioned="pay" in normalized,
                explicit_authorisation=self._has_explicit_authorisation(text),
                requires_confirmation=True,
                slots={"payment_date": corrected},
                discarded_slots={"payment_date": discarded},
                correction_detected=True,
                confidence=0.87,
                rationale=f"User corrected {discarded} to {corrected}.",
                flags=[*flags, "self_correction"],
            )

        goodbye_detected = bool(_GOODBYE_RE.search(text)) or normalized in {
            "thanks bye",
            "thank you bye",
        }
        continue_conversation = not goodbye_detected or bool(
            _THANKS_CONTINUE_RE.search(text)
        )
        intent = self._intent_for(text)
        risky_action_mentioned = self._risky_action_mentioned(text, intent)
        explicit_authorisation = self._has_explicit_authorisation(text)
        speech_act = self._speech_act_for(
            text,
            intent,
            risky_action_mentioned,
            goodbye_detected,
            continue_conversation,
        )

        return SemanticFrame(
            addressed_to_agent=True,
            utterance_complete=True,
            speech_act=speech_act,
            intent=intent,
            risky_action_mentioned=risky_action_mentioned,
            requested_action=self._requested_action_for(intent, explicit_authorisation),
            explicit_authorisation=explicit_authorisation,
            requires_confirmation=self._requires_confirmation(
                intent, explicit_authorisation
            ),
            slots=self._slots_for(text),
            correction_detected=False,
            goodbye_detected=goodbye_detected
            or "thanks" in normalized
            or "thank you" in normalized,
            continue_conversation=continue_conversation,
            confidence=0.82
            if intent != Intent.UNKNOWN or speech_act != SpeechAct.UNKNOWN
            else 0.55,
            rationale=self._rationale_for(
                speech_act, intent, risky_action_mentioned, explicit_authorisation
            ),
            flags=[
                *flags,
                *self._flags_for(
                    text,
                    risky_action_mentioned,
                    explicit_authorisation,
                    continue_conversation,
                ),
            ],
        )

    def _incomplete_flags(self, state, text: str, normalized: str) -> list[str]:
        flags: list[str] = []
        stripped = text.strip()
        if _INCOMPLETE_RE.search(stripped):
            flags.append("incomplete")
        if stripped.endswith("..."):
            flags.append("incomplete")
        if any(
            normalized.endswith(fragment)
            for fragment in ("i was wondering if", "can you", "could you", "i need to")
        ):
            flags.append("incomplete")

        question_body = self._question_body_after_preamble(normalized)
        if question_body is not None:
            is_empty_preamble = not question_body or question_body in (
                ["a", "question"],
                ["the", "question"],
                ["question"],
            )
            is_bare_question_word = (
                len(question_body) == 1 and question_body[0] in _BARE_QUESTION_WORDS
            )
            if is_empty_preamble or is_bare_question_word:
                flags.append("question_preamble")
                if is_bare_question_word:
                    flags.append("bare_question_word")
                if _QUESTION_INVITE_RE.search(str(state.last_agent_message or "")):
                    flags.append("awaiting_question_body")
            elif self._appears_incomplete_embedded_question_body(
                question_body, explicit_question=stripped.endswith("?")
            ):
                flags.extend(["question_preamble", "incomplete_wh_clause"])

        if self._appears_incomplete_wh_clause(
            normalized, explicit_question=stripped.endswith("?")
        ):
            flags.append("incomplete_wh_clause")

        deduped: list[str] = []
        for flag in flags:
            if flag not in deduped:
                deduped.append(flag)
        return deduped

    def _clarification_type_for_incomplete(
        self, normalized: str, flags: list[str]
    ) -> str | None:
        if not {
            "question_preamble",
            "bare_question_word",
            "incomplete_wh_clause",
        }.intersection(flags):
            return None
        question_body = self._question_body_after_preamble(normalized)
        if "bare_question_word" in flags:
            if question_body == ["why"] or normalized == "why":
                return "bare_why"
            return "bare_question_word"
        if question_body is not None and (
            not question_body
            or question_body in (["a", "question"], ["the", "question"], ["question"])
        ):
            return "empty_question_preamble"
        if "incomplete_wh_clause" in flags:
            return "incomplete_wh_clause"
        if "question_preamble" in flags:
            return "empty_question_preamble"
        return None

    def _question_body_after_preamble(self, normalized: str) -> list[str] | None:
        words = normalized.split()
        while words and words[0] in _QUESTION_PREAMBLE_LEAD_INS:
            words = words[1:]
        for prefix in _QUESTION_PREAMBLE_PREFIXES:
            if tuple(words[: len(prefix)]) != prefix:
                continue
            body = words[len(prefix) :]
            if body[:1] == ["you"]:
                body = body[1:]
            return body
        return None

    def _appears_incomplete_embedded_question_body(
        self, words: list[str], *, explicit_question: bool = False
    ) -> bool:
        if not words:
            return True
        if len(words) == 1 and words[0] in _BARE_QUESTION_WORDS:
            return True
        if explicit_question:
            return False
        if words[0] not in _BARE_QUESTION_WORDS:
            return False
        if tuple(words) in _SHORT_COMPLETE_WH_PHRASES:
            return False
        if len(words) <= 1:
            return True
        if words[-1] in _TRAILING_PREPOSITIONS and any(
            self._looks_like_predicate_token(word) for word in words[1:-1]
        ):
            return False
        if words[-1] in _INCOMPLETE_TRAILING_WORDS:
            if (
                words[-1] in _COPULA_WORDS
                and words[0] != "why"
                and len(words) >= 4
            ):
                return False
            return True
        if (
            words[0] == "why"
            and len(words) == 4
            and words[1] in _INCOMPLETE_TRAILING_WORDS
            and words[2] in _DETERMINERS
        ):
            return True
        if len(words) <= 3 and words[1] in _DETERMINERS:
            return True
        if len(words) == 4 and words[1] in _DETERMINERS:
            return not self._looks_like_predicate_token(words[-1])
        return False

    def _appears_incomplete_wh_clause(
        self, normalized: str, *, explicit_question: bool = False
    ) -> bool:
        if explicit_question:
            return False
        words = normalized.split()
        if not words or words[0] not in _BARE_QUESTION_WORDS:
            return False
        if tuple(words) in _SHORT_COMPLETE_WH_PHRASES:
            return False
        if len(words) <= 1:
            return True
        if words[-1] in _INCOMPLETE_TRAILING_WORDS:
            return True
        if (
            words[0] == "why"
            and len(words) == 4
            and words[1] in _INCOMPLETE_TRAILING_WORDS
            and words[2] in _DETERMINERS
        ):
            return True
        if (
            len(words) == 4
            and words[1] in _DETERMINERS
            and not self._looks_like_predicate_token(words[-1])
        ):
            return True
        return len(words) <= 3 and words[1] in _DETERMINERS

    def _looks_like_predicate_token(self, word: str) -> bool:
        return (
            word in _COPULA_WORDS
            or word.endswith("ed")
            or (len(word) > 3 and word.endswith("s"))
        )

    def _intent_for(self, text: str) -> Intent:
        lowered = text.lower()
        if "cancellation fee" in lowered or ("cancel" in lowered and "fee" in lowered):
            return Intent.ASK_CANCELLATION_FEE
        if "cancel" in lowered and any(
            word in lowered
            for word in ("service", "broadband", "account", "subscription")
        ):
            return Intent.CANCEL_SERVICE
        if "direct debit" in lowered:
            return Intent.CHANGE_DIRECT_DEBIT
        if any(
            word in lowered for word in ("update my", "change my address", "details")
        ):
            return Intent.UPDATE_DETAILS
        if "complaint" in lowered or "complain" in lowered:
            return Intent.COMPLAINT
        if "invoice" in lowered or "bill" in lowered or "account number" in lowered:
            if "pay" in lowered or "payment" in lowered:
                return Intent.PAY_INVOICE
            return Intent.ASK_INVOICE
        return Intent.UNKNOWN

    def _speech_act_for(
        self,
        text: str,
        intent: Intent,
        risky_action_mentioned: bool,
        goodbye_detected: bool,
        continue_conversation: bool,
    ) -> SpeechAct:
        lowered = text.lower()
        normalized = re.sub(r"[^a-z0-9' ]+", "", lowered).strip()
        if goodbye_detected and not continue_conversation:
            return SpeechAct.GOODBYE
        if normalized in _BACKCHANNELS:
            return SpeechAct.BACKCHANNEL
        if self._is_direct_action_request(lowered, intent):
            return SpeechAct.REQUEST
        if "?" in text:
            return (
                SpeechAct.EXPLORATION if risky_action_mentioned else SpeechAct.QUESTION
            )
        if intent == Intent.ASK_CANCELLATION_FEE:
            return SpeechAct.EXPLORATION
        if any(
            phrase in lowered
            for phrase in ("i need to", "please", "can you", "could you")
        ):
            return SpeechAct.REQUEST
        if normalized in {"yes", "yeah", "yep", "sure", "ok", "okay"}:
            return SpeechAct.CONFIRMATION
        return SpeechAct.UNKNOWN if intent == Intent.UNKNOWN else SpeechAct.QUESTION

    def _is_direct_action_request(self, lowered: str, intent: Intent) -> bool:
        if intent not in _STATE_CHANGING_INTENTS:
            return False
        return bool(_ACTION_REQUEST_RE.search(lowered))

    def _risky_action_mentioned(self, text: str, intent: Intent) -> bool:
        lowered = text.lower()
        return intent in {
            Intent.PAY_INVOICE,
            Intent.CANCEL_SERVICE,
            Intent.CHANGE_DIRECT_DEBIT,
            Intent.UPDATE_DETAILS,
            Intent.ASK_CANCELLATION_FEE,
        } or any(
            word in lowered for word in ("cancel", "payment", "pay", "direct debit")
        )

    def _has_explicit_authorisation(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            phrase in lowered
            for phrase in (
                "yes cancel",
                "please cancel",
                "go ahead and cancel",
                "authorise",
                "authorize",
                "i confirm",
                "make the payment",
                "please pay",
                "go ahead and pay",
            )
        )

    def _requires_confirmation(
        self, intent: Intent, explicit_authorisation: bool
    ) -> bool:
        return (
            intent
            in {
                Intent.PAY_INVOICE,
                Intent.CANCEL_SERVICE,
                Intent.CHANGE_DIRECT_DEBIT,
                Intent.UPDATE_DETAILS,
            }
            and not explicit_authorisation
        )

    def _requested_action_for(
        self, intent: Intent, explicit_authorisation: bool
    ) -> str | None:
        if not explicit_authorisation:
            return None
        if intent in {
            Intent.PAY_INVOICE,
            Intent.CANCEL_SERVICE,
            Intent.CHANGE_DIRECT_DEBIT,
            Intent.UPDATE_DETAILS,
        }:
            return intent.value
        return None

    def _detect_payment_date_correction(self, text: str) -> tuple[str, str] | None:
        lowered = text.lower()
        days = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
        match = re.search(
            rf"\b({days})\b.*\b(no|sorry|actually|rather|i mean)\b.*\b({days})(?:\s+(morning|afternoon|evening))?\b",
            lowered,
        )
        if not match:
            return None
        discarded = match.group(1).title()
        corrected = match.group(3).title()
        if match.group(4):
            corrected = f"{corrected} {match.group(4)}"
        return corrected, discarded

    def _detect_cancellation_scope_correction(
        self, text: str
    ) -> tuple[str, str] | None:
        lowered = text.lower()
        if "cancel" not in lowered or "account" not in lowered:
            return None
        if not re.search(r"\b(actually|just|rather|i mean|sorry)\b", lowered):
            return None
        if re.search(r"\b(add[- ]?on|addon)\b", lowered):
            return "add-on", "account"
        return None

    def _slots_for(self, text: str) -> dict[str, str]:
        slots: dict[str, str] = {}
        invoice_match = re.search(
            r"\b(?:invoice|account)\s*(?:number|id)?\s*([A-Z]{2,}\d{2,}|\d{4,})\b",
            text,
            re.I,
        )
        if invoice_match:
            slots["invoice_id"] = invoice_match.group(1).upper()
        service_match = re.search(
            r"\b(broadband|mobile|phone|landline|subscription)\b", text, re.I
        )
        if service_match:
            slots["service"] = service_match.group(1).lower()
        return slots

    def _rationale_for(
        self,
        speech_act: SpeechAct,
        intent: Intent,
        risky_action_mentioned: bool,
        explicit_authorisation: bool,
    ) -> str:
        if (
            speech_act == SpeechAct.EXPLORATION
            and risky_action_mentioned
            and not explicit_authorisation
        ):
            return "User mentions a risky action while asking for information."
        if speech_act == SpeechAct.GOODBYE:
            return "User appears to be ending the call."
        if intent == Intent.UNKNOWN:
            return "No high-confidence business intent was detected."
        return f"Detected {intent.value} as a {speech_act.value}."

    def _flags_for(
        self,
        text: str,
        risky_action_mentioned: bool,
        explicit_authorisation: bool,
        continue_conversation: bool,
    ) -> list[str]:
        flags: list[str] = []
        lowered = text.lower()
        if risky_action_mentioned:
            flags.append("risky_action_mentioned")
        if "cancel" in lowered:
            flags.append("mentions_cancellation")
        if risky_action_mentioned and not explicit_authorisation:
            flags.append("no_explicit_authorisation")
        if "thanks" in lowered or "thank you" in lowered:
            flags.append("thanks")
        if continue_conversation and ("thanks" in lowered or "thank you" in lowered):
            flags.append("continues_after_thanks")
        return flags
