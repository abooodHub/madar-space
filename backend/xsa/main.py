"""Compatibility module exposing the API app and command-line runner."""

from xsa.api.app import app  # noqa: F401 - exported as the ASGI application
from xsa.cli import main

if __name__ == "__main__":
    main()
