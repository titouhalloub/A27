"""Phase 3 — classification: clear loan, clear sukuk, ambiguous -> UNCLASSIFIED."""

from app.classification import classify_document
from app.models.enums import DocumentType
from app.telemetry import clear_traces, find_trace

LOAN_TEXT = """
LOAN AGREEMENT
Borrower: Alpha Manufacturing Sdn Bhd
Lender: Meridian Bank Ltd
Principal: USD 2,500,000
Interest Rate: 6.5%
Repayment: quarterly amortization over 5 years
Maturity Date: 2029-12-31
Governing Law: English law
Negative covenants: no further encumbrance without prior consent
"""

SUKUK_TEXT = """\
SUKUK CERTIFICATE
Certificate Holders Trust
Issuer: Petra Energy Sukuk SPV
Total Issue Size: USD 500,000,000
Structure: al-Ijara
Profit Rate: 4.25% per annum
Rental of underlying assets: quarterly
Shariah Committee: Approved per fatwa reference FA-2024-011
Periodic Distribution: semi-annual
"""

AMBIGUOUS_TEXT = """\
AGREEMENT BETWEEN PARTIES
Principal: the parties agree the principal amount shall be repaid monthly.
Lender: the lender may demand early amortization at its discretion.
This document references the profit rate for a rental structure and
the murabaha asset for which the borrower has provided a fatwa.
Repayment schedule is attached; the sukuk holders consent to the loan.
"""


def test_loan_classified_clearly():
    result = classify_document(LOAN_TEXT)
    assert result.document_type == DocumentType.LOAN_AGREEMENT
    assert result.confidence >= 0.75


def test_sukuk_classified_clearly():
    result = classify_document(SUKUK_TEXT)
    assert result.document_type == DocumentType.SUKUK_CERTIFICATE
    assert result.confidence >= 0.75


def test_ambiguous_document_routed_to_unclassified_not_guess():
    result = classify_document(AMBIGUOUS_TEXT)
    assert result.document_type == DocumentType.UNCLASSIFIED
    assert result.confidence < 0.75


def test_classification_emits_trace_with_confidence():
    clear_traces()
    result = classify_document(LOAN_TEXT)
    trace = find_trace("classification")
    assert trace is not None
    assert trace.metadata["confidence"] == result.confidence
    assert trace.output["document_type"] == result.document_type.value


def test_llm_outage_degrades_to_heuristics_not_500(monkeypatch):
    """A rate-limited / failing LLM must never propagate an exception up to
    the upload endpoint. classification falls back to the deterministic
    keyword path and the confidence gate still applies."""
    import app.classification as mod

    def boom(text):
        raise RuntimeError("Rate limit exceeded: free-models-per-day")

    monkeypatch.setattr(mod, "classify_with_llm", boom)
    # Force the LLM branch even if no key is configured in the test env.
    monkeypatch.setattr(mod.settings, "openrouter_api_key", "sk-or-v1-test")

    # Must NOT raise.
    result = classify_document(LOAN_TEXT, filename="loan-agreement.pdf")
    # The heuristic path is deterministic and knows LOAN_TEXT is a loan.
    assert result.document_type == DocumentType.LOAN_AGREEMENT
    assert result.backend == "heuristic"
def test_llm_type_string_is_canonicalised():
    """LLMs return the same class in free-form ("Loan Agreement", "sha",
    "PPM"). The parser must map them to the canonical enum value,and
    never crash on an unknown value -- unknown -> UNCLASSIFIED."""
    from app.classification import _parse_llm_document_type

    assert _parse_llm_document_type("Loan Agreement") == DocumentType.LOAN_AGREEMENT
    assert _parse_llm_document_type("loan_agreement") == DocumentType.LOAN_AGREEMENT
    assert _parse_llm_document_type("loan-agreement") == DocumentType.LOAN_AGREEMENT
    assert _parse_llm_document_type("sha") == DocumentType.SHA
    assert _parse_llm_document_type("PPM") == DocumentType.PPM
    assert _parse_llm_document_type("lpa") == DocumentType.LPA
    assert _parse_llm_document_type("sukuk-certificate") == DocumentType.SUKUK_CERTIFICATE
    assert _parse_llm_document_type("??") == DocumentType.UNCLASSIFIED
    assert _parse_llm_document_type("") == DocumentType.UNCLASSIFIED


def test_fund_factsheet_is_never_classified_as_a_contract():
    """Regression (Principal Islamic Malaysia fund factsheet): contract
    vocabulary ('sukuk', 'fund', 'units') ranked it loan_agreement at
    exactly 0.75, sneaking past the gate. Factsheet markers ('top
    holdings', 'fund performance', 'base currency', ...) are decisive:
    the document is UNCLASSIFIED and routes to human review."""
    factsheet = """
    Principal Islamic Malaysia Government Sukuk Fund - Class A MYR
    Fund Objective Fund Performance Fund Information
    ISIN Code MYU1000HR006 Currency MYR Base Currency MYR
    Fund Inception 21 Jun 2021 Domicile Malaysia
    Top Holdings Country % of Assets
    GII Murabahah Malaysia 16.46 Beta 1.10
    Total Returns 1.14 percent Management Fee 1.25% p.a.
    Unit Price NAV per unit as at 31 Dec 2025
    """
    result = classify_document(factsheet, filename="fund_factsheet.pdf")
    assert result.document_type == DocumentType.UNCLASSIFIED
    assert result.confidence < 0.75
