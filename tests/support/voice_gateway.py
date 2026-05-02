from __future__ import annotations

from typing import TYPE_CHECKING

from services.voice_gateway.models import (
    CallLeg,
    CallSession,
    LegRole,
    SessionMode,
    SessionState,
)
from services.voice_gateway.clients import TranslationResult

if TYPE_CHECKING:
    from services.voice_gateway.main import VoiceGateway


class FakeReply:
    def __init__(self, reply_text="+OK Job-UUID: test") -> None:
        self.reply_text = reply_text

    def get(self, key, default=None):
        if key == "Reply-Text":
            return self.reply_text
        return default


class FakeFSSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.hangups: list[str] = []
        self.answered = False

    async def answer(self) -> None:
        self.answered = True

    async def send(self, command: str):
        self.sent.append(command)
        return FakeReply()

    async def hangup(self, cause: str) -> None:
        self.hangups.append(cause)


class FakeTTSClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def speak(self, fs_uuid: str, text: str, **kwargs) -> None:
        self.requests.append({"fs_uuid": fs_uuid, "text": text, **kwargs})


class MeasuredFakeTTSClient(FakeTTSClient):
    async def speak(self, fs_uuid: str, text: str, **kwargs) -> dict[str, object]:
        await super().speak(fs_uuid, text, **kwargs)
        return {
            "status": "queued",
            "fs_uuid": fs_uuid,
            "attempted_spec": "piper|en|Hello",
            "command_latency_ms": 12.5,
            "event_lock_requested": bool(kwargs.get("wait_complete")),
        }


class FakeLLMClient:
    def __init__(self, response: str = "Short answer.") -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    async def respond(
        self,
        session_id: str,
        text: str,
        history: list[dict[str, str]],
        *,
        metadata: dict[str, object] | None = None,
    ) -> str:
        self.requests.append(
            {
                "session_id": session_id,
                "text": text,
                "history": list(history),
                "metadata": metadata or {},
            }
        )
        return self.response

    async def stream_respond(
        self,
        session_id: str,
        text: str,
        history: list[dict[str, str]],
        *,
        metadata: dict[str, object] | None = None,
    ):
        self.requests.append(
            {
                "session_id": session_id,
                "text": text,
                "history": list(history),
                "metadata": metadata or {},
            }
        )
        yield {
            "type": "started",
            "session_id": session_id,
            "model": "fake",
            "provider": "fake",
        }
        yield {"type": "delta", "text": self.response}
        yield {
            "type": "completed",
            "session_id": session_id,
            "text": self.response,
            "model": "fake",
            "provider": "fake",
        }


class FakeChunkedLLMClient(FakeLLMClient):
    def __init__(self, deltas: list[str], completed_text: str | None = None) -> None:
        super().__init__(completed_text or "".join(deltas))
        self.deltas = deltas

    async def stream_respond(
        self,
        session_id: str,
        text: str,
        history: list[dict[str, str]],
        *,
        metadata: dict[str, object] | None = None,
    ):
        self.requests.append(
            {
                "session_id": session_id,
                "text": text,
                "history": list(history),
                "metadata": metadata or {},
            }
        )
        yield {
            "type": "started",
            "session_id": session_id,
            "model": "fake",
            "provider": "fake",
        }
        for delta in self.deltas:
            yield {"type": "delta", "text": delta}
        yield {
            "type": "completed",
            "session_id": session_id,
            "text": self.response,
            "model": "fake",
            "provider": "fake",
        }


class FakeTranslationClient:
    def __init__(
        self,
        response: str = "Translated response.",
        *,
        model: str = "fake-translation",
        provider: str = "fake",
    ) -> None:
        self.response = response
        self.model = model
        self.provider = provider
        self.requests: list[dict[str, object]] = []

    async def translate(
        self,
        session_id: str,
        text: str,
        *,
        source_language: str,
        target_language: str,
    ) -> str:
        self.requests.append(
            {
                "session_id": session_id,
                "text": text,
                "source_language": source_language,
                "target_language": target_language,
            }
        )
        return TranslationResult(
            text=self.response,
            model=self.model,
            provider=self.provider,
        )


class FakeControlClient:
    def __init__(self) -> None:
        self.api_commands: list[tuple[str, str | None]] = []
        self.bgapi_commands: list[tuple[str, str | None]] = []

    async def api(self, command: str, *, fs_host: str | None = None):
        self.api_commands.append((command, fs_host))
        return FakeReply()

    async def bgapi(self, command: str, *, fs_host: str | None = None):
        self.bgapi_commands.append((command, fs_host))
        return FakeReply()


class RejectingControlClient(FakeControlClient):
    async def api(self, command: str, *, fs_host: str | None = None):
        self.api_commands.append((command, fs_host))
        return FakeReply("-ERR no such channel")


async def drain_events(subscription):
    events = []
    while not subscription.queue.empty():
        events.append((await subscription.queue.get()).to_dict())
    return events


def register_assistant_session(
    gateway: VoiceGateway, *, session_id: str = "session-a", fs_uuid: str = "uuid-a"
):
    session = CallSession(
        session_id=session_id, mode=SessionMode.ASSISTANT, state=SessionState.LISTENING
    )
    session.add_leg(CallLeg(leg_id="a", fs_uuid=fs_uuid, role=LegRole.CALLER))
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid[fs_uuid] = session.session_id
    return session


def register_translation_session(gateway: VoiceGateway):
    session = CallSession(
        session_id="session-a",
        mode=SessionMode.TRANSLATION,
        state=SessionState.LISTENING,
    )
    session.add_leg(
        CallLeg(
            leg_id="a",
            fs_uuid="uuid-a",
            role=LegRole.CALLER,
            source_language="fr",
            target_language="en",
            peer_leg_id="b",
        )
    )
    session.add_leg(
        CallLeg(
            leg_id="b",
            fs_uuid="uuid-b",
            role=LegRole.PEER,
            source_language="en",
            target_language="fr",
            peer_leg_id="a",
        )
    )
    gateway.sessions[session.session_id] = session
    gateway.sessions_by_uuid["uuid-a"] = session.session_id
    gateway.sessions_by_uuid["uuid-b"] = session.session_id
    return session
