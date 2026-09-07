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
Underlying Asset: Solar plant and transmission assets
"""

BAD_LOAN_TEXT = """This is a restaurant menu.
No borrower, no lender, no principal, no rates anywhere in here."""

# Mimics the real Elzaad subscription agreement failure: a form-style
# questionnaire ("... Name: Yes") plus per-unit pricing and page markers.
SUB_JUNK_TEXT = """SUBSCRIPTION AGREEMENT
Elzaad Sukuk Fund — Subscription Document V8 22.12.2022
Fund Name: Yes
Investor Name: No
The units are offered at TRY 5 per unit subject to clause 4.2.
Page 5 of 34.
"""

SUB_GOOD_TEXT = """SUBSCRIPTION AGREEMENT
Fund Name: Elzaad Sukuk Fund
Investor Name: Gulf Holdings LLC
Subscription Amount: TRY 25,000,000
Payment Date: 2023-06-30
Payment instructions: wire to account number 00112233
"""


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


def test_subscription_junk_answers_and_bare_numbers_never_become_deal_data():
    """Regression (Elzaad subscription agreement): the dashboard showed
    Issuer 'Yes' and Amount '5 TRY'. Form answers must not become party
    names, and bare numbers (per-unit price, page marker, document date)
    must never become a commitment amount. Honest failure -> review."""
    outcome = run_extraction(SUB_JUNK_TEXT, DocumentType.SUBSCRIPTION_AGREEMENT.value)
    data = (outcome.extracted_data or {}).get("data", {})
    assert data.get("fund_name") != "Yes"
    assert data.get("investor_name") != "No"
    if outcome.extraction is not None:
        assert outcome.extraction.commitment_amount != 5.0


def test_subscription_extracts_amount_and_currency_from_anchor():
    outcome = run_extraction(SUB_GOOD_TEXT, DocumentType.SUBSCRIPTION_AGREEMENT.value)
    assert outcome.extraction is not None
    data = outcome.extracted_data["data"]
    assert data["fund_name"] == "Elzaad Sukuk Fund"
    assert data["commitment_amount"] == 25_000_000.0
    assert data["currency"] == "TRY"


def test_subscription_date_is_never_a_commitment_amount():
    """'Subscription Document V8 22.12.2022' — the document date must not be
    parsed as money even though it follows a keyword and has 4+ digits."""
    outcome = run_extraction(
        "SUBSCRIPTION AGREEMENT\nSubscription Document V8 dated 22.12.2022\n"
        "Investor: Acme Holdings Ltd\nNo commitment figures stated here.",
        DocumentType.SUBSCRIPTION_AGREEMENT.value,
    )
    data = (outcome.extracted_data or {}).get("data", {})
    if outcome.extraction is not None:
        assert outcome.extraction.commitment_amount != 2212.0
        assert outcome.extraction.commitment_amount != 22.12
    assert data.get("commitment_amount") != 2212.0
    assert data.get("commitment_amount") != 22.12


def test_sukuk_extraction_parses_al_ijara_without_silent_default():
    # "Structure: al-Ijara" must parse to IJARA, not fall back to MURABAHA.
    outcome = run_extraction(SUKUK_TEXT, DocumentType.SUKUK_CERTIFICATE.value)
    assert outcome.extraction is not None
    data = outcome.extracted_data["data"]
    assert data["contract_type"] == ShariahContractType.IJARA
    assert data["asset_description"], "underlying asset must be captured"
    # A contract type that genuinely cannot be parsed must stay None (so the
    # compliance gateway can flag it) — never silently masked as MURABAHA.
    unparseable = run_extraction(
        "SUKUK CERTIFICATE\nIssuer: Gulf Finance Holdings Ltd\n"
        "Total Issue Size: USD 100,000,000\n"
        "Structure: lease-to-own hybrid instrument\n"
        "Profit Rate: 3% \nShariah: fatwa ref F-1\n",
        DocumentType.SUKUK_CERTIFICATE.value,
    )
    assert unparseable.extraction is not None
    assert unparseable.extraction.contract_type is None
    assert unparseable.extracted_data["data"]["contract_type"] is None


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
        # NOTE: shariah_contract_type / underlying_asset_* are intentionally
        # NOT pre-filled here. They must be derived from the extraction by the
        # pipeline; this test proves that wiring, not a manually-built fixture.
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
    # auto-approved — the same pipeline code, different configuration. This
    # now exercises the wiring of contract_type / asset_description from the
    # extraction onto the instrument.
    assert result.outcome == ShariahReviewStatus.PENDING_SCHOLAR_REVIEW
    assert instr.shariah_review_status == ShariahReviewStatus.PENDING_SCHOLAR_REVIEW
    assert instr.shariah_review_status.value != "scholar_approved"
    assert instr.shariah_contract_type == ShariahContractType.IJARA
    assert instr.underlying_asset_description
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
    # Genuinely ambiguous: no strong keywords for any document type
    result = process_document(session, instr, ComplianceGateway(),
                              "This document relates to a financial agreement "
                              "between parties regarding obligations and terms.")
    assert result.document.document_type == DocumentType.UNCLASSIFIED
    assert result.routed is True
    session.close()


def test_subscription_heading_anchor_and_dollar_amount():
    """Fund vehicles are named in headings like 'Elzaad Sukuk Fund (The
    Fund)' with no 'Fund Name:' label, and amounts may be $ figures.
    Regression: the real Elzaad subscription agreement caught neither."""
    from app.extraction import extract_subscription
    text = (
        "Elzaad Sukuk Fund (The Fund)\n"
        "Subscription Agreement\n"
        "Section 13 - Subscription Amount\n"
        "The Subscriber hereby subscribes for interests of US$ 5,000,000 in the Fund."
    )
    extraction, confidence = extract_subscription(text)
    assert extraction is not None
    assert extraction.fund_name == "Elzaad Sukuk Fund"
    assert extraction.commitment_amount == 5_000_000.0
    assert extraction.currency == "USD"
    assert confidence >= 0.6


def test_subscription_form_instructions_only_is_not_extracted():
    """An unfilled instruction checklist ('...amount in United States Dollars
    in the section titled Subscription Amount', page markers, Yes/No rows)
    contains no actual commitment — must return None, never '5 TRY'."""
    from app.extraction import extract_subscription
    text = (
        "Elzaad Sukuk Fund (The Fund)\n"
        "Subscription Agreement\n"
        "4. Fill in the amount of desired subscription\n"
        "amount in United States Dollars in the section 13 Yes\n"
        "titled 'Subscription Amount'\n"
        "Page 5 of 34\n"
        "All Yes\n"
    )
    extraction, confidence = extract_subscription(text)
    assert extraction is None
    assert confidence < 0.6


def test_spoken_currency_used_when_no_code_token():
    """'United States Dollars' spelled out in prose must resolve to USD
    instead of the hardcoded default silently masking a mismatch."""
    from app.extraction import extract_subscription
    text = (
        "Fund Name: Al Miraj Fund\n"
        "The Subscriber agrees to pay the sum of 7,500,000\n"
        "in United States Dollars on the closing date."
    )
    extraction, _ = extract_subscription(text)
    assert extraction is not None
    assert extraction.commitment_amount == 7_500_000.0
    assert extraction.currency == "USD"


def test_subscription_trailing_comma_list_punctuation_is_not_amount():
    """Regression (Elzaad, round 2): the dashboard showed '13 TRY'. The
    amount regex matched '13,' from 'pages 12, 13, and 15' list punctuation
    and the old guard accepted it because it contained a comma. A trailing
    separator is punctuation, not a thousands grouping — and the real
    'USD1,000,000' further in the document must win instead."""
    from app.extraction import _plausible_amount, extract_subscription
    assert not _plausible_amount("13,")
    assert not _plausible_amount("13.")
    assert _plausible_amount("1,000,000")
    assert not _plausible_amount("12,34")  # invalid grouping

    text = (
        "Elzaad Sukuk Fund (The Fund)\n"
        "4. Fill in the desired subscription amount\n"
        "in United States Dollars in the section 13 Yes\n"
        "All pages between 18 and 27 (inclusive)\n"
        "Class A Units have a minimum Subscription Amount of USD1,000,000.\n"
    )
    extraction, _ = extract_subscription(text)
    assert extraction is not None
    assert extraction.commitment_amount == 1_000_000.0
    assert extraction.currency == "USD"


def test_subscription_iban_and_phone_digits_never_become_amounts():
    """Bank-page digits (IBAN '00001745215100', account '745215', building
    '2505') are not money, even with 4+ digits."""
    from app.extraction import extract_subscription
    text = (
        "Fund Name: SICO IX Elzaad Sukuk Sub Acc\n"
        "IBAN: BH54BBME00001745215100 Currency: USD\n"
        "Account Number: 001-745215-100\n"
        "Building 2505, Road 2832, Tel: +973 17515700\n"
        "Please wire your subscription to the account above."
    )
    extraction, _ = extract_subscription(text)
    if extraction is not None:
        assert extraction.commitment_amount not in (
            1745215100.0, 745215.0, 2505.0, 2832.0, 17515700.0)