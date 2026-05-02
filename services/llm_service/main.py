"""LLM microservice for voice turns."""

from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.common.config import (
    ai_service_degraded_payload,
    ai_service_ready,
    env_bool,
    env_int,
    offline_fallback_enabled,
)

from .mock_weather import (
    WeatherToolRequest,
    WeatherToolResponse,
    mock_weather_lookup as _mock_weather_lookup,
    mock_weather_stream_events as _mock_weather_stream_events,
    normalise_short_utterance,
    weather_answer as _weather_answer,
    weather_request_for_text,
)


class RespondRequest(BaseModel):
    session_id: str
    text: str
    history: list[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RespondResponse(BaseModel):
    session_id: str
    text: str
    model: str
    provider: str


class TranslateRequest(BaseModel):
    session_id: str
    text: str
    source_language: str
    target_language: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TranslateResponse(BaseModel):
    session_id: str
    text: str
    source_language: str
    target_language: str
    model: str
    provider: str


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("llm_service")

DEFAULT_LLM_MAX_RESPONSE_WORDS = 24
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 120
DEFAULT_LLM_HISTORY_MESSAGES = 6
DEFAULT_TRANSLATION_MAX_OUTPUT_TOKENS = 96
DELIVERY_PROMPT_TEXT_LIMIT = 500

FAST_PATH_GREETINGS = {
    "hello",
    "hello there",
    "hey",
    "hi",
    "hi there",
    "hiya",
    "good morning",
    "good afternoon",
    "good evening",
}
FAST_PATH_THANKS = {
    "cheers",
    "thanks",
    "thanks a lot",
    "thank you",
    "thank you very much",
}

_openai_client: Any | None = None
_openai_client_api_key: str | None = None


async def _close_openai_client() -> None:
    global _openai_client, _openai_client_api_key
    client = _openai_client
    _openai_client = None
    _openai_client_api_key = None
    if client is not None and hasattr(client, "close"):
        result = client.close()
        if inspect.isawaitable(result):
            await result


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        _get_openai_client(api_key)
    try:
        yield
    finally:
        await _close_openai_client()


app = FastAPI(title="LLM Service", lifespan=lifespan)


@app.get("/health")
async def health():
    if not ai_service_ready():
        return JSONResponse(
            status_code=503,
            content=ai_service_degraded_payload(),
        )
    return {"status": "ok"}


def _upstream_failure_fallback_enabled() -> bool:
    return env_bool("LLM_FALLBACK_ON_UPSTREAM_ERROR", False)


def _fast_path_responses_enabled() -> bool:
    return env_bool("LLM_FAST_PATH_RESPONSES", False)


def _response_word_limit() -> int:
    return max(
        8, env_int("OPENAI_LLM_MAX_RESPONSE_WORDS", DEFAULT_LLM_MAX_RESPONSE_WORDS)
    )


def _response_token_limit() -> int:
    return max(
        64, env_int("OPENAI_LLM_MAX_OUTPUT_TOKENS", DEFAULT_LLM_MAX_OUTPUT_TOKENS)
    )


def _translation_model() -> str:
    return (
        (os.getenv("OPENAI_TRANSLATION_MODEL") or "gpt-5-nano").strip()
        or "gpt-5-nano"
    )


def _translation_token_limit() -> int:
    return max(
        24,
        env_int(
            "OPENAI_TRANSLATION_MAX_OUTPUT_TOKENS",
            DEFAULT_TRANSLATION_MAX_OUTPUT_TOKENS,
        ),
    )


def _history_message_limit() -> int:
    return max(0, env_int("OPENAI_LLM_HISTORY_MESSAGES", DEFAULT_LLM_HISTORY_MESSAGES))


def _get_openai_client(api_key: str) -> Any:
    global _openai_client, _openai_client_api_key
    if _openai_client is None or _openai_client_api_key != api_key:
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(api_key=api_key)
        _openai_client_api_key = api_key
    return _openai_client


def _local_response(request: RespondRequest) -> RespondResponse:
    return RespondResponse(
        session_id=request.session_id,
        text=f"I heard you say: {request.text}",
        model="mock",
        provider="local",
    )


def _normalise_short_utterance(text: str) -> str:
    return normalise_short_utterance(text)


def _fast_path_response_text(request: RespondRequest) -> str | None:
    if not _fast_path_responses_enabled():
        return None
    if _delivery_prompt(request.metadata):
        return None
    normalised = _normalise_short_utterance(request.text)
    if normalised in FAST_PATH_GREETINGS and not request.history:
        return "Hi, how can I help?"
    if normalised in FAST_PATH_THANKS:
        return "You're welcome."
    return None


def _fast_path_response(request: RespondRequest, text: str) -> RespondResponse:
    return RespondResponse(
        session_id=request.session_id,
        text=text,
        model="fast-path",
        provider="local_fast_path",
    )


def _local_translation(request: TranslateRequest) -> TranslateResponse:
    return TranslateResponse(
        session_id=request.session_id,
        text=f"[{request.target_language}] {request.text}",
        source_language=request.source_language,
        target_language=request.target_language,
        model="mock",
        provider="local",
    )


def _weather_request_for_text(request: RespondRequest) -> WeatherToolRequest | None:
    return weather_request_for_text(request.session_id, request.text)


def _policy_prompt(metadata: dict[str, Any]) -> str:
    policy = metadata.get("policy") if isinstance(metadata, dict) else None
    if not isinstance(policy, dict):
        return ""

    lines: list[str] = []
    instruction = str(policy.get("response_instruction") or "").strip()
    blocked_actions = [
        str(action).strip()
        for action in policy.get("blocked_actions") or []
        if str(action).strip()
    ]
    if instruction:
        lines.append(f"Follow this local policy instruction: {instruction}")
    if policy.get("safe_to_execute_tools") is False:
        lines.append(
            "Do not claim to perform account, payment, subscription, or service changes. "
            "You may explain or ask for confirmation only."
        )
    if blocked_actions:
        lines.append(
            "Blocked actions for this turn: "
            + ", ".join(blocked_actions)
            + ". Do not say these actions have been performed."
        )
    return "\n".join(lines)


def _delivery_prompt(metadata: dict[str, Any]) -> str:
    delivery = (
        metadata.get("previous_assistant_delivery")
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(delivery, dict):
        return ""
    if str(delivery.get("delivery_status") or "") == "completed":
        return ""
    undelivered_text = _truncate_prompt_text(
        str(delivery.get("undelivered_text") or "").strip()
    )
    if not undelivered_text:
        return ""
    delivered_text = _truncate_prompt_text(
        str(delivery.get("delivered_text") or "").strip()
    )
    latest_user_text = _truncate_prompt_text(
        str(
            delivery.get("latest_user_text")
            or delivery.get("interruption_user_text")
            or ""
        ).strip()
    )
    lines = [
        "The previous assistant response was likely interrupted before all generated speech was heard.",
        "Do not assume the caller heard the unheard portion.",
    ]
    if latest_user_text:
        lines.append(f"Latest user turn after interruption: {latest_user_text}")
    if delivered_text:
        lines.append(f"Likely heard: {delivered_text}")
    else:
        lines.append("Likely heard: nothing from that response.")
    lines.append(f"Likely unheard: {undelivered_text}")
    lines.append(
        "If the unheard part is important for the latest user turn, briefly restate or complete it naturally. "
        "Do not say you were interrupted unless that is useful."
    )
    lines.append(
        "If the latest user turn asks you to continue or carry on, start with a brief bridge that acknowledges "
        "where you are resuming from the likely heard text, then continue with the likely unheard text. "
        "Do not restart the full answer."
    )
    return "\n".join(lines)


def _truncate_prompt_text(text: str, limit: int = DELIVERY_PROMPT_TEXT_LIMIT) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "..."


def _respond_create_kwargs(
    request: RespondRequest, *, stream: bool = False
) -> dict[str, Any]:
    model = os.getenv("OPENAI_LLM_MODEL", "gpt-5-mini")
    max_response_words = _response_word_limit()
    max_output_tokens = _response_token_limit()
    history_message_limit = _history_message_limit()
    reasoning_effort = (os.getenv("OPENAI_LLM_REASONING_EFFORT") or "minimal").strip()
    developer_prompt = (
        "You are a low-latency live phone voice assistant. Answer directly, naturally, "
        f"and briefly for text-to-speech playback. Aim for at most {max_response_words} words. "
        "Use one short sentence by default and two only when needed, avoid bullet lists unless the user asks for a list, "
        "and ask at most one follow-up question."
    )
    policy_prompt = _policy_prompt(request.metadata)
    if policy_prompt:
        developer_prompt = (
            f"{developer_prompt}\n\nLocal policy constraints:\n{policy_prompt}"
        )
    delivery_prompt = _delivery_prompt(request.metadata)
    if delivery_prompt:
        developer_prompt = (
            f"{developer_prompt}\n\nResponse delivery context:\n{delivery_prompt}"
        )
    messages: list[dict[str, str]] = [
        {
            "role": "developer",
            "content": developer_prompt,
        }
    ]
    if history_message_limit > 0:
        messages.extend(request.history[-history_message_limit:])
    messages.append({"role": "user", "content": request.text})
    create_kwargs: dict[str, Any] = {
        "model": model,
        "input": messages,
        "max_output_tokens": max_output_tokens,
    }
    if stream:
        create_kwargs["stream"] = True
    if reasoning_effort and (model.startswith("gpt-5") or model.startswith("o")):
        create_kwargs["reasoning"] = {"effort": reasoning_effort}
    return create_kwargs


def _translation_create_kwargs(request: TranslateRequest) -> dict[str, Any]:
    model = _translation_model()
    reasoning_effort = (
        os.getenv("OPENAI_TRANSLATION_REASONING_EFFORT")
        or os.getenv("OPENAI_LLM_REASONING_EFFORT")
        or "minimal"
    ).strip()
    verbosity = (os.getenv("OPENAI_TRANSLATION_VERBOSITY") or "low").strip()
    create_kwargs: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": (
                    "Translate the user's utterance for a live phone call. "
                    "Return only the translated sentence. Preserve intent, tone, "
                    "names, numbers, and short conversational fillers when useful. "
                    "Do not add explanations."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Source language: {request.source_language}\n"
                    f"Target language: {request.target_language}\n"
                    f"Text: {request.text}"
                ),
            },
        ],
        "max_output_tokens": _translation_token_limit(),
    }
    if reasoning_effort and (model.startswith("gpt-5") or model.startswith("o")):
        create_kwargs["reasoning"] = {"effort": reasoning_effort}
    if verbosity and model.startswith("gpt-5"):
        create_kwargs["text"] = {"verbosity": verbosity}
    return create_kwargs


