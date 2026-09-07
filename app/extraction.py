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
_AMOUNT = r"(\d+(?:,\d{3})*(?:\.\d+)?)"

# Spelled-out currencies in contract prose ("amount in United States Dollars").
_SPOKEN_CURRENCY = re.compile(
    r"(United\s+States\s+Dollars?|U\.?S\.?\s+Dollars?|US\s+Dollars?|US\$|"
    r"Turkish\s+Lira|Euros?|Pound\s+Sterling|Saudi\s+Riyals?|"
    r"UAE\s+Dirhams?|Malaysian\s+Ringgit|Singapore\s+Dollars?|"
    r"Indonesian\s+Rupiah)",
    re.IGNORECASE,
)
_SPOKEN_TO_CODE = {
    "united states dollars": "USD", "united states dollar": "USD",
    "u.s. dollars": "USD", "us dollars": "USD", "us$": "USD",
    "turkish lira": "TRY", "euro": "EUR", "euros": "EUR",
    "pound sterling": "GBP", "saudi riyal": "SAR", "saudi riyals": "SAR",
    "uae dirham": "AED", "uae dirhams": "AED",
    "malaysian ringgit": "MYR", "singapore dollars": "SGD",
    "singapore dollar": "SGD", "indonesian rupiah": "IDR",
}

# "$" or "US$" followed by an amount is an explicit USD denomination.
_DOLLAR_AMOUNT = re.compile(r"(US\$|[\$])[\s]*(" + _AMOUNT + r")", re.IGNORECASE)


def _spoken_currency(text: str) -> str | None:
    m = _SPOKEN_CURRENCY.search(text)
    if not m:
        return None
    return _SPOKEN_TO_CODE.get(m.group(1).lower().replace("  ", " "))

# Form/questionnaire answers and other filler that a lazy `[^\n,]{2,80}` name
# capture happily swallows ("Issuer Name: Yes"). These must never become party
# names on a deal container.
_JUNK_NAMES = {
    "yes", "no", "n/a", "na", "n.a.", "tbd", "tbc", "none", "nil",
    "true", "false", "y", "n", "x", "-", "--", "name", "unknown", "other",
    "witness signature", "signature", "witness", "applicant", "subscriber",
}


def _clean_name(value: str | None) -> str | None:
    """Reject junk name captures: boolean answers, placeholders, no letters."""
    if not value:
        return None
    v = value.strip(" \t:;-–—*.")
    if not v or v.lower() in _JUNK_NAMES:
        return None
    if sum(ch.isalpha() for ch in v) < 2:
        return None
    return v


def _plausible_amount(raw: str) -> bool:
    """A money amount has magnitude: 4+ integer digits with valid thousands
    grouping. Rejects page numbers, clause ids, phone/IBAN digits and list
    punctuation — e.g. '13,' from 'pages 12, 13, and 15' is a trailing
    comma, NOT a thousands separator (regression: read as 13.0 TRY)."""
    int_part = raw.split(".")[0]
    if int_part != int_part.rstrip(",."):
        return False  # trailing separator = list/decimal punctuation
    digits = int_part.replace(",", "")
    if "," in int_part:
        groups = int_part.split(",")
        return len(groups[0]) in (1, 2, 3) and all(len(g) == 3 for g in groups[1:])
    return len(digits) >= 4


def _is_grouped_amount(raw: str) -> bool:
    """True only for properly thousands-grouped integers ('5,000,000').
    Used where the number has NO currency attached: without grouping it
    could be an IBAN, phone or building number."""
    int_part = raw.split(".")[0]
    groups = int_part.split(",")
    return (len(groups) >= 2 and len(groups[0]) in (1, 2, 3)
            and all(len(g) == 3 for g in groups[1:]))


