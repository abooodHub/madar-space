"""Strict Space URL parsing for operator recording requests."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def space_id_from_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid Space URL") from exc
    if (
        parts.scheme != "https"
        or parts.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise ValueError("Use an HTTPS x.com or twitter.com Space link")
    match = re.fullmatch(r"/i/spaces/([A-Za-z0-9]{1,100})(?:/peek)?/?", parts.path)
    if not match:
        raise ValueError("Invalid Space URL path")
    return match.group(1)
