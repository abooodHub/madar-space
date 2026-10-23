"""Disposable native worker: cancellation reaps the owner before acknowledging."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import sys
from contextlib import suppress

from xsa.blocking import run_blocking


def _child(connection, function, args, parent_pid):
    if sys.platform.startswith("linux"):
        import ctypes

        os.setsid()
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)
        os.nice(10)
    try:
        connection.send((True, function(*args)))
    except BaseException as exc:
        connection.send((False, (type(exc).__name__, str(exc))))
    finally:
        connection.close()


async def run_owned_process(function, *args):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_child, args=(sender, function, args, os.getpid()), daemon=True)
    worker.start()
    sender.close()
    try:
        while worker.is_alive() and not receiver.poll():
            await asyncio.sleep(0.1)
        if not receiver.poll():
            raise RuntimeError(f"Native worker exited without result (code {worker.exitcode})")
        success, payload = await run_blocking(receiver.recv)
        if not success:
            raise RuntimeError(f"{payload[0]}: {payload[1]}")
        return payload
    finally:
        if worker.is_alive():
            if sys.platform.startswith("linux"):
                try:
                    os.killpg(worker.pid, signal.SIGTERM)
                except ProcessLookupError:
                    worker.terminate()
            else:
                worker.terminate()
        await run_blocking(worker.join, 5)
        if worker.is_alive():
            worker.kill()
            await run_blocking(worker.join, 5)
        if sys.platform.startswith("linux"):
            # Reap any FFmpeg grandchildren in the owned session too, including
            # the case where the native parent crashed before cleanup.
            with suppress(ProcessLookupError):
                os.killpg(worker.pid, signal.SIGKILL)
        receiver.close()
        if worker.is_alive():
            raise RuntimeError("Native worker was not reaped; ownership must remain quarantined")
