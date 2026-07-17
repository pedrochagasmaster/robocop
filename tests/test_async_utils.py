from __future__ import annotations

import asyncio
import importlib
import importlib.util


def test_await_uncancellable_preserves_result_across_repeated_cancellation() -> None:
    spec = importlib.util.find_spec("dispatch.asyncio_utils")
    assert spec is not None, "dispatch.asyncio_utils must centralize task handoff"
    async_utils = importlib.import_module("dispatch.asyncio_utils")

    async def run() -> str:
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation() -> str:
            started.set()
            await release.wait()
            return "finished"

        async def handoff() -> str:
            task = asyncio.create_task(operation())
            return await async_utils.await_uncancellable(task)

        wrapper = asyncio.create_task(handoff())
        await started.wait()
        wrapper.cancel()
        await asyncio.sleep(0)
        wrapper.cancel()
        release.set()
        return await wrapper

    assert asyncio.run(run()) == "finished"
