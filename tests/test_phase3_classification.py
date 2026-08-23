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