def _ndjson_event(event: dict[str, Any]) -> str:
    return json.dumps(event, separators=(",", ":")) + "\n"


def _stream_event_value(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def _stream_event_delta(event: Any) -> str:
    event_type = str(_stream_event_value(event, "type") or "")
    if event_type and event_type not in {
        "response.output_text.delta",
        "response.refusal.delta",
        "output_text.delta",
    }:
        return ""
    delta = _stream_event_value(event, "delta")
    if delta is None:
        return ""
    return str(delta)


def _stream_event_completed_text(event: Any) -> str:
    event_type = str(_stream_event_value(event, "type") or "")
    if event_type not in {"response.output_text.done", "output_text.done"}:
        return ""
    text = _stream_event_value(event, "text")
    return str(text) if text is not None else ""


async def _iterate_response_stream(stream: Any):
    if hasattr(stream, "__aenter__"):
        async with stream as active_stream:
            async for event in active_stream:
                yield event
        return
    async for event in stream:
        yield event


async def _respond_stream_events(
    request: RespondRequest,
) -> AsyncIterator[dict[str, Any]]:
    model = os.getenv("OPENAI_LLM_MODEL", "gpt-5-mini")
    weather_request = _weather_request_for_text(request)
    if weather_request:
        async for event in _mock_weather_stream_events(weather_request):
            yield event
        return

    if offline_fallback_enabled():
        response = _local_response(request)
        logger.info(
            "respond session_id=%s provider=%s model=%s",
            request.session_id,
            response.provider,
            response.model,
        )
        yield {
            "type": "started",
            "session_id": request.session_id,
            "model": response.model,
            "provider": response.provider,
        }
        yield {"type": "delta", "text": response.text}
        yield {
            "type": "completed",
            "session_id": request.session_id,
            "text": response.text,
            "model": response.model,
            "provider": response.provider,
        }
        return

    fast_path_text = _fast_path_response_text(request)
    if fast_path_text:
        response = _fast_path_response(request, fast_path_text)
        logger.info(
            "respond session_id=%s provider=%s model=%s",
            request.session_id,
            response.provider,
            response.model,
        )
        yield {
            "type": "started",
            "session_id": request.session_id,
            "model": response.model,
            "provider": response.provider,
        }
        yield {"type": "delta", "text": response.text}
        yield {
            "type": "completed",
            "session_id": request.session_id,
            "text": response.text,
            "model": response.model,
            "provider": response.provider,
        }
        return

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is required unless AI_OFFLINE_FALLBACK=1",
        )

    client = _get_openai_client(api_key)
    yield {
        "type": "started",
        "session_id": request.session_id,
        "model": model,
        "provider": "openai",
    }
    parts: list[str] = []
    completed_text = ""
    try:
        stream_result = client.responses.create(
            **_respond_create_kwargs(request, stream=True)
        )
        stream = (
            await stream_result if inspect.isawaitable(stream_result) else stream_result
        )
        async for event in _iterate_response_stream(stream):
            delta = _stream_event_delta(event)
            if delta:
                parts.append(delta)
                yield {"type": "delta", "text": delta}
                continue
            event_completed_text = _stream_event_completed_text(event)
            if event_completed_text:
                completed_text = event_completed_text
    except Exception:
        logger.exception(
            "respond stream upstream failed session_id=%s", request.session_id
        )
        if _upstream_failure_fallback_enabled():
            response = _local_response(request)
            yield {"type": "delta", "text": response.text}
            yield {
                "type": "completed",
                "session_id": request.session_id,
                "text": response.text,
                "model": response.model,
                "provider": response.provider,
            }
            return
        yield {
            "type": "error",
            "code": "openai_response_failed",
            "message": "OpenAI response failed",
        }
        return

    text = completed_text or "".join(parts)
    if not text:
        logger.warning(
            "respond stream upstream returned empty text session_id=%s model=%s",
            request.session_id,
            model,
        )
        if _upstream_failure_fallback_enabled():
            response = _local_response(request)
            yield {"type": "delta", "text": response.text}
            yield {
                "type": "completed",
                "session_id": request.session_id,
                "text": response.text,
                "model": response.model,
                "provider": response.provider,
            }
            return
        text = "I could not produce a response."
        yield {"type": "delta", "text": text}
    logger.info(
        "respond session_id=%s provider=openai model=%s", request.session_id, model
    )
    yield {
        "type": "completed",
        "session_id": request.session_id,
        "text": text,
        "model": model,
        "provider": "openai",
    }


