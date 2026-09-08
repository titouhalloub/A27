"""Regression: US LLC equity subscription agreements (e.g.
``emktbrew_subagmtca2.pdf``) must classify as EQUITY_SUBSCRIPTION, route to the
``extract_equity_subscription`` extractor, and populate the deal container.

Root causes fixed together here:
- ``EXTRACTION_ROUTE_NAMES`` was undefined -> NameError on dispatch.
- ``extract_result_to_document_data`` was referenced by ``run_extraction`` but
  never defined anywhere (latent bug in every commit) -> NameError at runtime.
- The LLM classifier's system prompt did not list ``equity_subscription``, so
  the model mapped LLC equity offers to ``subscription_agreement`` (fund-style).
"""

import inspect
import json

from app.classification import (
    _classify_with_openai_compatible,
    _parse_llm_document_type,
    classify_with_heuristics,
)
from app.compliance import ComplianceGateway
from app.extraction import run_extraction, extract_equity_subscription
from app.models.enums import (
    ComplianceMode,
    DocumentStatus,
    DocumentType,
    IngestionSource,
    ShariahReviewStatus,
    TransactionType,
)
from app.models.orm import Document
from app.pipeline import process_document
from app.schemas import (
    EquitySubscriptionExtraction,
    extract_result_to_document_data,
)
from tests.conftest import make_instrument

# Mirrors the real OCR'd brew subscription agreement ($1.00/unit, 1,235,000
# non-voting common units, $500 minimum, Michigan LLC, Regulation Crowdfunding).
EQUITY_SUB_TEXT = """SUBSCRIPTION AGREEMENT
5 MILE BREW COMPANY LLC
5 Mile Brewing Company LLC, a Michigan limited liability company (the "Company")
The Company is offering an aggregate of 1,235,000 Non-Voting Common Units (the "Units")
to certain investors pursuant to a "Regulation Crowdfunding" offering
The Offering consists of the sale of Units at a price per Unit of $1.00
the Company will not accept subscriptions for less than $500 (500 Units)
Target Offering Amount identified in the Offering Statement (Form C)
https://www.mainvest.com
"""


def test_equity_subscription_heuristic_classifies_with_filename_tiebreak():
    result = classify_with_heuristics(
        EQUITY_SUB_TEXT, filename="emktbrew_subagmtca2.pdf"
    )
    assert result.document_type == DocumentType.EQUITY_SUBSCRIPTION
    assert result.confidence >= 0.75


def test_llm_parse_accepts_equity_subscription():
    assert (
        _parse_llm_document_type("equity_subscription")
        == DocumentType.EQUITY_SUBSCRIPTION
    )


def test_llm_prompt_vocabulary_includes_equity_subscription():
    """Root-cause guard: the LLM classifier used to map LLC equity offers to
    ``subscription_agreement`` because ``equity_subscription`` was not in the
    allowed output vocabulary."""
    source = inspect.getsource(_classify_with_openai_compatible)
    assert "equity_subscription" in source
def test_equity_subscription_extraction_populates_all_fields():
    outcome = run_extraction(EQUITY_SUB_TEXT, DocumentType.EQUITY_SUBSCRIPTION.value)
    assert outcome.schema_name == "EquitySubscriptionExtraction"
    assert outcome.extraction is not None
    assert isinstance(outcome.extraction, EquitySubscriptionExtraction)
    assert outcome.confidence == 1.0
    assert not outcome.routed_to_review

    data = outcome.extracted_data["data"]
    assert data["schema_name"] == "EquitySubscriptionExtraction"
    assert data["schema_version"] == "v1"
    assert data["company_name"] == "5 MILE BREW COMPANY LLC"
    assert data["state_of_incorporation"] == "Michigan"
    assert data["security_type"] == "Non-Voting Common Units"
    assert data["price_per_unit"] == 1.0
    assert data["total_offering_amount"] == 1_235_000.0
    assert data["minimum_investment"] == 500.0
    assert data["currency"] == "USD"

    # The envelope must stay JSON-serialisable for the SQLAlchemy JSON column.
    json.dumps(outcome.extracted_data)


def test_equity_subscription_envelope_uses_schema_name_and_version():
    extraction, _ = extract_equity_subscription(EQUITY_SUB_TEXT)
    assert extraction is not None
    envelope = extract_result_to_document_data(extraction, "EquitySubscriptionExtraction")
    assert envelope["schema_name"] == "EquitySubscriptionExtraction"
    assert envelope["schema_version"] == "v1"
    assert set(envelope["data"]) >= {
        "company_name", "state_of_incorporation", "security_type",
        "price_per_unit", "total_offering_amount", "minimum_investment", "currency",
    }
    json.dumps(envelope)


def test_equity_subscription_pipeline_backfills_deal_container(db, monkeypatch):
    from app.config import settings

    # Force the deterministic heuristic classifier: the pipeline must not
    # depend on external LLM availability/quality in a unit test.
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.LOAN,  # placeholder from creation time
        mode=ComplianceMode.TRADITIONAL,
    )
    session.add(instr)
    session.flush()
    # KYC present so the traditional rule set does not block the deal.
    session.add(Document(
        id="kyc-doc", instrument_id=instr.id, filename="kyc.pdf",
        file_url="mem://kyc", document_type=DocumentType.KYC,
        classification_confidence=0.99, extraction_confidence=0.0,
        ingestion_source=IngestionSource.MANUAL_ENTRY,
        compliance_mode=instr.compliance_mode, status="processed",
    ))
    session.commit()

    result = process_document(
        session, instr, ComplianceGateway(), EQUITY_SUB_TEXT,
        filename="emktbrew_subagmtca2.pdf",
    )
    session.refresh(instr)
    assert result.document.document_type == DocumentType.EQUITY_SUBSCRIPTION
    assert not result.routed
    assert result.document.status == DocumentStatus.PROCESSED
    assert result.outcome != ShariahReviewStatus.PENDING_SCHOLAR_REVIEW

    # Creation-time placeholders must be superseded by the document's facts.
    assert instr.transaction_type == TransactionType.EQUITY
    assert instr.issuer_name == "5 MILE BREW COMPANY LLC"
    assert instr.amount == 1_235_000.0
    assert instr.currency == "USD"
    assert result.document.extracted_data["schema_name"] == "EquitySubscriptionExtraction"
    session.close()