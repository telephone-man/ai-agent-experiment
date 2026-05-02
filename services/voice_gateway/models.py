"""Session models for single-leg AI calls and translated calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LegRole(str, Enum):
    CALLER = "caller"
    PEER = "peer"


class SessionMode(str, Enum):
    ASSISTANT = "assistant"
    TRANSLATION = "translation"


class SessionState(str, Enum):
    NEW = "new"
    ANSWERED = "answered"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ENDED = "ended"


@dataclass(slots=True)
class CallLeg:
    leg_id: str
    fs_uuid: str
    role: LegRole = LegRole.CALLER
    aor: str | None = None
    source_language: str = "en"
    target_language: str = "en"
    media_stream_id: str | None = None
    peer_leg_id: str | None = None
    is_active: bool = True


@dataclass(slots=True)
class CallSession:
    session_id: str
    mode: SessionMode = SessionMode.ASSISTANT
    state: SessionState = SessionState.NEW
    legs: dict[str, CallLeg] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def primary_leg(self) -> CallLeg:
        if not self.legs:
            raise ValueError("session has no legs")
        return next(iter(self.legs.values()))

    def add_leg(self, leg: CallLeg) -> None:
        self.legs[leg.leg_id] = leg

    def leg_by_uuid(self, fs_uuid: str) -> CallLeg | None:
        for leg in self.legs.values():
            if leg.fs_uuid == fs_uuid:
                return leg
        return None

    def peer_for(self, leg: CallLeg) -> CallLeg | None:
        if not leg.peer_leg_id:
            return None
        return self.legs.get(leg.peer_leg_id)
