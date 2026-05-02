"""Admission and provider circuit reliability helpers for the voice gateway."""

from __future__ import annotations

import time
from dataclasses import dataclass


DEFAULT_MAX_ACTIVE_SESSIONS = 4
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 3
DEFAULT_CIRCUIT_RESET_SECONDS = 30.0


@dataclass(slots=True)
class AdmissionDecision:
    accepted: bool
    session_id: str
    active_sessions: int
    max_active_sessions: int
    reason: str


class AdmissionController:
    def __init__(
        self, *, max_active_sessions: int = DEFAULT_MAX_ACTIVE_SESSIONS
    ) -> None:
        self.max_active_sessions = max(1, int(max_active_sessions))
        self._active_session_ids: set[str] = set()

    def try_acquire(self, session_id: str) -> AdmissionDecision:
        clean_session_id = str(session_id or "").strip()
        if clean_session_id in self._active_session_ids:
            return AdmissionDecision(
                accepted=True,
                session_id=clean_session_id,
                active_sessions=len(self._active_session_ids),
                max_active_sessions=self.max_active_sessions,
                reason="already_active",
            )
        if len(self._active_session_ids) >= self.max_active_sessions:
            return AdmissionDecision(
                accepted=False,
                session_id=clean_session_id,
                active_sessions=len(self._active_session_ids),
                max_active_sessions=self.max_active_sessions,
                reason="max_active_sessions_reached",
            )
        self._active_session_ids.add(clean_session_id)
        return AdmissionDecision(
            accepted=True,
            session_id=clean_session_id,
            active_sessions=len(self._active_session_ids),
            max_active_sessions=self.max_active_sessions,
            reason="capacity_available",
        )

    def release(self, session_id: str) -> AdmissionDecision:
        clean_session_id = str(session_id or "").strip()
        self._active_session_ids.discard(clean_session_id)
        return AdmissionDecision(
            accepted=True,
            session_id=clean_session_id,
            active_sessions=len(self._active_session_ids),
            max_active_sessions=self.max_active_sessions,
            reason="released",
        )


class ProviderCircuitOpenError(RuntimeError):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"{provider} provider circuit is open")


class ProviderCircuitBreaker:
    def __init__(
        self,
        provider: str,
        *,
        failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
        reset_seconds: float = DEFAULT_CIRCUIT_RESET_SECONDS,
    ) -> None:
        self.provider = provider
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_seconds = max(0.0, float(reset_seconds))
        self.failure_count = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def allow(self) -> tuple[bool, str | None]:
        if self.opened_at is None:
            return True, None
        if time.perf_counter() - self.opened_at >= self.reset_seconds:
            self.opened_at = None
            self.failure_count = 0
            return True, "provider.circuit_closed"
        return False, "provider.circuit_blocked"

    def record_success(self) -> str | None:
        if not self.failure_count and self.opened_at is None:
            return None
        self.failure_count = 0
        was_open = self.opened_at is not None
        self.opened_at = None
        return "provider.circuit_closed" if was_open else None

    def record_failure(self) -> str | None:
        if self.opened_at is not None:
            return None
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.opened_at = time.perf_counter()
            return "provider.circuit_opened"
        return None
