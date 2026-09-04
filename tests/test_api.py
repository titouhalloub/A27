"""API layer tests -- auth gate, the full instrument/document/evidence/review
flow, exercised the same way a real client would: over HTTP, not by calling
pipeline functions directly.

Uses FastAPI dependency overrides rather than environment variables for
test config: app.db and app.config both create module-level singletons
(engine, settings) at import time, and conftest.py's own imports trigger
that before any per-test env var would take effect. Overriding the
dependencies directly is the correct, order-independent way to isolate this.

The ``client`` fixture (shared scratch DB + mocked auth) lives in conftest.py.
"""
import pytest

from app.main import require_api_key

SUKUK_TEXT = """SUKUK CERTIFICATE
Issuer: Petra Energy Sukuk SPV
Total Issue Size: USD 500,000,000
Structure: al-Ijara
Underlying Asset: a portfolio of income-generating logistics warehouses
Profit Rate: 4.25% per annum
Shariah Committee: Approved per fatwa reference FA-2024-011
"""

HEADERS = {"X-API-Key": "test-key-123"}


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


def test_cap_table_series_a_dilution_over_http(client):
    """Same claim as the unit test, exercised end to end over HTTP: a
    Series A round dilutes the founder, and the API computes and returns
    the correct percentages -- not something a client has to calculate."""
    from datetime import datetime, timedelta, timezone

    r = client.post("/investors", headers=HEADERS,
                    json={"name": "Founder", "investor_type": "individual"})
    founder_id = r.json()["id"]
    r = client.post("/investors", headers=HEADERS,
                    json={"name": "Series A Fund", "investor_type": "institution"})
    vc_id = r.json()["id"]

    r = client.post("/securities", headers=HEADERS, json={
        "issuer_name": "Acme Inc", "name": "Common Stock",
        "security_type": "common", "authorized_shares": 10_000_000,
    })
    common_id = r.json()["id"]
    r = client.post("/securities", headers=HEADERS, json={
        "issuer_name": "Acme Inc", "name": "Series A Preferred",
        "security_type": "preferred", "authorized_shares": 5_000_000,
    })
    preferred_id = r.json()["id"]

    t0 = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    t1 = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()

    r = client.post("/cap-table-events", headers=HEADERS, json={
        "security_id": common_id, "event_type": "issuance",
        "holder_id": founder_id, "quantity": 8_000_000, "effective_date": t0,
    })
    assert r.status_code == 201

    r = client.get("/cap-table/Acme%20Inc", headers=HEADERS)
    assert r.json()["positions"][0]["ownership_percent"] == 100.0

    r = client.post("/cap-table-events", headers=HEADERS, json={
        "security_id": preferred_id, "event_type": "issuance",
        "holder_id": vc_id, "quantity": 2_000_000, "price_per_share": 5.0,
        "effective_date": t1,
    })
    assert r.status_code == 201

    r = client.get("/cap-table/Acme%20Inc", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total_fully_diluted_shares"] == 10_000_000
    by_holder = {p["holder_id"]: p["ownership_percent"] for p in body["positions"]}
    assert by_holder[founder_id] == 80.0
    assert by_holder[vc_id] == 20.0

    # Bad event (overdraft) is rejected at write time with 400, not
    # silently accepted and only caught on the next unrelated read.
    r = client.post("/cap-table-events", headers=HEADERS, json={
        "security_id": common_id, "event_type": "cancellation",
        "from_holder_id": founder_id, "quantity": 999_999_999,
        "effective_date": t1,
    })
    assert r.status_code == 400