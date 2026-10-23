from __future__ import annotations

import importlib
from types import SimpleNamespace

from xsa import runtime_metrics
from xsa.recorder import media


def test_windows_unrelated_process_command_line_is_never_inspected(monkeypatch, tmp_path):
    process = SimpleNamespace(
        info={"name": "System"}, cmdline=lambda: (_ for _ in ()).throw(OSError(87, "denied"))
    )
    monkeypatch.setattr("psutil.process_iter", lambda **_kwargs: [process])
    api = importlib.import_module("xsa.api.app")
    assert api._ffmpeg_role_counts() == {"recording": 0, "processing": 0}
    assert not media._capture_active(tmp_path)


def test_uninspectable_ffmpeg_prevents_claiming_safe_idleness(monkeypatch, tmp_path):
    process = SimpleNamespace(
        info={"name": "ffmpeg.exe"}, cmdline=lambda: (_ for _ in ()).throw(OSError(87, "denied"))
    )
    monkeypatch.setattr("psutil.process_iter", lambda **_kwargs: [process])
    api = importlib.import_module("xsa.api.app")
    assert api._recording_ffmpeg_count() > 0
    assert media._capture_active(tmp_path)


def test_cpu_requires_two_samples_and_labels_one_core_basis(monkeypatch):
    times = iter([10, 12])
    cpu = iter([2, 5])
    monkeypatch.setattr(runtime_metrics, "_previous", None)
    monkeypatch.setattr(runtime_metrics, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(
        runtime_metrics.psutil,
        "Process",
        lambda: SimpleNamespace(
            cpu_times=lambda: SimpleNamespace(user=next(cpu), system=0),
            memory_info=lambda: SimpleNamespace(rss=64 * 1024**2),
        ),
    )
    assert runtime_metrics.application_metrics()["cpu_percent_one_core"] is None
    metrics = runtime_metrics.application_metrics()
    assert metrics["cpu_percent_one_core"] == 150
    assert metrics["scope"] == "application_process" and metrics["rss_mb"] == 64
