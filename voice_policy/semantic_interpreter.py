"""Shared semantic interpreter interfaces."""

from __future__ import annotations

from typing import Protocol

from voice_policy.schema import PolicyInput, SemanticFrame


class SemanticInterpreter(Protocol):
    def interpret(self, policy_input: PolicyInput | dict) -> SemanticFrame:
        """Return a validated semantic frame for the supplied policy input."""


def coerce_policy_input(policy_input: PolicyInput | dict) -> PolicyInput:
    if isinstance(policy_input, PolicyInput):
        return policy_input
    return PolicyInput.model_validate(policy_input)
