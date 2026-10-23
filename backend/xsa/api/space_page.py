"""The source-backed recording and analysis workspace."""

from importlib.resources import files

from xsa.api.appearance import apply_appearance
from xsa.api.design_system import DESIGN_SYSTEM_CSS

_API_RESOURCES = files("xsa.api")
SPACE_ANALYSIS_HTML = _API_RESOURCES.joinpath("templates", "studio.html").read_text(encoding="utf-8")
SPACE_ANALYSIS_HTML = SPACE_ANALYSIS_HTML.replace(
    "__XSA_STUDIO_CSS__", _API_RESOURCES.joinpath("assets", "studio.css").read_text(encoding="utf-8")
)
SPACE_ANALYSIS_HTML = SPACE_ANALYSIS_HTML.replace(
    "__XSA_STUDIO_JS__", _API_RESOURCES.joinpath("assets", "studio.js").read_text(encoding="utf-8")
)
SPACE_ANALYSIS_HTML = SPACE_ANALYSIS_HTML.replace("__XSA_DESIGN_SYSTEM__", DESIGN_SYSTEM_CSS)
SPACE_ANALYSIS_HTML = apply_appearance(SPACE_ANALYSIS_HTML, "studio")
