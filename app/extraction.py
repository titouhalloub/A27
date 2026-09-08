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
    LoanExtraction,
    SukukExtraction,
    CapitalCallExtraction,
    SubscriptionAgreementExtraction,
    EquitySubscriptionExtraction,
    extract_result_to_document_data,
)
from app.models.enums import DocumentType

_CURRENCY = r"\b(?:USD|EUR|GBP|MYR|AED|SAR|SGD|IDR|TRY)\b"
# Optional-currency wrapper for embedding in larger patterns (a bare "?" after
# _CURRENCY would try to quantify the trailing \b -> "nothing to repeat").
_CUR_OPT = r"(?:" + _CURRENCY + r")?"
# Currency codes are UPPERCASE tokens: matching them case-insensitively read
# 'TRY' out of 'Country' and 'the try blocks' (real regression). Standalone
# lookups use this case-sensitive compiled pattern.
_CURRENCY_RE = re.compile(_CURRENCY)


def _currency_of(text: str) -> str | None:
    m = _CURRENCY_RE.search(text)
    return m.group(0) if m else None
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
    cur_amt = re.compile(r"(" + _CURRENCY + r")[\s$]*(" + _AMOUNT + r")")
    amt_cur = re.compile(r"(" + _AMOUNT + r")\s*(" + _CURRENCY + r")")
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
            # $_AMOUNT is money by definition — the '$' token IS the
            # plausibility proof, so _plausible_amount doesn't apply.
            # '$1.00' and '$500' are legitimate prices/offerings even
            # though their integer part has < 4 digits.
            if not _is_date_fragment(text, offset + mm.start(2), offset + mm.end(2)):
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
    cur_amt = re.compile(r"(" + _CURRENCY + r")[\s$]*(" + _AMOUNT + r")")
    for m in cur_amt.finditer(text):
        if _plausible_amount(m.group(2)) and not _is_date_fragment(text, m.start(2), m.end(2)):
            return float(m.group(2).replace(",", "")), m.group(1).upper()[:3]
    for m in _DOLLAR_AMOUNT.finditer(text):
        # $_AMOUNT is money by definition — skip _plausible_amount.
        if not _is_date_fragment(text, m.start(2), m.end(2)):
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
    """Grep for a money pattern and float it — but only when the captured
    number is a plausible money magnitude. Without this guard a factsheet
    chart value like '1.14' next to the word 'Total' became total_size."""
    v = _grep(pattern, text)
    if not v:
        return None
    return float(v.replace(",", "")) if _plausible_amount(v) else None


