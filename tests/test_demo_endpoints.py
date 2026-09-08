"""Verify the static demo (static/index.html) only calls endpoints that
actually exist in the API -- a regression guard so the demo can never
silently render error boxes because a route it depends on was renamed or
never built."""
import re
from pathlib import Path

from app.main import app

HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _demo_api_paths() -> set[str]:
    html = HTML.read_text(encoding="utf-8")
    calls: set[str] = set()
    # apiCall('/literal/path') and apiCall(`/template/${x}/path`)
    calls |= set(re.findall(r"apiCall\(\s*['\"]([^'\"]+)['\"]", html))
    calls |= set(re.findall(r"apiCall\(`([^`]+)`", html))
    # bare fetches like fetch(`${API_BASE}/health`)
    calls |= set(re.findall(r"fetch\([^)]*/([a-z-]+)", html))
    return calls


def _shape(path: str) -> str:
    """Collapse every placeholder -- a JS ``${...}`` interpolation or a
    FastAPI ``{param}`` -- to a bare ``{}``, so a template-literal call
    compares positionally against the real route no matter what either
    side names its parameter. Query strings are dropped (they carry no
    routing information)."""
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path


def test_every_demo_endpoint_exists_on_the_api():
    valid_shapes = {
        _shape(r.path) for r in app.routes if hasattr(r, "methods")
    }
    missing = []
    for raw in _demo_api_paths():
        if not raw.startswith("/"):
            continue
        path = _shape(raw)
        if path in valid_shapes:
            continue
        # JS string concatenation fragments ('/cap-table/' + encodeURIComponent(...))
        # are captured as a trailing-slash prefix of the real parametrised route.
        if path.endswith("/") and any(s.startswith(path) for s in valid_shapes):
            continue
        missing.append((raw, path))
    assert not missing, f"demo calls missing API routes: {missing}"