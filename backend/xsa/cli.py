"""Command-line entry point for running the XSpace Analyzer API."""

import uvicorn

from xsa.config import get_settings


def main() -> None:
    """Start the configured API server."""
    cfg = get_settings()
    uvicorn.run(
        "xsa.api.app:app",
        host=cfg.bind_host,
        port=cfg.api_port,
        log_level=cfg.log_level.lower(),
        reload=False,
    )