def _is_date_fragment(text: str, start: int, end: int) -> bool:
    """True when the number at text[start:end] is part of a date like
    22.12.2022 / 12/2022 / 2022-06-30 — never a money amount."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before in "./-" and start >= 2 and text[start - 2].isdigit():
        return True
    if after in "./-" and end + 1 < len(text) and text[end + 1].isdigit():
        return True
    return False


def _money_after(text: str, keyword_pattern: str, window: int = 80):
    """Keyword-anchored money lookup: `keyword ... CUR 1,234` / `1,234 CUR`.

    Returns (amount, currency) — currency is None when the matched number
    carried no explicit currency token. Every candidate must pass the
    magnitude plausibility guard, so "TRY 5 per unit" or "Page 5 of 34"
    can never be mistaken for a commitment amount.
    """
    cur_amt = re.compile(r"(" + _CURRENCY + r")[\s$]*(" + _AMOUNT + r")", re.IGNORECASE)
    amt_cur = re.compile(r"(" + _AMOUNT + r")\s*(" + _CURRENCY + r")", re.IGNORECASE)
    for m in re.finditer(keyword_pattern, text, re.IGNORECASE):
        segment = text[m.end(): m.end() + window]
        offset = m.end()
        for mm in cur_amt.finditer(segment):
            if _plausible_amount(mm.group(2)) and not _is_date_fragment(
                    text, offset + mm.start(2), offset + mm.end(2)):
                return float(mm.group(2).replace(",", "")), mm.group(1).upper()[:3]
        for mm in amt_cur.finditer(segment):
            if _plausible_amount(mm.group(1)) and not _is_date_fragment(
                    text, offset + mm.start(1), offset + mm.end(1)):
                return float(mm.group(1).replace(",", "")), mm.group(2).upper()[:3]
        for mm in _DOLLAR_AMOUNT.finditer(segment):
            if _plausible_amount(mm.group(2)) and not _is_date_fragment(
                    text, offset + mm.start(2), offset + mm.end(2)):
                return float(mm.group(2).replace(",", "")), "USD"
        for mm in re.finditer(_AMOUNT, segment):
            # A bare number is only plausible money when properly
            # thousands-grouped ("5,000,000"); otherwise it could be an
            # IBAN, phone or building number.
            if _is_grouped_amount(mm.group(1)) and not _is_date_fragment(
                    text, offset + mm.start(), offset + mm.end()):
                return float(mm.group(1).replace(",", "")), None
    return None, None


def _money_anywhere(text: str):
    """Fallback: the first plausible, explicitly-currency-denominated amount
    in the document. A bare number is never money — without a currency token
    it could be a page number, a clause id or a per-unit price."""
    cur_amt = re.compile(r"(" + _CURRENCY + r")[\s$]*(" + _AMOUNT + r")", re.IGNORECASE)
    for m in cur_amt.finditer(text):
        if _plausible_amount(m.group(2)) and not _is_date_fragment(text, m.start(2), m.end(2)):
            return float(m.group(2).replace(",", "")), m.group(1).upper()[:3]
    for m in _DOLLAR_AMOUNT.finditer(text):
        if _plausible_amount(m.group(2)) and not _is_date_fragment(text, m.start(2), m.end(2)):
            return float(m.group(2).replace(",", "")), "USD"
    # Amount followed by the currency spelled out in prose, e.g.
    # "pay the sum of 7,500,000 in United States Dollars".
    # _AMOUNT and _SPOKEN_CURRENCY.pattern each carry their own capture group.
    for m in re.finditer(
            _AMOUNT + r"[\s\S]{0,40}?" + _SPOKEN_CURRENCY.pattern, text, re.IGNORECASE):
        if _plausible_amount(m.group(1)) and not _is_date_fragment(text, m.start(1), m.end(1)):
            code = _SPOKEN_TO_CODE.get(m.group(2).lower().replace("  ", " "))
            if code:
                return float(m.group(1).replace(",", "")), code
    return None, None


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
    issuer = _clean_name(_grep(r"(?:borrower|obligor)[\s:]+([^,\n]{3,80})", text))
    lender = _clean_name(_grep(r"(?:lender|bank)[\s:]+([^,\n]{3,80})", text))
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

    issuer = _clean_name(_grep(r"(?:issuer|originator)[\s:]+([^,\n]{3,80})", text))
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
    funder = _clean_name(_grep(r"(?:call(?:ed|ing)? to|notice.*(?:capital|call|contribution).*from|lp|limited partner)[^\n,]*:\s*([^\n,]{2,80})", text))
    # Fallback: look for "Name:" near fund names
    if not funder:
        funder = _clean_name(_grep(r'(?:fund|lp|limited partner)(?:\s+name)?[:\s]+([^\n,]{2,80})', text))
    currency = (_grep(_CURRENCY, text) or "USD").upper()[:3]
    owing, owing_cur = _money_after(
        text,
        r"(?:capital call|called|amount (?:(?:to )?be )?due|contribution|amount due|call amount|together with)",
        window=60,
    )
    if owing_cur:
        currency = owing_cur
    # Fallback: explicit currency required — a bare number is never money.
    if owing is None:
        owing, owing_cur = _money_anywhere(text)
        if owing_cur:
            currency = owing_cur
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
    fund_name = _clean_name(_grep(
        r"(?:fund|issuer|vehicle)\s*name[\s:]+([^\n,]{2,80})",
        text
    ))
    if fund_name:
        fund_name = fund_name.lstrip(": \t").strip()
    if not fund_name:
        fund_name = _clean_name(_grep(r"(?:on behalf of|for the account of)[^\n]{0,50}?([^\n,]{2,80})", text))
    if not fund_name:
        # Fund documents name the vehicle in a heading like
        # "Elzaad Sukuk Fund (The Fund)" — no "Fund Name:" label anywhere.
        fund_name = _clean_name(_grep(
            r"([^\n]{2,80}?)\s*\(\s*(?:the\s+)?fund\s*\)", text))

    investor_name = _clean_name(_grep(
        r"(?:investor|subscriber|limited partner|lp)\s*name[\s:]+([^\n,]{2,80})",
        text
    ))
    if investor_name:
        investor_name = investor_name.lstrip(": \t").strip()
    if not investor_name:
        investor_name = _clean_name(_grep(r"(?:the undersigned|investor name)[^\n]{0,30}?([^\n,]{2,80})", text))

    commitment, commit_cur = _money_after(
        text, r"(?:capital|subscription|commitment|committed)", window=80,
    )
    if commitment is None:
        # Fallback: explicit currency required — a bare number is never money
        # (a page number, a clause id or a per-unit price is not a commitment).
        commitment, commit_cur = _money_anywhere(text)

    currency = (commit_cur or _grep(_CURRENCY, text)
                or _spoken_currency(text) or "USD").upper()[:3]

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
