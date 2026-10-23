"""Cancellation barriers for synchronous work that owns writable resources."""

from __future__ import annotations

import asyncio


async def run_blocking(function, /, *args, **kwargs):
    """Keep ownership until the thread exits, including repeated cancellation.

    Python cannot kill a native thread safely. Cancellation is acknowledged only
    after it has stopped; callers must keep their job/slot lock until this returns.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            continue
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result
