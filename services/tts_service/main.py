"""FreeSWITCH TTS injection service."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, Field

from services.common.config import env_bool, env_int, env_str
from services.common.freeswitch import inbound_connection
from services.tts_service.freeswitch import (
    TTSConfig,
    build_sendmsg_payload,
    tts_variants,
)


class SpeakRequest(BaseModel):
    fs_uuid: str
    text: str
    language: str | None = None
    interruptible: bool = True
    event_lock: bool = Field(
        default=False,
        validation_alias=AliasChoices("event_lock", "wait_complete"),
        description=(
            "Request FreeSWITCH event-lock command sequencing. "
            "This does not wait for audible playback completion."
        ),
    )
    fs_host: str | None = None
    event_uuid: str | None = None


class SpeakResponse(BaseModel):
    status: str
    fs_uuid: str
    attempted_spec: str
    event_uuid: str | None = None
    command_latency_ms: float | None = None
    playback_started_ms: float | None = Field(
        default=None,
        description=(
            "Not populated by tts_service; gateway timing is inferred from "
            "FreeSWITCH channel events or estimates."
        ),
    )
    playback_completed_ms: float | None = Field(
        default=None,
        description=(
            "Not populated by tts_service; gateway timing is inferred from "
            "FreeSWITCH channel events or estimates."
        ),
    )
    event_lock_requested: bool = Field(
        default=False,
        description="Whether FreeSWITCH event-lock command sequencing was requested.",
    )


app = FastAPI(title="TTS Service")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("tts_service")
_inbound_lock = asyncio.Lock()
_inbound_by_host: dict[str, object] = {}


@app.get("/health")
async def health() -> JSONResponse:
    errors = _tts_readiness_errors()
    if errors:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "detail": "FreeSWITCH ESL configuration is incomplete",
                "errors": errors,
            },
        )
    return JSONResponse(content={"status": "ok"})


def _tts_readiness_errors() -> list[str]:
    errors: list[str] = []
    if not env_str("FREESWITCH_HOST"):
        errors.append("FREESWITCH_HOST is required")
    port_value = (os.getenv("FREESWITCH_ESL_PORT") or "").strip()
    if not port_value:
        errors.append("FREESWITCH_ESL_PORT is required")
    else:
        try:
            port = int(port_value)
        except ValueError:
            errors.append("FREESWITCH_ESL_PORT must be an integer")
        else:
            if port < 1 or port > 65535:
                errors.append("FREESWITCH_ESL_PORT must be between 1 and 65535")
    if not env_str("FREESWITCH_ESL_PASSWORD"):
        errors.append("FREESWITCH_ESL_PASSWORD is required")
    return errors


async def _inbound(fs_host: str):
    host = fs_host or os.getenv("FREESWITCH_HOST", "freeswitch")
    password = os.getenv("FREESWITCH_ESL_PASSWORD", "")
    if not password:
        raise RuntimeError("FREESWITCH_ESL_PASSWORD is required")
    return await inbound_connection(
        host=host,
        port=env_int("FREESWITCH_ESL_PORT", 8021),
        password=password,
        events="events plain CHANNEL_EXECUTE_COMPLETE BACKGROUND_JOB",
        lock=_inbound_lock,
        connections=_inbound_by_host,
    )


@app.post("/v1/speak", response_model=SpeakResponse)
async def speak(request: SpeakRequest) -> SpeakResponse:
    config = TTSConfig.from_env(request.language)
    specs = tts_variants(request.text, config)
    if env_bool("TTS_DRY_RUN", False):
        return SpeakResponse(
            status="dry_run",
            fs_uuid=request.fs_uuid,
            attempted_spec=specs[0],
            event_uuid=request.event_uuid,
            event_lock_requested=request.event_lock,
        )

    last_failure = "FreeSWITCH speak command was rejected"
    fs_host = request.fs_host or os.getenv("FREESWITCH_HOST", "freeswitch")
    try:
        ctl = await _inbound(fs_host)
    except Exception as exc:
        logger.exception(
            "freeswitch inbound connection failed fs_uuid=%s fs_host=%s",
            request.fs_uuid,
            fs_host,
        )
        raise HTTPException(
            status_code=502,
            detail="FreeSWITCH control connection failed",
        ) from exc
    for spec in specs:
        try:
            payload = build_sendmsg_payload(
                request.fs_uuid,
                "speak",
                spec,
                lock=request.event_lock,
                event_uuid=request.event_uuid,
            )
            started_at = time.perf_counter()
            reply = await ctl.send(payload)
            command_latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
            reply_text = str(reply.get("Reply-Text") or "")
            if "-err" not in reply_text.lower():
                logger.info(
                    "queued speak fs_uuid=%s language=%s spec=%s",
                    request.fs_uuid,
                    request.language or "",
                    _spec_label(spec),
                )
                return SpeakResponse(
                    status="queued",
                    fs_uuid=request.fs_uuid,
                    attempted_spec=spec,
                    event_uuid=request.event_uuid,
                    command_latency_ms=command_latency_ms,
                    event_lock_requested=request.event_lock,
                )
            last_failure = reply_text or last_failure
            logger.warning(
                "freeswitch speak rejected fs_uuid=%s spec=%s reply=%s",
                request.fs_uuid,
                _spec_label(spec),
                last_failure,
            )
        except Exception as exc:
            last_failure = exc.__class__.__name__
            logger.exception(
                "freeswitch speak command failed fs_uuid=%s spec=%s",
                request.fs_uuid,
                _spec_label(spec),
            )
    logger.error(
        "freeswitch speak failed fs_uuid=%s last_failure=%s",
        request.fs_uuid,
        last_failure,
    )
    raise HTTPException(
        status_code=502,
        detail="FreeSWITCH speak command failed",
    )


def _spec_label(spec: str) -> str:
    return "|".join(spec.split("|", 2)[:2])
