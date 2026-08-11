"""Source-level contract helpers for the split Flask application.

Runtime compatibility intentionally keeps every extracted boundary in the
``app`` module namespace.  Tests that inspect source must therefore inspect the
whole executable bundle instead of assuming every function still lives in
``app.py``.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE_PATHS = (
    ROOT / "app.py",
    ROOT / "routes_core.py",
    ROOT / "ai_gemini.py",
    ROOT / "routes_calendar_dock.py",
    ROOT / "routes_dock_submit.py",
    ROOT / "routes_tail.py",
)


def read_app_sources():
    return "\n".join(path.read_text(encoding="utf-8") for path in APP_SOURCE_PATHS)
