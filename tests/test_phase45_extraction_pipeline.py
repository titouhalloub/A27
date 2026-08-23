"""Phase 4 — extraction gating; Phase 5 — full pipeline, two tracks."""

from app.compliance import ComplianceGateway
from app.extraction import run_extraction
from app.models.enums import (
    ComplianceMode,
    DocumentStatus,
    DocumentType,
    IngestionSource,
    ShariahContractType,
    ShariahReviewStatus,
    TransactionType,
)
from app.models.orm import Document, LedgerEntry
from app.pipeline import process_document
from app.schemas import extract_result_to_document_data
from app.telemetry import clear_traces
from tests.conftest import make_instrument

LOAN_TEXT = """LOAN AGREEMENT
Borrower: Alpha Manufacturing Sdn Bhd
Lender: Meridian Bank Ltd
Principal: USD 2,500,000
Interest Rate: 6.5%
Repayment: quarterly amortization over 5 years
Maturity Date: 2029-12-31
Governing Law: English law
"""

SUKUK_TEXT = """SUKUK CERTIFICATE
Issuer: Petra Energy Sukuk SPV
Total Issue Size: USD 500,000,000
Structure: al-Ijara
Profit Rate: 4.25% per annum
Shariah Committee: Approved per fatwa reference FA-2024-011
"""

BAD_LOAN_TEXT = """This is a restaurant menu.
No borrower, no lender, no principal, no rates anywhere in here."""


def test_loan_extraction_full_confidence(db):
    outcome = run_extraction(LOAN_TEXT, DocumentType.LOAN_AGREEMENT.value)
    assert outcome.extraction is not None
    assert not outcome.routed_to_review
    assert outcome.confidence >= 0.85
    assert outcome.extracted_data["schema_name"] == "LoanExtraction"
    assert outcome.extracted_data["schema_version"] == "v1"
    assert outcome.extracted_data["data"]["principal_amount"] == 2500000.0
    assert outcome.extracted_data["data"]["interest_rate"] == 0.065


def test_phase4_malformed_routes_to_review():
    outcome = run_extraction(BAD_LOAN_TEXT, DocumentType.LOAN_AGREEMENT.value)
    assert outcome.routed_to_review is True
    assert outcome.extraction is None


def test_phase5_traditional_loan_pipeline(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.LOAN,
        mode=ComplianceMode.TRADITIONAL,
        type_specific_data={"interest_rate": 0.065},
    )
    session.add(instr)
    session.flush()
    kycdoc = Document(
        id="kyc-doc", instrument_id=instr.id, filename="kyc.pdf",
        file_url="mem://kyc", document_type=DocumentType.KYC,
        classification_confidence=0.99, extraction_confidence=0.0,
        ingestion_source=IngestionSource.MANUAL_ENTRY,
        compliance_mode=instr.compliance_mode, status="processed",
    )
    session.add(kycdoc)
    session.commit()

    result = process_document(session, instr, ComplianceGateway(), LOAN_TEXT,
                              filename="loan.txt")
    session.refresh(instr)
    assert result.outcome == ShariahReviewStatus.NOT_APPLICABLE
    assert instr.shariah_review_status == ShariahReviewStatus.NOT_APPLICABLE
    assert result.document.document_type == DocumentType.LOAN_AGREEMENT
    assert result.document.ingestion_source == IngestionSource.NATIVE_EXTRACTION
    assert not result.routed
    session.close()


def test_phase5_islamic_sukuk_pipeline_pending_review(db):
    session = db[0]()
    instr = make_instrument(
        txn_type=TransactionType.SUKUK,
        mode=ComplianceMode.ISLAMIC,
        shariah_contract_type=ShariahContractType.IJARA,
        underlying_asset_id="asset-500M",
        type_specific_data={"profit_rate": 0.0425},
    )
    session.add(instr)
    session.flush()
    fatwa = Document(
        id="fatwa-doc", instrument_id=instr.id, filename="fatwa.pdf",
        file_url="mem://fatwa", document_type=DocumentType.FATWA,
        classification_confidence=0.99, extraction_confidence=0.0,
        ingestion_source=IngestionSource.MANUAL_ENTRY,
        compliance_mode=instr.compliance_mode, status="uploaded",
    )
    session.add(fatwa)
    session.commit()

    result = process_document(session, instr, ComplianceGateway(), SUKUK_TEXT,
                              filename="sukuk.txt")
    session.refresh(instr)

    # Islamic track: a clean sukuk lands in PENDING_SCHOLAR_REVIEW, never
    # auto-approved — the same pipeline code, different configuration.
    assert result.outcome == ShariahReviewStatus.PENDING_SCHOLAR_REVIEW
    assert instr.shariah_review_status == ShariahReviewStatus.PENDING_SCHOLAR_REVIEW
    assert instr.shariah_review_status.value != "scholar_approved"
    assert result.document.document_type == DocumentType.SUKUK_CERTIFICATE
    assert result.document.ingestion_source == IngestionSource.NATIVE_EXTRACTION
    assert not result.routed

    ledger = session.query(LedgerEntry).filter(
        LedgerEntry.instrument_id == instr.id).all()
    assert any(e.payload.get("event") == "compliance_gateway" for e in ledger)
    session.close()


def test_unclassified_document_routes_to_triage(db):
    session = db[0]()
    instr = make_instrument()
    session.add(instr)
    session.commit()
    result = process_document(session, instr, ComplianceGateway(),
                              "ALMOST A LOAN: principal but no repayment...")
    assert result.document.document_type == DocumentType.UNCLASSIFIED
    assert result.routed is True
    session.close()