def _hit(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def extract_loan(text: str) -> tuple[LoanExtraction | None, float]:
    issuer = _clean_name(_grep(r"(?:borrower|obligor)[\s:]+([^,\n]{3,80})", text))
    lender = _clean_name(_grep(r"(?:lender|bank)[\s:]+([^,\n]{3,80})", text))
    # "Principal: USD 2,500,000" — skip the colon/space/currency token, then
    # capture the amount. _CURRENCY is a non-capturing group, so the only
    # capture group is the numeric amount in _AMOUNT.
    principal = _amount(r"(?:principal|facility|loan)[^0-9\n]{0,40}?[\s$]*" + _CUR_OPT + r"[\s$]*" + _AMOUNT, text)
    currency = (_currency_of(text) or "USD").upper()[:3]
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
    total = _amount(r"(?:total|issue|size)[^0-9\n]{0,40}?[\s$]*" + _CUR_OPT + r"[\s$]*" + _AMOUNT, text)
    cur = (_currency_of(text) or "USD").upper()[:3]
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
    currency = (_currency_of(text) or "USD").upper()[:3]
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


_ENTITY_SUFFIX = (
    r"(?:Inc\.?|LLC|L\.L\.C\.?|Corp\.?|Corporation|Co\.?|Company|"
    r"Ltd\.?|Limited|LP|L\.P\.|LLP)"
)

_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _clean_issuer_prefix(raw: str | None) -> str | None:
    """Clean a company-name capture and reject junk.

    SEC subscription agreements name the issuer in a wrapped preamble:
    ``Investview, Inc. (the "Company")`` or ``5 Mile Brewing Company LLC, a
    Michigan limited liability company (the "Company")``. Stripping those tails
    and rejecting a bare suffix (``Inc.``, ``LLC``) is what turns a truncated
    capture like ``Inc. (the "Company"`` back into the real entity name.
    """
    if not raw:
        return None
    name = raw.strip().strip("\"'").strip()
    name = re.sub(
        r'\s*\(\s*(?:the\s+)?["“”\']?Company["“”\']?\s*\)\s*$', "", name,
        flags=re.IGNORECASE)
    name = re.sub(
        r"\s*\(\s*the\s*[\"“”']?Shares?[\"“”']?\s*\)\s*$", "", name,
        flags=re.IGNORECASE)
    name = re.sub(
        r"\s*,\s*(?:an?\s+)?[A-Z][a-zA-Z]*\s+limited\s+liability\s+company.*$",
        "", name, flags=re.IGNORECASE)
    name = re.sub(
        r"\s*,\s*(?:an?\s+)?[A-Za-z]+\s+(?:corporation|corp\.?|company).*$",
        "", name, flags=re.IGNORECASE)
    name = name.rstrip(" \t,;:-–—•·").strip()
    if not name:
        return None
    if name.lower().lstrip("0123456789., ") in _JUNK_NAMES:
        return None
    if sum(ch.isalpha() for ch in name) < 2:
        return None
    # A capture whose whole content is an entity suffix ('Inc.', 'LLC') is
    # the truncation bug, not a name -- and 'The Company'/'An LLC' are the
    # document's generic self-reference, not the issuer.
    core = re.sub(
        r"\b(?:Inc\.?|LLC|L\.L\.C\.?|Corp\.?|Corporation|Company|Ltd\.?|"
        r"Limited|LP|LLP)\b.*$", "", name, flags=re.IGNORECASE).strip(" ,")
    if core.lower() in {"", "the", "a", "an"}:
        return None
    if not core and len(name) <= 12:
        return None
    return name


def _extract_company_name(text: str) -> str | None:
    """Entity name, tolerant of the SEC preamble conventions and OCR glue."""
    cands: list[str | None] = []
    # 1. Explicit label ("Company Name:", "Issuer Name:").
    cands.append(_grep(
        r"(?:company|issuer|corporation|llc|inc)\s*name[\s:]+([^\n]{2,120})",
        text))
    # 2. ALL-CAPS heading line ending in an entity suffix
    #    ('5 MILE BREW COMPANY LLC' / 'INVESTVIEW, INC.'). Strongest signal:
    #    it is the document's own title block.
    cands.append(_grep(
        r"(?m)^\s*([A-Z0-9][A-Z0-9 .,&\"'-]{2,60}?\b(?:INC\.?|LLC|"
        r"CORP\.?|CORPORATION|COMPANY|LTD\.?|LIMITED|LP|LLP))\.?,?\s*$", text))
    # 3. "(the "Company")" preamble: "Investview, Inc. (the "Company")".
    cands.append(_grep(
        r'([A-Z0-9][A-Za-z0-9 .,&"\'-]{2,90}?)\s*' +
        r'\(\s*the\s*["“”\']?Company["“”\']?\s*\)', text))
    # 4. "X, a Michigan limited liability company (the ...)".
    cands.append(_grep(
        r'([A-Z0-9][A-Za-z0-9 .,&"\'-]{2,90}?)\s*,\s*(?:an?\s+)?[A-Z][a-zA-Z]+'
        r'\s+limited\s+liability\s+company', text))
    # 5. Sentence-start entity followed by a verb of the agreement.
    cands.append(_grep(
        r"([A-Z][A-Za-z0-9 .,&\"'-]{2,90}?\b(?:Inc\.?|LLC|Corp\.?|Corporation|"
        r"Company|Ltd\.?|Limited|LP|LLP)\b)\s+(?:has|is|hereby|authorized|"
        r"will|agrees|represents)", text))
    # 6. Digit-led entity (5 Mile Brewing Company LLC / Acme 2 LLC).
    cands.append(_grep(
        r"(\d[\w\s,]{2,60}(?:LLC|Inc\.?|Corporation|Corp\.?|Company|LP|"
        r"LLP|Ltd\.?))", text))
    for cand in cands:
        cleaned = _clean_issuer_prefix(cand)
        if cleaned:
            return cleaned
    return None


def _extract_subscriber_name(text: str) -> str | None:
    """The subscribing investor — the *buyer* in an equity subscription.

    Deliberately stricter than the issuer heuristics: a proposed CapTableEvent
    holder must be the entity the document itself names as the subscriber,
    not a guess from signature blocks or representative titles. Only the
    explicit preamble convention counts:

        "Jane Q. Investor (the \\"Subscriber\\") hereby subscribes..."
        "... (the \\"Purchaser\\") hereby subscribes for ..."
    """
    cands: list[str | None] = [
        # "X (the "Subscriber" / "Purchaser")" followed by subscribe-verb.
        _grep(
            r'([A-Z][A-Za-z0-9 .,&"\'-]{2,90}?)\s*'
            r'\(\s*the\s*["“”\']?(?:Subscriber|Purchaser)["“”\']?\s*\)\s*'
            r"(?:hereby\s+)?subscribes?\b",
            text,
        ),
        # Same preamble, verb anywhere later in the sentence (OCR line breaks).
        _grep(
            r'([A-Z][A-Za-z0-9 .,&"\'-]{2,90}?)\s*'
            r'\(\s*the\s*["“”\']?(?:Subscriber|Purchaser)["“”\']?\s*\)',
            text,
        ),
    ]
    for cand in cands:
        cleaned = _clean_name(cand)
        if cleaned:
            return cleaned
    return None


def _extract_state_of_incorporation(text: str) -> str | None:
    """Name the incorporation/formation state without confusing it with the
    governing-law state ('governed by the laws of the State of Delaware' is
    NOT incorporation)."""
    state = _grep(
        r"(?:state|jurisdiction)\s*(?:of\s*)?(?:incorporation|organization"
        r"|formation)[\s:]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text)
    if not state:
        state = _grep(
            r"(?:organized|incorporated|formed)[^.]{0,160}?"
            r"under\s+the\s+laws\s+of\s+(?:the\s+[Ss]tate\s+of\s+)?"
            r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", text)
    if not state:
        state = _grep(
            r"(?:a|an)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+limited\s+"
            r"liability\s+company", text)
    if not state:
        state = _grep(
            r"existing\s+under\s+the\s+laws\s+of\s+(?:the\s+[Ss]tate\s+of\s+)?"
            r"([A-Z][a-zA-Z]+)", text)
    return state
def _extract_security_type(text: str) -> str | None:
    """Security being offered/subscribed: 'Series A Preferred stock',
    '100,000 shares (the "Shares") of Series A Preferred stock',
    'Non-Voting Common Units'. Never returns a bare share-count string."""
    cands: list[str | None] = []
    cands.append(_grep(
        r"(?:security|unit|share)\s*type[\s:]+([^\n,]{2,80})", text))
    cands.append(_grep(
        r"\b(Series\s+[A-Z1-9]\s+(?:[A-Za-z]+[- ])?(?:Preferred|Common)\s+"
        r"(?:Stock|Shares?|Units|Interests))\b", text))
    cands.append(_grep(
        r"\d[\d,]*(?:\.\d+)?\s+shares?\s*(?:\([^)]*\))?\s+of\s+"
        r"([A-Za-z][^,\n]{2,60})", text))
    cands.append(_grep(
        r"(?:(?:\d[\d,]*(?:\.\d+)?)\s*)?((?:Non-Voting\s+|Voting\s+)?"
        r"(?:Common|Preferred)\s+(?:Units|Shares|Interests|Stock))\b", text))
    cands.append(_grep(
        r"\b(Preferred\s+(?:Stock|Shares?|Units|Interests))\b", text))
    cands.append(_grep(
        r"\b(Common\s+(?:Stock|Shares?|Units|Interests))\b", text))
    for cand in cands:
        cleaned = _clean_name(cand)
        if cleaned:
            return cleaned
    return None


def _extract_share_count(text: str) -> int | None:
    """Number of shares/units being subscribed or offered for sale.

    Targets sale/subscription contexts ('authorized for sale 100,000 shares',
    'aggregate of 1,235,000 Non-Voting Common Units') and never pre-existing
    capitalization ('10,000,000 Units issued and outstanding').
    """
    m = re.search(
        r"(?:authorized\s+(?:for\s+sale|to\s+be\s+sold)|for\s+sale|"
        r"offer(?:ed)?\s+for\s+sale|to\s+purchase|purchase\s+of|"
        r"subscribes?\s+for|purchased\s+hereunder)"
        r"[^\n$]{0,80}?(\d{1,3}(?:,\d{3})*)\s+(?:shares?|units|interests)\b",
        text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(
        r"aggregate\s+of\s+(\d{1,3}(?:,\d{3})*)\s*[A-Za-z]*\s*"
        r"(?:(?:Non-)?Voting\s+|Common\s+|Preferred\s+)*"
        r"(?:Units|Shares|Interests)\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


_AI_MARKS = r"[Xx✓✔☑■●▪✗✘]"


def _extract_accredited_category(text: str) -> str | None:
    """Which accredited-investor Category (A-H) the subscriber selected.

    SEC subscription forms lay out Categories A-H as blanks to mark
    ('Category A___X'). Only an explicit mark counts -- the blank template
    ('Category A___ The undersigned is ...') stays None, never a guess.
    """
    for m in re.finditer(r"Category\s*([A-H])\b", text, re.IGNORECASE):
        tail = text[m.end(): m.end() + 160]
        if re.search(
                r"(?:^\s*_*\s*[Xx]|\b[Xx]\b|[_\-]{2,}\s*[Xx]|"
                r"[\[(]\s*[Xx]\s*[\])]|" + _AI_MARKS + r")", tail):
            return f"Category {m.group(1).upper()}"
    return None


def _parse_doc_date(text: str) -> date | None:
    """Document date ('May 29, 2015') -- the first valid date in the text is
    almost always the agreement date for SEC exhibits (the title line)."""
    m = re.search(r"\b([A-Z][a-z]{2,9})\s+(\d{1,2}),\s+(\d{4})\b", text)
    if m:
        month = _MONTH_NUM.get(m.group(1).lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                return None
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None
def extract_equity_subscription(text: str) -> tuple[EquitySubscriptionExtraction | None, float]:
    """US LLC / Corp equity subscription agreement (incl. SEC-form exhibits).

    An SEC exhibit wraps the issuer as 'Investview, Inc. (the "Company")',
    states the offering as 'authorized for sale 100,000 shares ... of Series A
    Preferred stock ... for the maximum offering of $5,000,000' and the
    subscriber's price as a 'cash purchase price of $5,000,000'. When a
    per-share price is not stated but the share count and subscription price
    both are, the effective per-share price is their quotient -- arithmetic on
    facts the document actually states, never a guess.
    """
    company_name = _extract_company_name(text)
    state = _extract_state_of_incorporation(text)
    security = _extract_security_type(text)
    share_count = _extract_share_count(text)
    price, _ = _money_after(
        text, r"(?:price\s*per|per\s*unit|per\s*share)", window=60)
    total, cur = _money_after(
        text,
        r"(?:total\s*(?:offering|issue|amount)|maximum\s+offering\s*|"
        r"offering\s*amount|up\s*to|aggregate\s*of)",
        window=80)
    if total is None:
        total, cur = _money_after(text, r"(?:offering|issue|sell)", window=100)
    minimum, _ = _money_after(
        text,
        r"(?:minimum\s*(?:investment|subscription)|minimum\s*amount|"
        r"less\s*than)",
        window=80)
    investment, inv_cur = _money_after(
        text,
        r"(?:cash\s+purchase\s+price|subscription\s+price|investment\s+amount|"
        r"purchase\s+price|amount\s+invested)",
        window=80)
    # Effective per-share price from facts the document states outright.
    if price is None and share_count and investment:
        per_share = investment / share_count
        if 0 < per_share <= 1_000_000:
            price = per_share

    currency = (cur or inv_cur or _currency_of(text) or "USD").upper()[:3]
    # A '$'-prefixed amount is unambiguously dollar-denominated even when the
    # document never spells out a currency code (Brew: '$500', '$1.00').
    currency_found = bool(cur or inv_cur or _currency_of(text)
                          or _DOLLAR_AMOUNT.search(text))
    category = _extract_accredited_category(text)
    doc_date = _parse_doc_date(text)
    subscriber = _extract_subscriber_name(text)

    if not any((company_name, state, security, share_count, price,
                total, minimum, investment)):
        return None, 0.0

    # Anchors (who/what/how-much + currency) carry the confidence weight 1.0
    # each.  Deal-shaping fields (state, per-share price, minimum) add 0.6.
    # Detail fields (share count, subscription price, date, accreditation
    # category) only ever ADD signal -- a blank Category A-H form, an absent
    # minimum, or an unstated share count are honest Nones, not failures, so
    # their absence is never penalized.
    num = (sum(1.0 for f in (company_name, total, security, currency_found) if f)
           + 0.6 * sum(1 for f in (state, price, minimum) if f)
           + 0.3 * sum(1 for f in (share_count, investment, doc_date, category) if f))
    den = 4.0 + 0.6 * 3 + 0.3 * 4
    conf = min(1.0, num / den)

    return EquitySubscriptionExtraction(
        company_name=company_name,
        state_of_incorporation=state,
        security_type=security,
        price_per_unit=price,
        total_offering_amount=total,
        minimum_investment=minimum,
        currency=currency,
        share_count=share_count,
        subscription_price_per_share=price,
        investment_amount=investment,
        accredited_investor_category=category,
        document_date=doc_date,
        subscriber_name=subscriber,
        source_text=text[:2000],
    ), conf
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
    if not fund_name:
        # US LLC equity subscriptions: "5 Mile Brewing Company LLC"
        fund_name = _clean_name(_grep(
            r"([A-Z][^\n,]{2,80}?\b(?:LLC|Inc|Ltd|Corp|Company)\b[^\n,]{0,20})",
            text))
    if not fund_name:
        # US LLC equity subscriptions: "5 Mile Brewing Company LLC, a
        # Michigan limited liability company (the \"Company\")"
        fund_name = _clean_name(_grep(
            r"([A-Z][^\n,]{2,80}?\b(?:LLC|Inc|Ltd|Corp|Company)\b[^\n,]{0,20})",
            text))

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

    currency = (commit_cur or _currency_of(text)
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
    "equity_subscription": extract_equity_subscription,
}

EXTRACTION_ROUTE_NAMES: dict[DocumentType, str] = {
    DocumentType.LOAN_AGREEMENT: "LoanExtraction",
    DocumentType.SUKUK_CERTIFICATE: "SukukExtraction",
    DocumentType.CAPITAL_CALL_NOTICE: "CapitalCallExtraction",
    DocumentType.SUBSCRIPTION_AGREEMENT: "SubscriptionAgreementExtraction",
    DocumentType.EQUITY_SUBSCRIPTION: "EquitySubscriptionExtraction",
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
