"""MVP-grade extraction — Loan (Phase 4) + Sukuk support (Phase 5)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel

from app.telemetry import get_tracer

from app.schemas import (
    SCHEMA_VERSION,
    EXTRACTION_ROUTE_NAMES,
    LoanExtraction,
    SukukExtraction,
    extract_result_to_document_data,
)

_CURRENCY = r"(?:USD|EUR|GBP|MYR|AED|SAR|SGD|IDR|TRY)"
_AMOUNT = r"(\d[\d,]*\.?\d*)"


def _grep(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    if m.re.groups:  # capture group(s) present -> prefer first group
        return m.group(1).strip()
    return m.group(0).strip()


def _amount(pattern: str, text: str) -> float | None:
    v = _grep(pattern, text)
    return float(v.replace(",", "")) if v else None


def _hit(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def extract_loan(text: str) -> tuple[LoanExtraction | None, float]:
    issuer = _grep(r"(?:borrower|obligor)[\s:]+([^,\n]{3,80})", text)
    lender = _grep(r"(?:lender|bank)[\s:]+([^,\n]{3,80})", text)
    # "Principal: USD 2,500,000" — skip the colon/space/currency token, then
    # capture the amount. _CURRENCY is a non-capturing group, so the only
    # capture group is the numeric amount in _AMOUNT.
    principal = _amount(r"(?:principal|facility|loan)[^0-9\n]{0,40}?[\s$]*" + _CURRENCY + r"?[\s$]*" + _AMOUNT, text)
    currency = (_grep(_CURRENCY, text) or "USD").upper()[:3]
    rate_raw = _grep(r"(?:interest rate)[\s:]*([\d.]+)\s*%?", text)
    rate = float(rate_raw) / 100.0 if rate_raw else None
    maturity = _grep(r"(?:maturity)[\s:]*(?:date)?[\s:]*([\d/\-]{4,20})", text)
    repayment = _grep(r"(?:repayment|amortization)[\s:]+([^\n]{3,200})", text)

    present = [bool(x) for x in (issuer, lender, principal, rate, currency, maturity)]
    confidence = round(sum(present) / len(present), 3)

    if not principal or not issuer or rate is None:
        return None, confidence

    try:
        extraction = LoanExtraction(
            issuer_name=issuer,
            lender_name=lender or "",
            principal_amount=principal,
            currency=currency,
            interest_rate=rate,
            repayment_schedule=repayment,
            secured=_hit(r"secured|collateral", text),
            governing_law=_grep(r"(?:governing law)[\s:]+([^\n]{3,60})", text),
        )
    except Exception:
        return None, confidence
    return extraction, confidence


def extract_sukuk(text: str) -> tuple[SukukExtraction | None, float]:
    from app.models.enums import ShariahContractType

    issuer = _grep(r"(?:issuer|originator)[\s:]+([^,\n]{3,80})", text)
    total = _amount(r"(?:total|issue|size)[^0-9\n]{0,40}?[\s$]*" + _CURRENCY + r"?[\s$]*" + _AMOUNT, text)
    cur = (_grep(_CURRENCY, text) or "USD").upper()[:3]
    profit_raw = _grep(r"(?:profit rate)[\s:]*([\d.]+)\s*%?", text)
    fatwa = _grep(r"(?:fatwa[^:\n]*|shariah[^:\n]*)[\s:]+([^\n]{3,150})", text)
    ctype = _grep(r"(?:contract|structure)[\s:]+(murabaha|ijara|musharakah|wakalah)", text)

    present = [issuer is not None, total is not None, profit_raw is not None, fatwa is not None]
    confidence = round(sum(bool(p) for p in present) / len(present), 3)

    if not issuer or total is None:
        return None, confidence

    try:
        extraction = SukukExtraction(
            issuer_name=issuer,
            total_size=total,
            currency=cur,
            contract_type=ShariahContractType(ctype) if ctype else ShariahContractType.MURABAHA,
            profit_rate=round(float(profit_raw) / 100.0, 4) if profit_raw else None,
            fatwa_reference=fatwa,
        )
    except Exception:
        return None, confidence
    return extraction, confidence


EXTRACTORS: dict[str, Callable[[str], tuple[BaseModel | None, float]]] = {
    "loan_agreement": extract_loan,
    "term_sheet": extract_loan,
    "sukuk_certificate": extract_sukuk,
}


@dataclass
class ExtractionOutcome:
    schema_name: str | None
    schema_version: str
    extraction: BaseModel | None
    confidence: float
    routed_to_review: bool
    extracted_data: dict = field(default_factory=dict)
    error: str | None = None


def run_extraction(text: str, document_type: str) -> ExtractionOutcome:
    from app.config import settings
    from app.models.enums import DocumentType

    doc_type = DocumentType(document_type)
    extractor = EXTRACTORS.get(doc_type.value)
    schema_name = EXTRACTION_ROUTE_NAMES.get(doc_type)
    if extractor is None or schema_name is None:
        return ExtractionOutcome(
            schema_name=None, schema_version=SCHEMA_VERSION, extraction=None,
            confidence=0.0, routed_to_review=True,
            error=f"No extractor for {doc_type}",
        )

    trace = get_tracer().span("extraction", doc_type=doc_type.value,
                              schema=schema_name, input_chars=len(text))

    extraction, confidence = extractor(text)
    if extraction is None:
        trace.finish({"routed_to_review": True, "reason": "validation_failed"},
                     confidence=confidence, cost=0.0, model="extractor-heuristic-v1")
        trace.emit()
        return ExtractionOutcome(
            schema_name=schema_name, schema_version=SCHEMA_VERSION,
            extraction=None, confidence=confidence, routed_to_review=True,
            error="validation failed")

    data = extract_result_to_document_data(extraction, schema_name)
    routed = confidence < settings.extraction_min_confidence

    trace.finish({"routed_to_review": routed, "confidence": confidence,
                  "schema_name": schema_name, "schema_version": SCHEMA_VERSION},
                 confidence=confidence, cost=0.0, model="extractor-heuristic-v1")
    trace.emit()

    return ExtractionOutcome(
        schema_name=schema_name, schema_version=SCHEMA_VERSION,
        extraction=extraction, confidence=confidence, routed_to_review=routed,
        extracted_data=data)
