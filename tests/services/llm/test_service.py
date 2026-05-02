import json

import pytest

fastapi = pytest.importorskip("fastapi")
if not hasattr(fastapi, "HTTPException") or not getattr(fastapi, "__file__", None):
    pytest.skip("real fastapi is required", allow_module_level=True)
from fastapi.testclient import TestClient  # noqa: E402

from services.llm_service.main import (  # noqa: E402
    DEFAULT_LLM_HISTORY_MESSAGES,
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
    DEFAULT_LLM_MAX_RESPONSE_WORDS,
    DEFAULT_TRANSLATION_MAX_OUTPUT_TOKENS,
    RespondRequest,
    TranslateRequest,
    WeatherToolRequest,
    _delivery_prompt,
    _fast_path_response_text,
    _history_message_limit,
    _local_response,
    _local_translation,
    _mock_weather_lookup,
    _policy_prompt,
    _respond_create_kwargs,
    _respond_stream_events,
    _response_token_limit,
    _response_word_limit,
    _translation_create_kwargs,
    _translation_model,
    _translation_token_limit,
    _upstream_failure_fallback_enabled,
    app,
    respond,
    translate,
)


def test_llm_upstream_failure_fallback_is_opt_in(monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_ON_UPSTREAM_ERROR", raising=False)
    assert _upstream_failure_fallback_enabled() is False

    monkeypatch.setenv("LLM_FALLBACK_ON_UPSTREAM_ERROR", "true")
    assert _upstream_failure_fallback_enabled() is True


def test_local_response_echoes_text():
    response = _local_response(RespondRequest(session_id="s1", text="hello"))

    assert response.session_id == "s1"
    assert response.provider == "local"
    assert response.text == "I heard you say: hello"


def test_local_translation_preserves_route():
    response = _local_translation(
        TranslateRequest(
            session_id="s1",
            text="hello",
            source_language="en",
            target_language="es",
        )
    )

    assert response.source_language == "en"
    assert response.target_language == "es"
    assert response.text == "[es] hello"


@pytest.mark.asyncio
async def test_mock_weather_tool_returns_structured_fake_weather(monkeypatch):
    monkeypatch.setenv("MOCK_WEATHER_TOOL_DELAY_MS", "0")

    response = await _mock_weather_lookup(
        WeatherToolRequest(session_id="s1", location="Cardiff", question="weather")
    )

    assert response.session_id == "s1"
    assert response.location == "Cardiff"
    assert response.provider == "local_tool"
    assert response.summary.startswith("Cardiff:")
    assert 0 <= response.chance_of_rain <= 80


@pytest.mark.asyncio
async def test_weather_question_streams_tool_call_events(monkeypatch):
    monkeypatch.setenv("LLM_ENABLE_MOCK_WEATHER_TOOL", "1")
    monkeypatch.setenv("MOCK_WEATHER_TOOL_DELAY_MS", "0")
    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    events = [
        event
        async for event in _respond_stream_events(
            RespondRequest(
                session_id="s1", text="What's the weather in Lisbon tomorrow?"
            )
        )
    ]

    assert [event["type"] for event in events] == [
        "started",
        "tool_call_started",
        "tool_call_completed",
        "delta",
        "completed",
    ]
    assert events[1]["tool_name"] == "mock_weather_lookup"
    assert events[1]["location"] == "Lisbon"
    assert "mock weather feed" in events[1]["speech_text"]
    assert events[2]["result"]["provider"] == "local_tool"
    assert "mock forecast for Lisbon" in events[-1]["text"]


def test_policy_prompt_constrains_blocked_actions():
    prompt = _policy_prompt(
        {
            "policy": {
                "response_instruction": "Explain, but do not change anything.",
                "safe_to_execute_tools": False,
                "blocked_actions": ["cancel_service"],
            }
        }
    )

    assert "Explain, but do not change anything." in prompt
    assert "Do not claim to perform" in prompt
    assert "cancel_service" in prompt


def test_delivery_prompt_includes_unheard_response_context():
    prompt = _delivery_prompt(
        {
            "previous_assistant_delivery": {
                "delivery_status": "interrupted",
                "latest_user_text": "No, carry on.",
                "delivered_text": "The sky is blue because",
                "undelivered_text": "short blue wavelengths scatter more in the atmosphere.",
            }
        }
    )

    assert "likely interrupted" in prompt
    assert "The sky is blue because" in prompt
    assert "short blue wavelengths scatter" in prompt
    assert "Do not assume" in prompt
    assert "Latest user turn after interruption: No, carry on." in prompt
    assert "asks you to continue or carry on" in prompt
    assert "Do not restart the full answer" in prompt


def test_delivery_prompt_ignores_empty_or_completed_context():
    assert _delivery_prompt({}) == ""
    assert (
        _delivery_prompt(
            {"previous_assistant_delivery": {"delivery_status": "completed"}}
        )
        == ""
    )
    assert (
        _delivery_prompt(
            {"previous_assistant_delivery": {"delivery_status": "interrupted"}}
        )
        == ""
    )


def test_policy_and_delivery_prompts_are_added_to_openai_request(monkeypatch):
    monkeypatch.setenv("OPENAI_LLM_MODEL", "gpt-5-mini")
    kwargs = _respond_create_kwargs(
        RespondRequest(
            session_id="s1",
            text="keep going",
            metadata={
                "policy": {"response_instruction": "Answer briefly."},
                "previous_assistant_delivery": {
                    "delivery_status": "interrupted",
                    "latest_user_text": "No, carry on.",
                    "delivered_text": "The sky is blue because",
                    "undelivered_text": "short blue wavelengths scatter more.",
                },
            },
        )
    )

    developer_message = kwargs["input"][0]["content"]
    assert "Local policy constraints" in developer_message
    assert "Answer briefly." in developer_message
    assert "Response delivery context" in developer_message
    assert "No, carry on." in developer_message
    assert "short blue wavelengths scatter more" in developer_message


def test_fast_path_is_disabled_when_delivery_context_is_present(monkeypatch):
    monkeypatch.delenv("LLM_FAST_PATH_RESPONSES", raising=False)
    text = _fast_path_response_text(
        RespondRequest(
            session_id="s1",
            text="Thanks",
            metadata={
                "previous_assistant_delivery": {
                    "delivery_status": "interrupted",
                    "undelivered_text": "The important conclusion.",
                }
            },
        )
    )

    assert text is None


def test_llm_generation_defaults_are_tuned_for_voice_latency(monkeypatch):
    monkeypatch.delenv("OPENAI_LLM_MAX_RESPONSE_WORDS", raising=False)
    monkeypatch.delenv("OPENAI_LLM_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("OPENAI_LLM_HISTORY_MESSAGES", raising=False)

    assert _response_word_limit() == DEFAULT_LLM_MAX_RESPONSE_WORDS
    assert _response_token_limit() == DEFAULT_LLM_MAX_OUTPUT_TOKENS
    assert _history_message_limit() == DEFAULT_LLM_HISTORY_MESSAGES


def test_translation_request_defaults_are_tuned_for_low_latency(monkeypatch):
    monkeypatch.delenv("OPENAI_TRANSLATION_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TRANSLATION_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("OPENAI_TRANSLATION_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("OPENAI_TRANSLATION_VERBOSITY", raising=False)
    monkeypatch.delenv("OPENAI_LLM_REASONING_EFFORT", raising=False)

    request = TranslateRequest(
        session_id="s1",
        text="Bonjour Bob, est-ce que tu entends la traduction?",
        source_language="fr",
        target_language="en",
    )
    kwargs = _translation_create_kwargs(request)

    assert _translation_model() == "gpt-5-nano"
    assert _translation_token_limit() == DEFAULT_TRANSLATION_MAX_OUTPUT_TOKENS
    assert kwargs["model"] == "gpt-5-nano"
    assert kwargs["max_output_tokens"] == DEFAULT_TRANSLATION_MAX_OUTPUT_TOKENS
    assert kwargs["reasoning"] == {"effort": "minimal"}
    assert kwargs["text"] == {"verbosity": "low"}


def test_translation_request_supports_env_overrides(monkeypatch):
    monkeypatch.setenv("OPENAI_TRANSLATION_MODEL", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_TRANSLATION_MAX_OUTPUT_TOKENS", "48")
    monkeypatch.setenv("OPENAI_TRANSLATION_REASONING_EFFORT", "low")
    monkeypatch.setenv("OPENAI_TRANSLATION_VERBOSITY", "medium")

    kwargs = _translation_create_kwargs(
        TranslateRequest(
            session_id="s1",
            text="hello",
            source_language="en",
            target_language="fr",
        )
    )

    assert kwargs["model"] == "gpt-5-mini"
    assert kwargs["max_output_tokens"] == 48
    assert kwargs["reasoning"] == {"effort": "low"}
    assert kwargs["text"] == {"verbosity": "medium"}


@pytest.mark.asyncio
async def test_translate_empty_openai_output_is_bad_gateway(monkeypatch):
    class FakeResponses:
        async def create(self, **_kwargs):
            return type("FakeResponse", (), {"output_text": ""})()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_ON_UPSTREAM_ERROR", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "services.llm_service.main._get_openai_client", lambda _api_key: FakeClient()
    )

    with pytest.raises(fastapi.HTTPException) as exc:
        await translate(
            TranslateRequest(
                session_id="s1",
                text="bonjour",
                source_language="fr",
                target_language="en",
            )
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "OpenAI translation returned empty output"


@pytest.mark.asyncio
async def test_translate_empty_openai_output_uses_explicit_fallback(monkeypatch):
    class FakeResponses:
        async def create(self, **_kwargs):
            return type("FakeResponse", (), {"output_text": " "})()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.setenv("LLM_FALLBACK_ON_UPSTREAM_ERROR", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "services.llm_service.main._get_openai_client", lambda _api_key: FakeClient()
    )

    response = await translate(
        TranslateRequest(
            session_id="s1",
            text="bonjour",
            source_language="fr",
            target_language="en",
        )
    )

    assert response.provider == "local"
    assert response.text == "[en] bonjour"


def test_fast_path_response_handles_initial_greeting(monkeypatch):
    monkeypatch.setenv("LLM_FAST_PATH_RESPONSES", "1")

    text = _fast_path_response_text(RespondRequest(session_id="s1", text="Hello."))

    assert text == "Hi, how can I help?"


def test_fast_path_response_does_not_hide_contextual_greeting(monkeypatch):
    monkeypatch.setenv("LLM_FAST_PATH_RESPONSES", "1")

    text = _fast_path_response_text(
        RespondRequest(
            session_id="s1",
            text="Hi",
            history=[{"role": "assistant", "content": "Should I continue?"}],
        )
    )

    assert text is None


@pytest.mark.asyncio
async def test_offline_fallback_preferred_even_with_api_key(monkeypatch):
    monkeypatch.setenv("AI_OFFLINE_FALLBACK", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    response = await respond(RespondRequest(session_id="s1", text="hello"))
    translation = await translate(
        TranslateRequest(
            session_id="s1",
            text="hello",
            source_language="en",
            target_language="fr",
        )
    )

    assert response.provider == "local"
    assert response.text == "I heard you say: hello"
    assert translation.provider == "local"
    assert translation.text == "[fr] hello"


@pytest.mark.asyncio
async def test_respond_stream_events_use_offline_fallback(monkeypatch):
    monkeypatch.setenv("AI_OFFLINE_FALLBACK", "1")

    events = [
        event
        async for event in _respond_stream_events(
            RespondRequest(session_id="s1", text="hello")
        )
    ]

    assert [event["type"] for event in events] == ["started", "delta", "completed"]
    assert events[1]["text"] == "I heard you say: hello"
    assert events[-1]["provider"] == "local"


def test_respond_stream_endpoint_emits_ndjson(monkeypatch):
    monkeypatch.setenv("AI_OFFLINE_FALLBACK", "1")
    client = TestClient(app)

    response = client.post(
        "/v1/respond/stream", json={"session_id": "s1", "text": "hello"}
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["started", "delta", "completed"]


def test_weather_stream_endpoint_does_not_require_openai(monkeypatch):
    monkeypatch.setenv("LLM_ENABLE_MOCK_WEATHER_TOOL", "1")
    monkeypatch.setenv("MOCK_WEATHER_TOOL_DELAY_MS", "0")
    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(app)

    response = client.post(
        "/v1/respond/stream",
        json={"session_id": "s1", "text": "Can you get the forecast for Berlin?"},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert "tool_call_started" in [event["type"] for event in events]


def test_weather_stream_requires_explicit_mock_tool_without_openai(monkeypatch):
    monkeypatch.delenv("LLM_ENABLE_MOCK_WEATHER_TOOL", raising=False)
    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(app)

    response = client.post(
        "/v1/respond/stream",
        json={"session_id": "s1", "text": "Can you get the forecast for Berlin?"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "OPENAI_API_KEY is required unless AI_OFFLINE_FALLBACK=1"
    )


def test_weather_tool_endpoint(monkeypatch):
    monkeypatch.setenv("MOCK_WEATHER_TOOL_DELAY_MS", "0")
    client = TestClient(app)

    response = client.post(
        "/v1/tools/weather", json={"session_id": "s1", "location": "Tokyo"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["location"] == "Tokyo"
    assert body["provider"] == "local_tool"


def test_respond_endpoint_sanitizes_upstream_error(monkeypatch):
    class FakeResponses:
        async def create(self, **_kwargs):
            raise RuntimeError("raw upstream credential detail")

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_ON_UPSTREAM_ERROR", raising=False)
    monkeypatch.setenv("LLM_FAST_PATH_RESPONSES", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "services.llm_service.main._get_openai_client", lambda _api_key: FakeClient()
    )
    client = TestClient(app)

    response = client.post("/v1/respond", json={"session_id": "s1", "text": "hello"})

    assert response.status_code == 502
    assert response.json()["detail"] == "OpenAI response failed"
    assert "raw upstream credential detail" not in response.text


def test_translate_endpoint_sanitizes_upstream_error(monkeypatch):
    class FakeResponses:
        async def create(self, **_kwargs):
            raise RuntimeError("raw translation upstream detail")

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_ON_UPSTREAM_ERROR", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "services.llm_service.main._get_openai_client", lambda _api_key: FakeClient()
    )
    client = TestClient(app)

    response = client.post(
        "/v1/translate",
        json={
            "session_id": "s1",
            "text": "bonjour",
            "source_language": "fr",
            "target_language": "en",
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "OpenAI translation failed"
    assert "raw translation upstream detail" not in response.text


@pytest.mark.asyncio
async def test_respond_stream_events_emit_mocked_openai_deltas(monkeypatch):
    class FakeStream:
        async def __aiter__(self):
            yield {"type": "response.output_text.delta", "delta": "Hello"}
            yield {"type": "response.output_text.delta", "delta": " there."}
            yield {"type": "response.output_text.done", "text": "Hello there."}

    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return FakeStream()

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    fake_client = FakeClient()
    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.setenv("LLM_FAST_PATH_RESPONSES", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "services.llm_service.main._get_openai_client", lambda _api_key: fake_client
    )

    events = [
        event
        async for event in _respond_stream_events(
            RespondRequest(session_id="s1", text="hello")
        )
    ]

    assert [event["type"] for event in events] == [
        "started",
        "delta",
        "delta",
        "completed",
    ]
    assert events[1]["text"] == "Hello"
    assert events[2]["text"] == " there."
    assert events[-1]["text"] == "Hello there."
    assert fake_client.responses.kwargs["stream"] is True


@pytest.mark.asyncio
async def test_respond_stream_events_sanitize_upstream_error(monkeypatch):
    class FakeResponses:
        def create(self, **_kwargs):
            raise RuntimeError("raw upstream credential detail")

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    monkeypatch.delenv("AI_OFFLINE_FALLBACK", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_ON_UPSTREAM_ERROR", raising=False)
    monkeypatch.setenv("LLM_FAST_PATH_RESPONSES", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "services.llm_service.main._get_openai_client", lambda _api_key: FakeClient()
    )

    events = [
        event
        async for event in _respond_stream_events(
            RespondRequest(session_id="s1", text="hello")
        )
    ]

    assert events[-1] == {
        "type": "error",
        "code": "openai_response_failed",
        "message": "OpenAI response failed",
    }
    assert "raw upstream credential detail" not in str(events)
