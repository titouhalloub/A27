"""Document -> cap table: the unification test.

The pipeline classifies, extracts, and backfills the deal container. These
tests prove the remaining link: a processed equity-subscription document can
be turned into a prepared cap-table proposal (inert -- no ledger writes),
and a named human's approval materializes it as a real CapTableEvent, so the
issuer's cap table reflects the document without anyone re-typing a number.
Just as important: the guardrails survive -- reviewed documents, missing
fields, and unresolved subscribers can never reach the ledger.
"""
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.models.enums import (
    ComplianceMode,
    DocumentStatus,
    DocumentType,
    TransactionType,
)
from app.models.orm import Document, Instrument, Investor

client = TestClient(app)


def _client_session():
    """A session on the *client fixture's* scratch DB (the dependency
    override's own session factory), so seeded rows and HTTP calls share
    one database."""
    return next(app.dependency_overrides[get_session]())


def _container_instrument(session, issuer_name: str) -> Instrument:
    """The deal container every document belongs to (documents.instrument_id
    is NOT NULL -- the pipeline always runs inside an instrument context)."""
    instrument = Instrument(
        id=str(uuid4()),
        transaction_type=TransactionType.EQUITY,
        compliance_mode=ComplianceMode.TRADITIONAL,
        issuer_name=issuer_name,
        issuer_type="Corporate",
        amount=0.0,
        currency="USD",
    )
    session.add(instrument)
    session.commit()
    return instrument


def _investview_extracted_document(session) -> Document:
    """A document that cleared both gates, holding the Investview extraction."""
    instrument = _container_instrument(session, "Demo Issuer")
    doc = Document(
        id=str(uuid4()),
        instrument_id=instrument.id,
        filename="investview-ex41.pdf",
        file_url="uploads/investview-ex41.pdf",
        compliance_mode=ComplianceMode.TRADITIONAL,
        document_type=DocumentType.EQUITY_SUBSCRIPTION,
        status=DocumentStatus.PROCESSED,
        classification_confidence=0.765,
        extraction_confidence=0.871,
        extracted_data={
            "schema_name": "EquitySubscriptionExtraction",
            "schema_version": "v1",
            "data": {
                "schema_name": "EquitySubscriptionExtraction",
                "schema_version": "v1",
                "extracted_at": "2015-05-29T00:00:00",
                "company_name": "INVESTVIEW, INC.",
                "state_of_incorporation": "Nevada",
                "security_type": "Series A Preferred Stock",
                "price_per_unit": None,
                "total_offering_amount": 5000000.0,
                "minimum_investment": None,
                "currency": "USD",
                "share_count": 100000,
                "subscription_price_per_share": 50.0,
                "investment_amount": 5000000.0,
                "accredited_investor_category": None,
                "document_date": "2015-05-29",
                "subscriber_name": "Jane Q. Investor",
                "source_text": "authorized for sale 100,000 shares...",
            },
        },
    )
    session.add(doc)
    session.commit()
    return doc


def test_proposal_from_processed_document(client):
    """The full link: document -> proposal -> approval -> live cap table."""
    doc = _investview_extracted_document(_client_session())

    # --- 1. Propose: inert preparation, real numbers, zero ledger writes.
    r = client.post(f"/documents/{doc.id}/cap-table-proposal")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "proposed"
    assert body["payload"]["issuer_name"] == "INVESTVIEW, INC."
    assert body["payload"]["share_count"] == 100000
    assert body["payload"]["price_per_share"] == 50.0
    assert body["payload"]["amount"] == 5000000.0
    assert body["payload"]["currency"] == "USD"
    assert body["payload"]["document_date"] == "2015-05-29"
    assert body["payload"]["subscriber_unresolved"] is False
    assert body["payload"]["provisional_investor_created"] is True

    # The preamble-named subscriber became a provisional investor row.
    sess = _client_session()
    subscriber = (
        sess.query(Investor).filter(Investor.name == "Jane Q. Investor").one()
    )
    assert body["payload"]["holder_id"] == subscriber.id

    # --- 2. The cap table is still empty before approval (proposal is inert).
    r = client.get("/cap-table/INVESTVIEW%2C%20INC.")
    assert r.status_code == 200
    assert r.json()["total_fully_diluted_shares"] == 0

    # --- 3. One named human approval materializes the issuance.
    r = client.post(
        f"/cap-table-proposals/{body['id']}/approve?reviewer=Dana%20Reviewer"
    )
    assert r.status_code in (200, 201), r.text
    event = r.json()
    assert event["event_type"] == "issuance"
    assert event["quantity"] == 100000
    assert event["holder_id"] == subscriber.id

    # --- 4. The cap table now reflects the document -- no re-typing.
    r = client.get("/cap-table/INVESTVIEW%2C%20INC.")
    assert r.status_code == 200
    snapshot = r.json()
    assert snapshot["total_fully_diluted_shares"] == 100000
    assert snapshot["ownership_by_holder"][subscriber.id] == 100.0
    pos = snapshot["positions"][0]
    assert pos["shares"] == 100000
    assert pos["security_name"] == "Series A Preferred Stock"


