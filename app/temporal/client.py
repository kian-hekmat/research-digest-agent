"""A process-wide Temporal client, connected lazily on first use and reused.

`get_temporal_client` doubles as a FastAPI dependency - override it in tests
to point at a `WorkflowEnvironment`'s client instead of a real server.
"""
from __future__ import annotations

import asyncio

from temporalio.client import Client

from app.config import get_settings

_client: Client | None = None
_connect_lock = asyncio.Lock()


async def get_temporal_client() -> Client:
    global _client
    if _client is None:
        async with _connect_lock:
            if _client is None:
                settings = get_settings()
                _client = await Client.connect(
                    settings.temporal_address, namespace=settings.temporal_namespace
                )
    return _client
