from __future__ import annotations

from types import SimpleNamespace

from xsa import cli


def test_cli_starts_uvicorn_with_application_settings(monkeypatch) -> None:
    settings = SimpleNamespace(bind_host="127.0.0.1", api_port=8765, log_level="INFO")
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli.main()

    assert calls == [
        (
            ("xsa.api.app:app",),
            {"host": "127.0.0.1", "port": 8765, "log_level": "info", "reload": False},
        )
    ]
