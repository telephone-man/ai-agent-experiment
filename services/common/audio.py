"""Small PCM helpers used by the streaming STT path."""

from __future__ import annotations

import math
import struct


def pcm16_rms(data: bytes) -> float:
    """Return RMS amplitude for little-endian signed 16-bit mono PCM."""
    if len(data) < 2:
        return 0.0
    sample_count = len(data) // 2
    total = 0
    for (sample,) in struct.iter_unpack("<h", data[: sample_count * 2]):
        total += sample * sample
    return math.sqrt(total / sample_count)


def pcm16_resample_mono(data: bytes, source_rate: int, target_rate: int) -> bytes:
    """
    Resample signed 16-bit mono PCM using linear interpolation.

    This avoids depending on audioop, which is removed in newer Python versions.
    It is adequate for feeding speech recognition; FreeSWITCH/RTPengine still own
    production media quality.
    """
    if source_rate == target_rate or not data:
        return data
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")

    sample_count = len(data) // 2
    if sample_count == 0:
        return b""
    samples = [
        sample for (sample,) in struct.iter_unpack("<h", data[: sample_count * 2])
    ]
    if len(samples) == 1:
        return struct.pack("<h", samples[0])

    output_count = max(1, int(round(len(samples) * target_rate / source_rate)))
    ratio = source_rate / target_rate
    out = bytearray()
    last_index = len(samples) - 1
    for out_index in range(output_count):
        src_pos = out_index * ratio
        left = min(int(src_pos), last_index)
        right = min(left + 1, last_index)
        frac = src_pos - left
        value = int(samples[left] + (samples[right] - samples[left]) * frac)
        out.extend(struct.pack("<h", max(-32768, min(32767, value))))
    return bytes(out)
