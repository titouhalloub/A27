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


def test_cross_fund_portfolio_unifies_traditional_and_islamic(client):
    """The actual claim being tested: one investor's portfolio spans a
    traditional loan AND an Islamic sukuk, from two different issuers, and
    the portfolio endpoint returns both in one response -- not two separate
    dashboards, not a per-fund view. This is the same unification thesis as
    the compliance gateway, proven on the investor-facing side.
    """
    r = client.post("/investors", headers=HEADERS,
                    json={"name": "Meridian Capital LP", "investor_type": "institution"})
    assert r.status_code == 201
    investor_id = r.json()["id"]

    # Fund A: a traditional loan.
    r = client.post("/instruments", headers=HEADERS, json={
        "transaction_type": "loan", "compliance_mode": "traditional",
        "issuer_name": "Alpha Manufacturing", "issuer_type": "Corporate",
        "amount": 2_500_000, "currency": "USD",
    })
    loan_id = r.json()["id"]
    r = client.post(f"/instruments/{loan_id}/holdings", headers=HEADERS,
                    json={"investor_id": investor_id, "stake_amount": 500_000,
                          "ownership_percentage": 20.0})
    assert r.status_code == 201

    # Fund B: an entirely separate Islamic sukuk, different issuer.
    r = client.post("/instruments", headers=HEADERS, json={
        "transaction_type": "sukuk", "compliance_mode": "islamic",
        "issuer_name": "Petra Energy Sukuk SPV", "issuer_type": "SPV",
        "amount": 500_000_000, "currency": "USD",
    })
    sukuk_id = r.json()["id"]
    r = client.post(f"/instruments/{sukuk_id}/holdings", headers=HEADERS,
                    json={"investor_id": investor_id, "stake_amount": 10_000_000})
    assert r.status_code == 201

    # One call, both funds, both tracks.
    r = client.get(f"/investors/{investor_id}/portfolio", headers=HEADERS)
    assert r.status_code == 200
    portfolio = r.json()

    assert portfolio["fund_count"] == 2
    assert portfolio["total_traditional_exposure"] == 500_000
    assert portfolio["total_islamic_exposure"] == 10_000_000
    held_instrument_ids = {h["instrument"]["id"] for h in portfolio["holdings"]}
    assert held_instrument_ids == {loan_id, sukuk_id}


def test_holding_requires_real_investor_and_instrument(client):
    r = client.post("/instruments", headers=HEADERS, json={
        "transaction_type": "loan", "compliance_mode": "traditional",
        "issuer_name": "Alpha Manufacturing", "issuer_type": "Corporate",
        "amount": 1_000_000, "currency": "USD",
    })
    instrument_id = r.json()["id"]

    r = client.post(f"/instruments/{instrument_id}/holdings", headers=HEADERS,
                    json={"investor_id": "does-not-exist", "stake_amount": 1000})
    assert r.status_code == 404

    r = client.post("/investors", headers=HEADERS,
                    json={"name": "Test LP", "investor_type": "individual"})
    investor_id = r.json()["id"]
    r = client.post("/instruments/does-not-exist/holdings", headers=HEADERS,
                    json={"investor_id": investor_id, "stake_amount": 1000})
    assert r.status_code == 404


def test_empty_portfolio_for_investor_with_no_holdings(client):
    r = client.post("/investors", headers=HEADERS,
                    json={"name": "New LP", "investor_type": "individual"})
    investor_id = r.json()["id"]
    r = client.get(f"/investors/{investor_id}/portfolio", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["fund_count"] == 0
    assert r.json()["holdings"] == []