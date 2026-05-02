"""Headless WebRTC smoke checks for the local SIP/WebRTC demo stack."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_WS_URL = "ws://127.0.0.1:5066"
COMPOSE_LOG_SERVICES = (
    "kamailio",
    "rtpengine",
    "freeswitch",
    "voice_gateway",
    "stt_service",
    "llm_service",
    "tts_service",
)
FAILURE_EVENTS = {
    "page_start_failed",
    "outbound_call_failed",
    "call_failed",
    "call_rejected",
}


@dataclass
class Capture:
    events: list[dict[str, Any]] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    events: list[str]
    missing_log_patterns: list[str] = field(default_factory=list)
    error: str = ""


def build_url(base_url: str, page: str, params: Mapping[str, object]) -> str:
    query: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            query[key] = "1" if value else "0"
        else:
            query[key] = str(value)
    return f"{base_url.rstrip('/')}/{page}?{urlencode(query)}"


def page_audio_source(audio_source: str) -> str:
    return "default" if audio_source == "fake-file" else audio_source


def assistant_url(
    base_url: str,
    *,
    ws_url: str = DEFAULT_WS_URL,
    audio_source: str = "tone",
    auto_hangup_ms: int = 55000,
    auto_start: bool = True,
) -> str:
    return build_url(
        base_url,
        "call/call.html",
        {
            "auto_start": auto_start,
            "make_call": True,
            "close_on_complete": True,
            "auto_hangup_ms": auto_hangup_ms,
            "ws_url": ws_url,
            "aor": "sip:demo-1001@voice.local",
            "dial_number": "7000",
            "number_to_call": "7000",
            "contact_number": "7000",
            "audio_source": page_audio_source(audio_source),
            "enable_media_debug": True,
        },
    )


def translation_urls(
    base_url: str,
    *,
    ws_url: str = DEFAULT_WS_URL,
    audio_source: str = "tone",
    auto_hangup_ms: int = 70000,
    auto_start: bool = True,
) -> tuple[str, str]:
    callee = build_url(
        base_url,
        "call/call.html",
        {
            "auto_start": auto_start,
            "auto_answer": True,
            "make_call": False,
            "close_on_complete": True,
            "ws_url": ws_url,
            "aor": "sip:bob@voice.local",
            "dial_number": "bob",
            "contact_number": "bob",
            "audio_source": "silence",
            "remote_audio_muted": True,
            "enable_media_debug": True,
        },
    )
    caller = build_url(
        base_url,
        "call/call.html",
        {
            "auto_start": auto_start,
            "make_call": True,
            "close_on_complete": True,
            "auto_hangup_ms": auto_hangup_ms,
            "ws_url": ws_url,
            "aor": "sip:demo-1001@voice.local",
            "dial_number": "7100",
            "number_to_call": "7100",
            "contact_number": "7100",
            "translate_peer": "sip:bob@voice.local",
            "source_language": "en",
            "target_language": "fr",
            "audio_source": page_audio_source(audio_source),
            "enable_media_debug": True,
        },
    )
    return callee, caller


def translation_demo_url(
    base_url: str,
    *,
    ws_url: str = DEFAULT_WS_URL,
    audio_source: str = "tone",
    source_language: str = "en",
    target_language: str = "fr",
    translate_peer: str = "sip:bob@voice.local",
) -> str:
    return build_url(
        base_url,
        "translation-demo/translation_demo.html",
        {
            "ws_url": ws_url,
            "audio_source": page_audio_source(audio_source),
            "source_language": source_language,
            "target_language": target_language,
            "translate_peer": translate_peer,
            "enable_media_debug": True,
        },
    )


def demo_trace_url(
    base_url: str,
    *,
    trace: str = "multilingual_replay",
    page: str = "call/call.html",
) -> str:
    return build_url(
        base_url,
        page,
        {
            "auto_start": True,
            "voice_events_mock": True,
            "voice_events_mock_trace": trace,
            "enable_media_debug": True,
            "contact_name": "Voice AI Demo",
            "contact_number": "mock",
            "audio_source": "tone",
        },
    )


def write_tone_wav(
    path: Path, *, seconds: float = 18.0, sample_rate: int = 48000
) -> None:
    """Write a mono WAV with voiced tone bursts separated by silence."""

    frequency = 440.0
    amplitude = int(32767 * 0.18)
    cycle_seconds = 3.2
    speech_seconds = 1.4
    fade_seconds = 0.03
    frame_count = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(frame_count):
            t = index / sample_rate
            cycle_t = t % cycle_seconds
            if cycle_t > speech_seconds:
                value = 0
            else:
                fade = (
                    min(cycle_t, speech_seconds - cycle_t, fade_seconds) / fade_seconds
                )
                sample = math.sin(2 * math.pi * frequency * t)
                value = int(amplitude * max(0.0, min(1.0, fade)) * sample)
            wav.writeframesraw(struct.pack("<h", value))


async def attach_page(context: Any, label: str, capture: Capture) -> Any:
    page = await context.new_page()
    capture.pages[label] = page

    def record_harness_event(payload: dict[str, Any]) -> None:
        capture.events.append({"page": label, **payload})

    await page.expose_function("recordHarnessEvent", record_harness_event)
    await page.add_init_script(
        """
        window.addEventListener("Company:harness-event", (event) => {
          window.recordHarnessEvent({
            detail: event.detail || {},
            href: window.location.href,
            title: document.title,
          });
        });
        """
    )
    page.on(
        "console", lambda msg: capture.console.append(f"{label} {msg.type}: {msg.text}")
    )
    page.on("pageerror", lambda exc: capture.errors.append(f"{label} pageerror: {exc}"))
    return page


def _event_name(event: dict[str, Any]) -> str:
    detail = event.get("detail") or {}
    return str(detail.get("event") or "")


def event_names(capture: Capture, label: str | None = None) -> list[str]:
    names: list[str] = []
    for event in capture.events:
        if label is not None and event.get("page") != label:
            continue
        names.append(f"{event.get('page')}:{_event_name(event)}")
    return names


async def wait_for_event(
    capture: Capture, label: str, expected: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    seen_failures: set[tuple[str, str]] = set()
    while time.monotonic() < deadline:
        for event in capture.events:
            if event.get("page") != label:
                continue
            name = _event_name(event)
            if name == expected:
                return event
            if name in FAILURE_EVENTS:
                key = (label, name)
                if key not in seen_failures:
                    seen_failures.add(key)
                    detail = event.get("detail") or {}
                    raise RuntimeError(
                        f"{label} emitted {name}: {detail.get('error') or detail}"
                    )
        await asyncio.sleep(0.25)
    raise TimeoutError(
        f"timed out waiting for {label}:{expected}; saw {event_names(capture, label)}"
    )


def compose_logs_since(since: datetime) -> str:
    command = [
        "docker",
        "compose",
        "logs",
        "--no-color",
        "--since",
        since.isoformat(),
        *COMPOSE_LOG_SERVICES,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"unable to fetch docker compose logs: {exc}"
    return result.stdout


def missing_patterns(logs: str, required: tuple[str, ...]) -> list[str]:
    return [pattern for pattern in required if pattern not in logs]


def scenario_missing_log_patterns(
    started_at: datetime, skip_log_checks: bool, required: tuple[str, ...]
) -> list[str]:
    if skip_log_checks:
        return []
    return missing_patterns(compose_logs_since(started_at), required)


def scenario_artifact_summary(
    result: ScenarioResult, capture: Capture
) -> dict[str, Any]:
    return {
        "name": result.name,
        "ok": result.ok,
        "events": result.events,
        "event_count": len(capture.events),
        "missing_log_patterns": result.missing_log_patterns,
        "errors": capture.errors,
        "console_tail": capture.console[-50:],
        "error": result.error,
    }


async def write_scenario_artifacts(
    artifacts_dir: Path | None,
    result: ScenarioResult,
    capture: Capture,
) -> dict[str, Any] | None:
    if artifacts_dir is None:
        return None
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    summary = scenario_artifact_summary(result, capture)
    stem = result.name
    summary_path = artifacts_dir / f"{stem}-summary.json"
    events_path = artifacts_dir / f"{stem}-events.json"
    console_path = artifacts_dir / f"{stem}-console.txt"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    events_path.write_text(json.dumps(capture.events, indent=2), encoding="utf-8")
    console_path.write_text("\n".join(capture.console[-200:]) + "\n", encoding="utf-8")
    screenshots: dict[str, str] = {}
    for label, page in capture.pages.items():
        screenshot_path = artifacts_dir / f"{stem}-{label}.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as exc:  # pragma: no cover - browser dependent
            capture.errors.append(f"{label} screenshot failed: {exc}")
            continue
        screenshots[label] = str(screenshot_path)
    summary["artifacts"] = {
        "summary": str(summary_path),
        "events": str(events_path),
        "console": str(console_path),
        "screenshots": screenshots,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


async def run_assistant(
    context: Any, args: argparse.Namespace
) -> tuple[ScenarioResult, Capture]:
    capture = Capture()
    page = await attach_page(context, "assistant", capture)
    started_at = datetime.now(timezone.utc)
    required_logs = (
        "assistant session started",
        "local transcript final",
        "assistant transcript final",
        "respond session_id=",
        "queued speak",
    )
    try:
        await page.goto(
            assistant_url(
                args.base_url,
                ws_url=args.ws_url,
                audio_source=args.audio_source,
                auto_hangup_ms=args.assistant_hangup_ms,
                auto_start=args.start_mode == "auto",
            ),
            wait_until="domcontentloaded",
        )
        await wait_for_event(capture, "assistant", "page_ready", args.timeout)
        if args.start_mode == "click":
            await page.click("#start-call-button")
        await wait_for_event(capture, "assistant", "registered", args.timeout)
        await wait_for_event(capture, "assistant", "call_answered", args.timeout)
        await wait_for_event(
            capture,
            "assistant",
            "call_hangup",
            args.timeout + args.assistant_hangup_ms / 1000,
        )
    except Exception as exc:
        capture.errors.append(str(exc))
        missing = scenario_missing_log_patterns(
            started_at, args.skip_log_checks, required_logs
        )
        return ScenarioResult(
            "assistant", False, event_names(capture), missing, str(exc)
        ), capture

    missing = scenario_missing_log_patterns(
        started_at, args.skip_log_checks, required_logs
    )
    return ScenarioResult(
        "assistant", not missing, event_names(capture), missing
    ), capture


async def run_translation(
    context: Any, args: argparse.Namespace
) -> tuple[ScenarioResult, Capture]:
    capture = Capture()
    callee_page = await attach_page(context, "callee", capture)
    caller_page = await attach_page(context, "caller", capture)
    started_at = datetime.now(timezone.utc)
    callee_url, caller_url = translation_urls(
        args.base_url,
        ws_url=args.ws_url,
        audio_source=args.audio_source,
        auto_hangup_ms=args.translation_hangup_ms,
        auto_start=args.start_mode == "auto",
    )
    required_logs = (
        "translation session started",
        "translation peer media started",
        "translation transcript final",
        "translate session_id=",
        "queued speak",
    )
    try:
        await callee_page.goto(callee_url, wait_until="domcontentloaded")
        await wait_for_event(capture, "callee", "page_ready", args.timeout)
        if args.start_mode == "click":
            await callee_page.click("#start-call-button")
        await wait_for_event(capture, "callee", "registered", args.timeout)
        await caller_page.goto(caller_url, wait_until="domcontentloaded")
        await wait_for_event(capture, "caller", "page_ready", args.timeout)
        if args.start_mode == "click":
            await caller_page.click("#start-call-button")
        await wait_for_event(capture, "caller", "registered", args.timeout)
        await wait_for_event(capture, "caller", "call_answered", args.timeout)
        await wait_for_event(capture, "callee", "call_received", args.timeout)
        await wait_for_event(capture, "callee", "call_answered", args.timeout)
        await wait_for_event(
            capture,
            "caller",
            "call_hangup",
            args.timeout + args.translation_hangup_ms / 1000,
        )
        await wait_for_event(capture, "callee", "call_hangup", args.timeout)
    except Exception as exc:
        capture.errors.append(str(exc))
        missing = scenario_missing_log_patterns(
            started_at, args.skip_log_checks, required_logs
        )
        return ScenarioResult(
            "translation", False, event_names(capture), missing, str(exc)
        ), capture

    missing = scenario_missing_log_patterns(
        started_at, args.skip_log_checks, required_logs
    )
    return ScenarioResult(
        "translation", not missing, event_names(capture), missing
    ), capture


def browser_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def run(args: argparse.Namespace) -> int:
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError:
        print(
            "Playwright is required for this smoke runner. "
            "Run with: uv run --with playwright python scripts/webrtc_smoke.py",
            file=sys.stderr,
        )
        return 2

    fake_audio_path: Path | None = None
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.audio_source == "fake-file":
        if args.fake_audio_file:
            fake_audio_path = Path(args.fake_audio_file).resolve()
        else:
            temp_dir = tempfile.TemporaryDirectory()
            fake_audio_path = Path(temp_dir.name) / "webrtc-smoke-tone.wav"
            write_tone_wav(fake_audio_path)

    launch_args = [
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if fake_audio_path is not None:
        launch_args.extend(
            [
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={fake_audio_path}",
            ]
        )

    results: list[ScenarioResult] = []
    artifact_summaries: list[dict[str, Any]] = []
    artifacts_dir = Path(args.artifacts_dir).resolve() if args.artifacts_dir else None
    try:
        async with async_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {
                "headless": not args.headful,
                "args": launch_args,
            }
            if args.browser_channel:
                launch_kwargs["channel"] = args.browser_channel
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(ignore_https_errors=True)
            await context.grant_permissions(
                ["microphone"], origin=browser_origin(args.base_url)
            )
            context.set_default_timeout(args.timeout * 1000)
            try:
                if args.scenario in {"assistant", "both"}:
                    result, capture = await run_assistant(context, args)
                    result.error = "\n".join(capture.errors)
                    results.append(result)
                    artifact_summary = await write_scenario_artifacts(
                        artifacts_dir, result, capture
                    )
                    if artifact_summary:
                        artifact_summaries.append(artifact_summary)
                if args.scenario in {"translation", "both"}:
                    result, capture = await run_translation(context, args)
                    result.error = "\n".join(capture.errors)
                    results.append(result)
                    artifact_summary = await write_scenario_artifacts(
                        artifacts_dir, result, capture
                    )
                    if artifact_summary:
                        artifact_summaries.append(artifact_summary)
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        print(f"WebRTC smoke failed: {exc}", file=sys.stderr)
        if "Executable doesn't exist" in str(exc):
            print(
                "Install a browser once with: uv run --with playwright playwright install chromium",
                file=sys.stderr,
            )
        if temp_dir:
            temp_dir.cleanup()
        return 1
    finally:
        if temp_dir:
            temp_dir.cleanup()

    output = {
        "ok": all(result.ok for result in results),
        "results": [result.__dict__ for result in results],
        "artifacts_dir": str(artifacts_dir) if artifacts_dir else None,
        "artifacts": artifact_summaries,
    }
    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "summary.json").write_text(
            json.dumps(output, indent=2), encoding="utf-8"
        )

    print(json.dumps(output, indent=2))
    return 0 if all(result.ok for result in results) else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=("assistant", "translation", "both"), default="both"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL)
    parser.add_argument("--timeout", type=float, default=75.0)
    parser.add_argument("--assistant-hangup-ms", type=int, default=55000)
    parser.add_argument("--translation-hangup-ms", type=int, default=70000)
    parser.add_argument(
        "--audio-source",
        choices=("tone", "noise", "default", "fake-file"),
        default="tone",
    )
    parser.add_argument(
        "--fake-audio-file", help="WAV file to feed through Chromium's fake microphone"
    )
    parser.add_argument("--start-mode", choices=("click", "auto"), default="click")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument(
        "--browser-channel", help="Use an installed browser channel, e.g. chrome"
    )
    parser.add_argument("--skip-log-checks", action="store_true")
    parser.add_argument(
        "--artifacts-dir",
        help="Directory for JSON summaries, screenshots, and console output",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
