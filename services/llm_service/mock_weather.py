"""Local mock weather tool used by the LLM service demo paths."""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator

from pydantic import BaseModel

from services.common.config import env_bool, env_int


DEFAULT_MOCK_WEATHER_DELAY_MS = 900
WEATHER_TOOL_NAME = "mock_weather_lookup"
WEATHER_QUERY_TERMS = {
    "weather",
    "forecast",
    "temperature",
    "rain",
    "raining",
    "sunny",
    "umbrella",
}
WEATHER_CONDITIONS = [
    "clear",
    "partly cloudy",
    "overcast",
    "light rain",
    "windy",
    "bright spells",
]
WEATHER_LOCATION_STOP_WORDS = {
    "today",
    "tomorrow",
    "tonight",
    "please",
    "now",
    "currently",
    "right",
    "this",
    "week",
    "weekend",
}


class WeatherToolRequest(BaseModel):
    session_id: str
    location: str = "your area"
    question: str = ""


class WeatherToolResponse(BaseModel):
    session_id: str
    location: str
    condition: str
    temperature_c: int
    chance_of_rain: int
    wind_kph: int
    summary: str
    model: str = "mock-weather-agent"
    provider: str = "local_tool"
    latency_ms: float


def mock_weather_tool_enabled() -> bool:
    return env_bool("LLM_ENABLE_MOCK_WEATHER_TOOL", False)


def mock_weather_delay_ms() -> int:
    return max(
        0,
        min(
            5_000, env_int("MOCK_WEATHER_TOOL_DELAY_MS", DEFAULT_MOCK_WEATHER_DELAY_MS)
        ),
    )


def weather_request_for_text(session_id: str, text: str) -> WeatherToolRequest | None:
    if not mock_weather_tool_enabled():
        return None
    normalised = normalise_short_utterance(text)
    if not any(term in normalised.split() for term in WEATHER_QUERY_TERMS):
        return None
    return WeatherToolRequest(
        session_id=session_id,
        location=weather_location_from_text(text),
        question=text,
    )


def normalise_short_utterance(text: str) -> str:
    cleaned = "".join(
        character.casefold() if character.isalnum() or character.isspace() else " "
        for character in text
    )
    return " ".join(cleaned.split())


def weather_location_from_text(text: str) -> str:
    match = re.search(r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z\s'.-]{1,48})", text or "")
    if not match:
        return "your area"
    words: list[str] = []
    for raw_word in match.group(1).replace("?", " ").replace(".", " ").split():
        word = raw_word.strip(" ,;:!?").casefold()
        if not word or word in WEATHER_LOCATION_STOP_WORDS:
            break
        words.append(raw_word.strip(" ,;:!?"))
    location = " ".join(words).strip()
    return location or "your area"


async def mock_weather_lookup(
    request: WeatherToolRequest, *, delay_ms: int | None = None
) -> WeatherToolResponse:
    started_at = time.perf_counter()
    delay = mock_weather_delay_ms() if delay_ms is None else max(0, delay_ms)
    if delay:
        await asyncio.sleep(delay / 1000)
    rng = random.SystemRandom()
    condition = rng.choice(WEATHER_CONDITIONS)
    temperature_c = rng.randint(5, 28)
    chance_of_rain = rng.randrange(0, 85, 5)
    wind_kph = rng.randint(3, 38)
    summary = (
        f"{request.location}: {condition}, {temperature_c} degrees Celsius, "
        f"{chance_of_rain}% chance of rain, wind around {wind_kph} kilometres per hour."
    )
    return WeatherToolResponse(
        session_id=request.session_id,
        location=request.location,
        condition=condition,
        temperature_c=temperature_c,
        chance_of_rain=chance_of_rain,
        wind_kph=wind_kph,
        summary=summary,
        latency_ms=round((time.perf_counter() - started_at) * 1000, 1),
    )


def weather_status_speech(request: WeatherToolRequest) -> str:
    if request.location == "your area":
        return "I'll check a mock weather feed now."
    return f"I'll check the mock weather feed for {request.location}."


def weather_answer(response: WeatherToolResponse) -> str:
    location = response.location if response.location != "your area" else "your area"
    return (
        f"The mock forecast for {location} is {response.condition}, "
        f"{response.temperature_c} degrees Celsius, with a {response.chance_of_rain}% chance of rain."
    )


async def mock_weather_stream_events(
    request: WeatherToolRequest,
) -> AsyncIterator[dict[str, object]]:
    model = "mock-weather-agent"
    provider = "local_tool_orchestration"
    yield {
        "type": "started",
        "session_id": request.session_id,
        "model": model,
        "provider": provider,
    }
    yield {
        "type": "tool_call_started",
        "session_id": request.session_id,
        "tool_name": WEATHER_TOOL_NAME,
        "location": request.location,
        "speech_text": weather_status_speech(request),
        "message": "Fetching generated weather from the local mock tool.",
    }
    delay_ms = mock_weather_delay_ms()
    if delay_ms:
        await asyncio.sleep(delay_ms / 2000)
        yield {
            "type": "tool_call_progress",
            "session_id": request.session_id,
            "tool_name": WEATHER_TOOL_NAME,
            "location": request.location,
            "elapsed_ms": round(delay_ms / 2, 1),
            "message": "Mock weather agent is composing a structured result.",
        }
        await asyncio.sleep(delay_ms / 2000)
    response = await mock_weather_lookup(request, delay_ms=0)
    result = response.model_dump()
    yield {
        "type": "tool_call_completed",
        "session_id": request.session_id,
        "tool_name": WEATHER_TOOL_NAME,
        "location": request.location,
        "latency_ms": response.latency_ms + delay_ms,
        "result": result,
    }
    text = weather_answer(response)
    yield {"type": "delta", "text": text}
    yield {
        "type": "completed",
        "session_id": request.session_id,
        "text": text,
        "model": model,
        "provider": provider,
    }
