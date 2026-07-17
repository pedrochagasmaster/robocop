"""Small cancellation-safe asyncio handoff primitives."""

from __future__ import annotations

import asyncio
from typing import TypeVar

T = TypeVar("T")


async def await_uncancellable(task: asyncio.Task[T]) -> T:
    """Wait for one already-started task despite repeated caller cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