def test_reviewed_document_cannot_seed_proposal(client):
    """A document that failed a gate (REVIEW_NEEDED) can never reach the
    cap table -- the confidence gates are the entry ticket, no exceptions."""
    sess = _client_session()
    instrument = _container_instrument(sess, "Demo Issuer")
    doc = Document(
        id=str(uuid4()),
        instrument_id=instrument.id,
        filename="unclassified.pdf",
        file_url="uploads/unclassified.pdf",
        compliance_mode=ComplianceMode.TRADITIONAL,
        document_type=DocumentType.UNCLASSIFIED,
        status=DocumentStatus.REVIEW_NEEDED,
        classification_confidence=0.67,
        extraction_confidence=0.0,
        extracted_data=None,
    )
    sess.add(doc)
    sess.commit()

    r = client.post(f"/documents/{doc.id}/cap-table-proposal")
    assert r.status_code == 409
    assert "processed" in r.json()["detail"]


def test_unresolved_subscriber_blocks_approval(client):
    """A signature-block-only document produces a proposal flagged
    subscriber_unresolved -- and approval refuses until a human resolves
    the holder. No guessed investor ever enters the ledger."""
    sess = _client_session()
    instrument = _container_instrument(sess, "Demo Issuer")
    doc = Document(
        id=str(uuid4()),
        instrument_id=instrument.id,
        filename="no-subscriber.pdf",
        file_url="uploads/no-subscriber.pdf",
        compliance_mode=ComplianceMode.TRADITIONAL,
        document_type=DocumentType.EQUITY_SUBSCRIPTION,
        status=DocumentStatus.PROCESSED,
        classification_confidence=0.8,
        extraction_confidence=0.9,
        extracted_data={
            "schema_name": "EquitySubscriptionExtraction",
            "schema_version": "v1",
            "data": {
                "schema_name": "EquitySubscriptionExtraction",
                "schema_version": "v1",
                "extracted_at": "2015-05-29T00:00:00",
                "company_name": "ACME CORP.",
                "security_type": "Common Stock",
                "currency": "USD",
                "share_count": 5000,
                "subscriber_name": None,
            },
        },
    )
    sess.add(doc)
    sess.commit()

    r = client.post(f"/documents/{doc.id}/cap-table-proposal")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["payload"]["subscriber_unresolved"] is True
    assert body["payload"]["holder_id"] is None

    r = client.post(
        f"/cap-table-proposals/{body['id']}/approve?reviewer=Dana"
    )
    assert r.status_code == 409
    assert "subscriber" in r.json()["detail"]

    # Nothing leaked into the ledger.
    r = client.get("/cap-table/ACME%20CORP.")
    assert r.status_code == 200
    assert r.json()["total_fully_diluted_shares"] == 0


def test_proposal_approved_at_most_once(client):
    """Double-approval is a 409: one proposal, one issuance, ever."""
    doc = _investview_extracted_document(_client_session())
    r = client.post(f"/documents/{doc.id}/cap-table-proposal")
    pid = r.json()["id"]
    r1 = client.post(
        f"/cap-table-proposals/{pid}/approve?reviewer=Dana"
    )
    assert r1.status_code in (200, 201), r1.text
    r2 = client.post(
        f"/cap-table-proposals/{pid}/approve?reviewer=Dana"
    )
    assert r2.status_code == 409
