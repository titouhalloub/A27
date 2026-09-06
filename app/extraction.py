"""MVP-grade extraction — Loan (Phase 4) + Sukuk support (Phase 5)."""

from __future__ import annotations

from datetime import date
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
    CapitalCallExtraction,
    SubscriptionAgreementExtraction,
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
    ctype = _grep(
        r"(?:contract|structure)[\s:]+(?:al-)?(murabaha|ijara|musharakah|wakalah)",
        text,
    )
    # Capture the declared underlying asset so the pipeline can carry it onto
    # Instrument.underlying_asset_description — the actual asset-backing
    # evidence — instead of silently dropping it before compliance review.
    asset_description = _grep(
        r"(?:underlying asset|asset description|collateral)[\s:]+([^\n]{3,200})",
        text,
    )

    present = [issuer is not None, total is not None, profit_raw is not None, fatwa is not None]
    confidence = round(sum(bool(p) for p in present) / len(present), 3)

    if not issuer or total is None:
        return None, confidence

    try:
        extraction = SukukExtraction(
            issuer_name=issuer,
            total_size=total,
            currency=cur,
            # No silent default: an unparseable contract type stays None and is
            # surfaced as a blocking finding by shariah_contract_type_declared,
            # rather than being masked as a confirmed Murabaha.
            # _grep preserves original case (e.g. "Ijara"); the enum values are
            # lowercase, so normalise before constructing.
            contract_type=ShariahContractType(ctype.lower()) if ctype else None,
            profit_rate=round(float(profit_raw) / 100.0, 4) if profit_raw else None,
            fatwa_reference=fatwa,
            asset_description=asset_description,
        )
    except Exception:
        return None, confidence
    return extraction, confidence


def extract_capital_call(text: str) -> tuple[CapitalCallExtraction | None, float]:
    """Extract capital call information from a capital call notice or subscription doc.

    Looks for: funder name, currency, capital being called, due date, and
    wire instructions. The source_text (the text window the amounts were
    parsed from) is captured for audit trail purposes.
    """
    funder = _grep(r"(?:call(?:ed|ing)? to|notice.*(?:capital|call|contribution).*from|lp|limited partner)[^\n,]*:\s*([^\n,]{2,80})", text)
    # Fallback: look for "Name:" near fund names
    if not funder:
        funder = _grep(r'(?:fund|lp|limited partner)(?:\s+name)?[:\s]+([^\n,]{2,80})', text)
    currency = (_grep(_CURRENCY, text) or "USD").upper()[:3]
    owing = _amount(r"(?:capital call|called|amount (?:(?:to )?be )?due|contribution|amount due|call amount|together with)[^0-9\n]{0,50}?[\s$]*" + _CURRENCY + r"?[\s$]*" + _AMOUNT, text)
    # Fallback patterns for the called amount
    if owing is None:
        owing = _amount(r"(?:usd|eur|gbp|myr|aed|sar|sgd|idr|try)?[\s$]*" + _AMOUNT + r"\s*(?:million|mio|k|thousand)?", text)
    due_raw = _grep(r"(?:due date|call date|payment date|expire)[:\s]+([\d/\-]{4,20})", text)
    due_date = None
    if due_raw:
        try:
            due_date = date.fromisoformat(due_raw.replace("/", "-"))
        except (ValueError, TypeError):
            pass
    wire = _grep(r"(?:wire|account)[^\n]{0,200}(?:account number|aba|routing|swift|iban)[^\n]{0,200}", text)

    present = [funder is not None, currency is not None, owing is not None, due_date is not None]
    confidence = round(sum(bool(p) for p in present) / len(present), 3)

    if not funder or owing is None:
        return None, confidence

    try:
        extraction = CapitalCallExtraction(
            funder_name=funder,
            currency=currency,
            capital_owing=owing,
            due_date=due_date,
            wire_details=wire,
            source_text=text[:2000],  # bounded audit trail of the parsed window
        )
    except Exception:
        return None, confidence
    return extraction, confidence


def extract_subscription(text: str) -> tuple[SubscriptionAgreementExtraction | None, float]:
    """Extract subscription agreement information.

    Looks for: fund name, investor name, commitment amount, currency,
    payment due date, and payment instructions.
    """
    fund_name = _grep(
        r"(?:fund|issuer|vehicle)\s*name[\s:]+([^\n,]{2,80})",
        text
    )
    if fund_name:
        fund_name = fund_name.lstrip(": \t").strip()
    if not fund_name:
        fund_name = _grep(r"(?:on behalf of|for the account of)[^\n]{0,50}?([^\n,]{2,80})", text)

    investor_name = _grep(
        r"(?:investor|subscriber|limited partner|lp)\s*name[\s:]+([^\n,]{2,80})",
        text
    )
    if investor_name:
        investor_name = investor_name.lstrip(": \t").strip()
    if not investor_name:
        investor_name = _grep(r"(?:the undersigned|investor name)[^\n]{0,30}?([^\n,]{2,80})", text)

    commitment = _amount(
        r"(?:capital|subscription|commitment|committed)[^\n]{0,50}?(?:amount|commitment)?[^\n]{0,30}?" + _CURRENCY + r"?[\s$]*" + _AMOUNT,
        text
    )
    if commitment is None:
        commitment = _amount(r"(?:usd|eur|gbp|myr|aed|sar|sgd|idr|try)?[\s$]*" + _AMOUNT + r"\s*(?:million|mio|k|thousand)?", text)

    currency = (_grep(_CURRENCY, text) or "USD").upper()[:3]

    payment_due_raw = _grep(r"(?:payment|due|closing|subscription)\s*date[\s:]+([\d/\-]{4,20})", text)
    payment_due_date = None
    if payment_due_raw:
        try:
            payment_due_date = date.fromisoformat(payment_due_raw.replace("/", "-"))
        except (ValueError, TypeError):
            pass

    payment_instructions = _grep(
        r"(?:wire|account|payment|bank|transfer)[^\n]{0,100}(?:number|details|instruction|info)[^\n]{0,100}",
        text
    )

    present = [
        fund_name is not None,
        investor_name is not None,
        commitment is not None,
        currency is not None,
        payment_due_date is not None,
    ]
    confidence = round(sum(bool(p) for p in present) / len(present), 3)

    if not fund_name or commitment is None:
        return None, confidence

    try:
        extraction = SubscriptionAgreementExtraction(
            fund_name=fund_name,
            investor_name=investor_name or "",
            commitment_amount=commitment,
            currency=currency,
            payment_due_date=payment_due_date,
            payment_instructions=payment_instructions,
            source_text=text[:2000],
        )
    except Exception:
        return None, confidence
    return extraction, confidence


EXTRACTORS: dict[str, Callable[[str], tuple[BaseModel | None, float]]] = {
    "loan_agreement": extract_loan,
    "term_sheet": extract_loan,
    "sukuk_certificate": extract_sukuk,
    "capital_call_notice": extract_capital_call,
    "subscription_agreement": extract_subscription,
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
