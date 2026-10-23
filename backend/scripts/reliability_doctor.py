"""Disposable real FFmpeg/Chromium checks; never opens production data or X."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

from playwright.async_api import async_playwright

from xsa.browser.adapter import XBrowserAdapter, _Sel
from xsa.owned_process import run_owned_process
from xsa.recorder.media import create_playback_mp4, has_audio_video
from xsa.recorder.recorder import DockerRecorder, browser_audio_route_ready


def _native_hold(path):
    Path(path).write_text(str(os.getpid()))
    time.sleep(10)


async def main():
    evidence = {"synthetic_inputs_only": True}
    with tempfile.TemporaryDirectory(prefix="xsa-reliability-doctor-") as temporary:
        root = Path(temporary)
        recorder = DockerRecorder()
        graph = recorder._build_cmd(root / "recording_000.mkv")
        graph = graph[graph.index("-filter_complex") + 1]
        for label, audio in (
            ("silence", "anullsrc=r=48000:cl=stereo:d=4"),
            ("tone", "sine=frequency=440:sample_rate=48000:duration=4"),
        ):
            target = root / f"{label}.mkv"
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "info",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=5:duration=4",
                "-f",
                "lavfi",
                "-i",
                audio,
                "-t",
                "4",
                "-filter_complex",
                graph,
                "-map",
                "0:v",
                "-map",
                "[record_audio]",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "1",
                "-c:a",
                "aac",
                str(target),
            ]
            completed = subprocess.run(command, capture_output=True, timeout=30)
            if completed.returncode:
                raise RuntimeError(completed.stderr.decode(errors="replace")[-1500:])
            recorder = DockerRecorder()
            recorder._output = target
            recorder._proc = SimpleNamespace(poll=lambda: None)
            (root / "ffmpeg_0.log").write_bytes(completed.stderr)
            measured = recorder.get_audio_silence_status()
            assert measured["audio_measurement"] == "healthy"
            assert measured["audio_input_ready"]
            assert has_audio_video(target)
            evidence[label] = measured
        assert create_playback_mp4(target, root / "playback.mp4")
        assert create_playback_mp4(target, root / "playback.mp4")
        evidence["real_remux_idempotent"] = True
        started = root / "native-worker.pid"
        work = asyncio.create_task(run_owned_process(_native_hold, str(started)))
        async with asyncio.timeout(10):
            while not started.exists() or not started.stat().st_size:
                if work.done():
                    await work
                    raise RuntimeError("Native worker exited before progress")
                await asyncio.sleep(0.05)
        import psutil

        worker_pid = int(started.read_text())
        work.cancel()
        with suppress(asyncio.CancelledError):
            await work
        assert not psutil.pid_exists(worker_pid)
        evidence["real_native_worker_cancellation_reaped"] = True
        subprocess.run(
            ["pulseaudio", "--daemonize=yes", "--exit-idle-time=-1", "-n", "-F", "/etc/xsa/pulse-default.pa"],
            check=True,
            timeout=10,
        )
        subprocess.run(
            ["pactl", "load-module", "module-null-sink", "sink_name=xspace_doctor"],
            capture_output=True,
            check=True,
            timeout=5,
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-features=AudioServiceSandbox"],
                ignore_default_args=["--mute-audio"],
                env={**os.environ, "PULSE_SINK": "xspace_doctor"},
            )
            page = await browser.new_page()
            await page.set_content(
                '<button data-testid="audiospace_guest_list" onclick="window.opens=(window.opens||0)+1">participants</button>'
            )
            adapter = XBrowserAdapter(root)
            adapter._page = page
            for _ in range(10):
                await adapter.focusSpeakersView()
            style_count = await page.locator("#xsa-speakers-style").count()
            clicks = await page.evaluate("window.opens || 0")
            assert style_count == clicks == 1
            evidence["style_nodes_after_ten_updates"] = style_count
            evidence["participant_button_clicks"] = clicks
            await page.set_content('<div data-testid="SpaceDock"><p>You are listening anonymously</p></div>')
            await page.wait_for_selector(_Sel.ANONYMOUS_CONFIRMED_BANNER, state="visible", timeout=1000)
            evidence["scoped_anonymous_banner_selector_verified"] = True
            assert not browser_audio_route_ready("xspace_doctor")
            await page.set_content(
                '<button id="audio" onclick="window.ctx=new AudioContext();window.osc=ctx.createOscillator();osc.connect(ctx.destination);osc.start();ctx.resume()">start</button>'
            )
            await page.locator("#audio").click()
            for _ in range(20):
                if browser_audio_route_ready("xspace_doctor"):
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Chrome audio route was not verified")
            evidence["real_chrome_audio_route_verified"] = True
            assert not browser_audio_route_ready("unrelated_sink")
            await browser.close()
    print(json.dumps(evidence, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
