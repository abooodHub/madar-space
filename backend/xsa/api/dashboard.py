"""Professional Web Dashboard HTML/JS for XSpace Analyzer.

Features:
  - Live on X Sidebar/Widget ("بث مباشر على X") with one-click direct link, recording trigger & preview.
  - Monitored Accounts with dynamic filtering, sorting (most recorded, recent, A-Z), search, and Tag system.
  - Multi-Space Active Recordings Grid (up to 10 concurrent slots with isolated channels & sinks).
  - Built-in Video Player Modal with HTTP Range seeking & thumbnail previews.
  - Interactive Space Inspector (Speaker timeline, Synced transcript, AI summary).
"""

from __future__ import annotations

from importlib.resources import files

from xsa.api.appearance import apply_appearance
from xsa.api.design_system import DESIGN_SYSTEM_CSS
from xsa.api.processing_ui import PROCESSING_UI_JS

_API_RESOURCES = files("xsa.api")
DASHBOARD_HTML = _API_RESOURCES.joinpath("templates", "dashboard.html").read_text(encoding="utf-8")
for marker, asset in (
    ("__XSA_DASHBOARD_CSS__", "dashboard.css"),
    ("__XSA_DASHBOARD_CONFIG_JS__", "dashboard-config.js"),
    ("__XSA_DASHBOARD_JS__", "dashboard.js"),
):
    DASHBOARD_HTML = DASHBOARD_HTML.replace(
        marker, _API_RESOURCES.joinpath("assets", asset).read_text(encoding="utf-8")
    )
DASHBOARD_HTML = DASHBOARD_HTML.replace("__XSA_DESIGN_SYSTEM__", DESIGN_SYSTEM_CSS)
DASHBOARD_HTML = DASHBOARD_HTML.replace("__XSA_PROCESSING_UI__", PROCESSING_UI_JS)
DASHBOARD_HTML = apply_appearance(DASHBOARD_HTML, "dashboard")
