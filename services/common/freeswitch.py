"""Shared FreeSWITCH ESL helpers."""

from __future__ import annotations

import asyncio
from typing import Any


async def inbound_connection(
    *,
    host: str,
    port: int,
    password: str,
    events: str,
    lock: asyncio.Lock,
    connections: dict[str, Any],
) -> Any:
    from genesis.inbound import Inbound

    async with lock:
        existing = connections.get(host)
        if existing is not None and getattr(existing, "is_connected", False):
            return existing
        ctl = Inbound(host, port, password)
        await ctl.start()
        await ctl.send(events)
        connections[host] = ctl
        return ctl
