"""Shared FreeSWITCH ESL command construction helpers."""

from __future__ import annotations

import re


_SIP_AOR_RE = re.compile(
    r"^(?:sip:)?[A-Za-z0-9._+%-]+@(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+(?::[0-9]{1,5})?$"
)
_UNSAFE_COMMAND_CHARS = set("{}[];,&|`$\\\"'<>\r\n\t ")


def build_uuid_break_command(fs_uuid: str) -> str:
    return f"uuid_break {fs_uuid} all"


def validate_translation_peer_aor(peer_aor: str) -> str:
    """Return a safe SIP AOR for FreeSWITCH originate command construction."""
    clean_peer = str(peer_aor or "").strip()
    if not clean_peer:
        raise ValueError("translation peer must be a SIP AOR such as sip:bob@voice.local")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in clean_peer):
        raise ValueError("translation peer contains unsupported control characters")
    if any(ch in _UNSAFE_COMMAND_CHARS for ch in clean_peer):
        raise ValueError("translation peer contains unsupported characters")
    if not _SIP_AOR_RE.fullmatch(clean_peer):
        raise ValueError("translation peer must be a SIP AOR such as sip:bob@voice.local")
    if not clean_peer.startswith("sip:"):
        clean_peer = f"sip:{clean_peer}"
    _, _, port = clean_peer.rpartition(":")
    if port.isdigit() and not (1 <= int(port) <= 65535):
        raise ValueError("translation peer port must be between 1 and 65535")
    return clean_peer


def build_originate_translation_leg_command(
    *,
    peer_aor: str,
    peer_uuid: str,
    fs_path: str,
    caller_id_number: str = "7100",
) -> str:
    clean_peer = validate_translation_peer_aor(peer_aor)
    clean_fs_path = fs_path.strip()
    variables = ",".join(
        [
            f"origination_uuid={peer_uuid}",
            "origination_caller_id_name=Translator",
            f"origination_caller_id_number={caller_id_number}",
            "ignore_early_media=true",
            "sip_h_X-type=to_registered",
        ]
    )
    return f"originate {{{variables}}}sofia/external/{clean_peer};fs_path={clean_fs_path} &park()"


def normalize_audio_stream_rate(sample_rate: str) -> str:
    clean = (sample_rate or "").strip().lower()
    aliases = {
        "8k": "8000",
        "8khz": "8000",
        "8000": "8000",
        "16k": "16000",
        "16khz": "16000",
        "16000": "16000",
    }
    try:
        return aliases[clean]
    except KeyError as exc:
        raise ValueError("mod_audio_stream sample rate must be 8000 or 16000") from exc


def build_audio_stream_start_command(
    fs_uuid: str,
    websocket_url: str,
    *,
    mix_type: str = "mono",
    sample_rate: str = "16000",
    metadata: str | None = None,
) -> str:
    command = (
        f"uuid_audio_stream {fs_uuid} start "
        f"{websocket_url} {mix_type} {normalize_audio_stream_rate(sample_rate)}"
    )
    if metadata:
        command = f"{command} {metadata}"
    return command


def build_audio_stream_stop_command(fs_uuid: str) -> str:
    return f"uuid_audio_stream {fs_uuid} stop"
