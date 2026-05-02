"""Shared helpers for deterministic eval runners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def format_row(row: tuple[str, ...], widths: list[int]) -> str:
    return "  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))


def write_report_pair(
    reports_dir: Path, stem: str, payload: dict[str, Any], markdown: str
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{stem}.json"
    md_path = reports_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
