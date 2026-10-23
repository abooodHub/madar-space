"""Source-backed operations pages shared by the dashboard and studio."""

from importlib.resources import files

from xsa.api.appearance import apply_appearance
from xsa.api.design_system import DESIGN_SYSTEM_CSS
from xsa.api.processing_ui import PROCESSING_UI_JS

SECTIONS = frozenset(
    {"live", "manual", "library", "accounts", "processing", "analytics", "health", "settings"}
)

_API_RESOURCES = files("xsa.api")
WORKSPACE_HTML = _API_RESOURCES.joinpath("templates", "workspace.html").read_text(encoding="utf-8")
WORKSPACE_HTML = WORKSPACE_HTML.replace(
    "__XSA_WORKSPACE_CSS__",
    _API_RESOURCES.joinpath("assets", "workspace.css").read_text(encoding="utf-8"),
)
WORKSPACE_HTML = WORKSPACE_HTML.replace(
    "__XSA_WORKSPACE_JS__",
    _API_RESOURCES.joinpath("assets", "workspace.js").read_text(encoding="utf-8"),
)
WORKSPACE_HTML = WORKSPACE_HTML.replace("__XSA_DESIGN_SYSTEM__", DESIGN_SYSTEM_CSS)
WORKSPACE_HTML = WORKSPACE_HTML.replace("__XSA_PROCESSING_UI__", PROCESSING_UI_JS)
WORKSPACE_HTML = apply_appearance(WORKSPACE_HTML, "workspace")
