"""Display & Audio Slot Pool for Concurrent Multi-Space Video Recording.

Enables recording multiple Spaces at the exact same time without audio/video crosstalk:
  - Slot 0+: Dedicated displays :100, :101... + private PulseAudio null sinks per recording

Each slot guarantees:
  1. Complete visual isolation (dedicated Xvfb framebuffer at the configured size).
  2. Complete audio isolation (dedicated PulseAudio null sink monitor).
  3. Seamless cleanup on session termination.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

from xsa.logging_setup import get_logger

log = get_logger("SLOT_POOL")


@dataclass
class DisplaySlot:
    slot_id: int
    display: str
    sink_name: str
    is_busy: bool = False
    pulse_module_id: int | None = None
    xvfb_proc: subprocess.Popen | None = None
    x11vnc_proc: subprocess.Popen | None = None
    websockify_proc: subprocess.Popen | None = None

    async def acquire(self) -> None:
        """Start an isolated Xvfb and PulseAudio sink for this recording slot."""
        self.is_busy = True
        log.info("Spawning isolated slot", slot_id=self.slot_id, display=self.display, sink=self.sink_name)

        # 1. Create dynamic PulseAudio null sink
        try:
            cmd = [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={self.sink_name}",
                "rate=48000",
                "channels=2",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            communication = asyncio.create_task(proc.communicate())
            cancelled = False
            try:
                stdout, stderr = await asyncio.shield(communication)
            except asyncio.CancelledError:
                cancelled = True
                stdout, stderr = await communication
            if proc.returncode == 0:
                self.pulse_module_id = int(stdout.decode().strip())
                log.info("Created PulseAudio sink", sink=self.sink_name, module_id=self.pulse_module_id)
            else:
                raise RuntimeError(
                    f"Could not create PulseAudio sink {self.sink_name}: "
                    f"{stderr.decode(errors='replace').strip()}"
                )
            if cancelled:
                raise asyncio.CancelledError
        except Exception as e:
            log.warning("Pulse sink creation error", error=str(e))
            raise

        # 2. Launch Xvfb virtual display
        try:
            from xsa.config import get_settings

            cfg = get_settings()
            xvfb_cmd = [
                "Xvfb",
                self.display,
                "-screen",
                "0",
                f"{cfg.record_size}x24",
                "-nolisten",
                "tcp",
                "-ac",
            ]
            self.xvfb_proc = subprocess.Popen(
                xvfb_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(0.5)
            if self.xvfb_proc.poll() is not None:
                raise RuntimeError(f"Xvfb exited while starting display {self.display}")
            self._verify_display()
            self._verify_audio_sink()
            # Expose this isolated recording display through its own loopback-only
            # noVNC port so the operator can inspect the actual capture browser.
            vnc_port = 5901 + self.slot_id
            web_port = 6081 + self.slot_id
            self.x11vnc_proc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    self.display,
                    "-forever",
                    "-shared",
                    "-nopw",
                    "-localhost",
                    "-rfbport",
                    str(vnc_port),
                    "-quiet",
                    "-noxdamage",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(0.3)
            if self.x11vnc_proc.poll() is not None:
                raise RuntimeError(f"x11vnc exited while starting viewer for {self.display}")
            self.websockify_proc = subprocess.Popen(
                ["websockify", "--web", "/usr/share/novnc", str(web_port), f"localhost:{vnc_port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(0.3)
            if self.websockify_proc.poll() is not None:
                raise RuntimeError(f"websockify exited while starting viewer on port {web_port}")
            log.info("Xvfb display started", display=self.display, pid=self.xvfb_proc.pid)
        except Exception as e:
            log.error("Failed to start Xvfb display", display=self.display, error=str(e))
            raise

    def _verify_display(self) -> None:
        result = subprocess.run(
            ["xdpyinfo", "-display", self.display],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"X display {self.display} is not accepting connections")

    def _verify_audio_sink(self) -> None:
        for target, kind, label in (
            (self.sink_name, "sinks", "sink"),
            (f"{self.sink_name}.monitor", "sources", "monitor"),
        ):
            result = subprocess.run(
                ["pactl", "list", "short", kind],
                capture_output=True,
                text=True,
                timeout=5,
            )
            available = (
                {fields[1] for line in result.stdout.splitlines() if len(fields := line.split()) > 1}
                if result.returncode == 0
                else set()
            )
            if target not in available:
                raise RuntimeError(f"PulseAudio {label} {target} is unavailable")

    def check_resources(self) -> None:
        """Verify the owned display and private audio route before recapturing."""
        if not self.is_busy or not self.xvfb_proc or self.xvfb_proc.poll() is not None:
            raise RuntimeError(f"Reserved Xvfb display {self.display} is not running")
        self._verify_display()
        self._verify_audio_sink()

    async def release(self) -> None:
        """Tear down dynamic slot resources."""
        log.info("Tearing down slot", slot_id=self.slot_id, display=self.display)

        for proc_name in ("websockify_proc", "x11vnc_proc"):
            proc = getattr(self, proc_name)
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                if proc.poll() is None:
                    raise RuntimeError(f"{proc_name} is still running; slot remains quarantined")
                setattr(self, proc_name, None)

        # 1. Kill Xvfb
        if self.xvfb_proc:
            try:
                self.xvfb_proc.terminate()
                self.xvfb_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.xvfb_proc.kill()
                self.xvfb_proc.wait(timeout=3)
            if self.xvfb_proc.poll() is None:
                raise RuntimeError("Xvfb is still running; slot remains quarantined")
            self.xvfb_proc = None

        # 2. Unload Pulse sink module
        if self.pulse_module_id is not None:
            try:
                cmd = ["pactl", "unload-module", str(self.pulse_module_id)]
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(stderr.decode(errors="replace").strip() or "pactl unload failed")
                log.info("Unloaded PulseAudio sink module", module_id=self.pulse_module_id)
            except Exception as e:
                log.warning("Failed to unload pulse module; slot remains quarantined", error=str(e))
                raise
            self.pulse_module_id = None
        self.is_busy = False


class DisplaySlotPool:
    """Manages a pool of virtual displays and isolated audio sinks."""

    def __init__(self, max_slots: int = 10, base_display_num: int = 100) -> None:
        self.max_slots = max_slots
        self.base_display_num = base_display_num
        self._slots: dict[int, DisplaySlot] = {}
        self._lock = asyncio.Lock()

        # Initialize slot representations
        for i in range(max_slots):
            disp = f":{base_display_num + i}"
            sink = f"xspace_slot{i}"
            self._slots[i] = DisplaySlot(slot_id=i, display=disp, sink_name=sink)

    async def acquire_slot(self) -> DisplaySlot | None:
        """Find an idle slot, initialize its virtual hardware, and return it."""
        async with self._lock:
            for slot_id in range(self.max_slots):
                slot = self._slots[slot_id]
                if not slot.is_busy:
                    try:
                        await slot.acquire()
                        return slot
                    except BaseException as e:
                        log.error("Failed to acquire slot", slot_id=slot_id, error=str(e))
                        await slot.release()
                        if isinstance(e, asyncio.CancelledError):
                            raise
            log.warning("All recording slots are currently busy", max_slots=self.max_slots)
            return None

    async def release_slot(self, slot: DisplaySlot | None) -> bool:
        """Release slot back to the idle pool."""
        if not slot:
            return True
        async with self._lock:
            try:
                await slot.release()
                return True
            except Exception as e:
                log.warning("Error releasing slot", slot_id=slot.slot_id, error=str(e))
                return False

    async def recover_slot(self, slot: DisplaySlot) -> None:
        """Recreate owned resources while keeping the reservation exclusive.

        The session must stop its FFmpeg and close its browser before calling.
        Failed cleanup or recreation quarantines the slot instead of recycling it.
        """
        async with self._lock:
            if self._slots.get(slot.slot_id) is not slot or not slot.is_busy:
                raise RuntimeError("Cannot recover a slot without its current reservation")
            try:
                await slot.release()
                await slot.acquire()
            except BaseException:
                slot.is_busy = True
                raise


# Global slot pool singleton
_pool: DisplaySlotPool | None = None


def get_slot_pool(max_slots: int | None = None) -> DisplaySlotPool:
    global _pool
    if _pool is None:
        _pool = DisplaySlotPool(max_slots=max_slots or 10)
    elif max_slots is not None and _pool.max_slots < max_slots:
        # Keep existing reservations/process handles when capacity is expanded.
        for index in range(_pool.max_slots, max_slots):
            _pool._slots[index] = DisplaySlot(
                slot_id=index,
                display=f":{_pool.base_display_num + index}",
                sink_name=f"xspace_slot{index}",
            )
        _pool.max_slots = max_slots
    return _pool