@app.post("/v1/respond", response_model=RespondResponse)
async def respond(request: RespondRequest) -> RespondResponse:
    model = os.getenv("OPENAI_LLM_MODEL", "gpt-5-mini")
    api_key = os.getenv("OPENAI_API_KEY")
    weather_request = _weather_request_for_text(request)
    if weather_request:
        response = await _mock_weather_lookup(weather_request)
        return RespondResponse(
            session_id=request.session_id,
            text=_weather_answer(response),
            model=response.model,
            provider="local_tool_orchestration",
        )
    if offline_fallback_enabled():
        logger.info("respond session_id=%s provider=local", request.session_id)
        return _local_response(request)
    fast_path_text = _fast_path_response_text(request)
    if fast_path_text:
        logger.info(
            "respond session_id=%s provider=local_fast_path", request.session_id
        )
        return _fast_path_response(request, fast_path_text)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is required unless AI_OFFLINE_FALLBACK=1",
        )

    client = _get_openai_client(api_key)
    try:
        response = await client.responses.create(**_respond_create_kwargs(request))
    except Exception as exc:
        logger.exception("respond upstream failed session_id=%s", request.session_id)
        if _upstream_failure_fallback_enabled():
            return _local_response(request)
        raise HTTPException(status_code=502, detail="OpenAI response failed") from exc
    text = getattr(response, "output_text", None)
    if not text:
        logger.warning(
            "respond upstream returned empty output_text session_id=%s model=%s status=%s incomplete=%s",
            request.session_id,
            model,
            getattr(response, "status", None),
            getattr(response, "incomplete_details", None),
        )
        if _upstream_failure_fallback_enabled():
            return _local_response(request)
        text = "I could not produce a response."
    logger.info(
        "respond session_id=%s provider=openai model=%s", request.session_id, model
    )
    return RespondResponse(
        session_id=request.session_id,
        text=text,
        model=model,
        provider="openai",
    )


