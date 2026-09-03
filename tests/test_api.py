"""API layer tests -- auth gate, the full instrument/document/evidence/review
flow, exercised the same way a real client would: over HTTP, not by calling
pipeline functions directly.

Uses FastAPI dependency overrides rather than environment variables for
test config: app.db and app.config both create module-level singletons
(engine, settings) at import time, and conftest.py's own imports trigger
that before any per-test env var would take effect. Overriding the
dependencies directly is the correct, order-independent way to isolate this.
"""
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, get_session
from app.main import app, require_api_key

SUKUK_TEXT = """SUKUK CERTIFICATE
Issuer: Petra Energy Sukuk SPV
Total Issue Size: USD 500,000,000
Structure: al-Ijara
Underlying Asset: a portfolio of income-generating logistics warehouses
Profit Rate: 4.25% per annum
Shariah Committee: Approved per fatwa reference FA-2024-011
"""

HEADERS = {"X-API-Key": "test-key-123"}


@pytest.fixture
def client(tmp_path) -> Generator[TestClient, None, None]:
    engine = create_engine(f"sqlite:///{tmp_path / 'api_test.db'}", future=True)
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override_get_session() -> Generator[Session, None, None]:
        with TestSessionLocal() as session:
            yield session

    def _override_require_api_key(key: str | None = None) -> str:
        # Mirrors the real dependency's shape closely enough for testing the
        # routes; the real auth-gate *logic* (fail-closed on no configured
        # keys, constant-time comparison) is exercised separately below by
        # calling the real dependency function directly, not through HTTP.
        return "test-key-123"

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[require_api_key] = _override_require_api_key
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_health_needs_no_auth(client):
    assert client.get("/health").status_code == 200


def test_auth_gate_fails_closed_when_no_keys_configured():
    """The real auth dependency, called directly -- not through the
    overridden-for-testing route -- must fail closed (503), never silently
    open, when no keys are configured on the server."""
    from fastapi import HTTPException

    from app.config import get_settings
    from app.main import require_api_key as real_require_api_key

    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as exc_info:
            real_require_api_key(key="anything")
        assert exc_info.value.status_code == 503
    finally:
        get_settings.cache_clear()


def test_auth_gate_rejects_wrong_key(monkeypatch):
    from fastapi import HTTPException

    from app.config import settings
    from app.main import require_api_key as real_require_api_key

    monkeypatch.setattr(settings, "api_keys", "correct-key")
    with pytest.raises(HTTPException) as exc_info:
        real_require_api_key(key="wrong-key")
    assert exc_info.value.status_code == 401


def test_auth_gate_accepts_correct_key(monkeypatch):
    from app.config import settings
    from app.main import require_api_key as real_require_api_key

    monkeypatch.setattr(settings, "api_keys", "correct-key")
    assert real_require_api_key(key="correct-key") == "correct-key"


def test_full_sukuk_flow_blocks_then_pends_after_evidence(client):
    r = client.post("/instruments", headers=HEADERS, json={
        "transaction_type": "sukuk", "compliance_mode": "islamic",
        "issuer_name": "Petra Energy Sukuk SPV", "issuer_type": "SPV",
        "amount": 500_000_000, "currency": "USD",
    })
    assert r.status_code == 201
    iid = r.json()["id"]

    # Before any fatwa is attached: correctly blocked, not a guess.
    r = client.post(f"/instruments/{iid}/documents", headers=HEADERS,
                     json={"text": SUKUK_TEXT, "filename": "sukuk.txt"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "system_flagged_noncompliant"
    assert r.json()["instrument"]["shariah_contract_type"] == "ijara"

    # Attach the fatwa as evidence -- no extraction attempted on it.
    r = client.post(f"/instruments/{iid}/evidence", headers=HEADERS, json={
        "text": "Fatwa FA-2024-011: approved as Shariah-compliant.",
        "document_type": "fatwa", "filename": "fatwa.pdf",
    })
    assert r.status_code == 201
    assert r.json()["document_type"] == "fatwa"

    # Re-run: now correctly pends for scholar review, never auto-approved.
    r = client.post(f"/instruments/{iid}/documents", headers=HEADERS,
                     json={"text": SUKUK_TEXT, "filename": "sukuk-v2.txt"})
    assert r.json()["outcome"] == "pending_scholar_review"

    # Ledger has a real trail.
    r = client.get(f"/instruments/{iid}/ledger", headers=HEADERS)
    assert len(r.json()) >= 3

    # Human review is the only path to approval, and it's auditable.
    r = client.post(f"/instruments/{iid}/review", headers=HEADERS, json={
        "reviewer_id": "scholar-001", "decision": "approved",
        "notes": "Verified against AAOIFI Standard 62.",
    })
    assert r.status_code == 200
    assert r.json()["shariah_review_status"] == "scholar_approved"


def test_review_rejects_instrument_never_routed(client):
    r = client.post("/instruments", headers=HEADERS, json={
        "transaction_type": "loan", "compliance_mode": "traditional",
        "issuer_name": "Alpha Manufacturing", "issuer_type": "Corporate",
        "amount": 2_500_000, "currency": "USD",
    })
    iid = r.json()["id"]
    # NOT_APPLICABLE (traditional, untouched) is not a reviewable state.
    r = client.post(f"/instruments/{iid}/review", headers=HEADERS, json={
        "reviewer_id": "scholar-001", "decision": "approved", "notes": "",
    })
    assert r.status_code == 400


def test_get_nonexistent_instrument_404s(client):
    r = client.get("/instruments/does-not-exist", headers=HEADERS)
    assert r.status_code == 404