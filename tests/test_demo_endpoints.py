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


def _normalise(path: str) -> str:
    """Turn a JS path with template placeholders into a FastAPI route path."""
    path = path.split("?")[0]
    path = path.replace("${iid}", "{instrument_id}")
    path = path.replace("${currentInstrumentId}", "{instrument_id}")
    path = path.replace(
        "${encodeURIComponent(CT_ISSUER)}", "{issuer_name}"
    )
    return path


def test_every_demo_endpoint_exists_on_the_api():
    valid = {r.path for r in app.routes if hasattr(r, "methods")}
    missing = []
    for raw in _demo_api_paths():
        if not raw.startswith("/"):
            continue
        path = _normalise(raw)
        if path not in valid:
            missing.append((raw, path))
    assert not missing, f"demo calls missing API routes: {missing}"