@app.post("/v1/respond/stream")
async def respond_stream(request: RespondRequest) -> StreamingResponse:
    if (
        not _weather_request_for_text(request)
        and not offline_fallback_enabled()
        and not _fast_path_response_text(request)
        and not os.getenv("OPENAI_API_KEY")
    ):
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is required unless AI_OFFLINE_FALLBACK=1",
        )

    async def lines() -> AsyncIterator[str]:
        async for event in _respond_stream_events(request):
            yield _ndjson_event(event)

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.post("/v1/tools/weather", response_model=WeatherToolResponse)
async def weather_tool(request: WeatherToolRequest) -> WeatherToolResponse:
    return await _mock_weather_lookup(request)


@app.post("/v1/translate", response_model=TranslateResponse)
async def translate(request: TranslateRequest) -> TranslateResponse:
    model = _translation_model()
    api_key = os.getenv("OPENAI_API_KEY")
    if offline_fallback_enabled():
        logger.info(
            "translate session_id=%s provider=local route=%s->%s",
            request.session_id,
            request.source_language,
            request.target_language,
        )
        return _local_translation(request)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is required unless AI_OFFLINE_FALLBACK=1",
        )

    client = _get_openai_client(api_key)
    try:
        response = await client.responses.create(**_translation_create_kwargs(request))
    except Exception as exc:
        logger.exception("translate upstream failed session_id=%s", request.session_id)
        if _upstream_failure_fallback_enabled():
            return _local_translation(request)
        raise HTTPException(status_code=502, detail="OpenAI translation failed") from exc
    text = (getattr(response, "output_text", None) or "").strip()
    if not text:
        logger.warning(
            "translate upstream returned empty output_text session_id=%s model=%s status=%s incomplete=%s",
            request.session_id,
            model,
            getattr(response, "status", None),
            getattr(response, "incomplete_details", None),
        )
        if _upstream_failure_fallback_enabled():
            return _local_translation(request)
        raise HTTPException(
            status_code=502,
            detail="OpenAI translation returned empty output",
        )
    logger.info(
        "translate session_id=%s provider=openai model=%s route=%s->%s",
        request.session_id,
        model,
        request.source_language,
        request.target_language,
    )
    return TranslateResponse(
        session_id=request.session_id,
        text=text,
        source_language=request.source_language,
        target_language=request.target_language,
        model=model,
        provider="openai",
    )
