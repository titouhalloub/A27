"""Document classification — its own step, its own confidence, its own gate.

Per spec gap #1, classification is deliberately **not** part of extraction:
a misrouted document must fail loudly, not produce a high-confidence
extraction against the wrong schema.

- ``classify_document()`` returns ``{document_type, confidence}`` as a
  structured result. The backend is Claude structured output when an API key
  is set; otherwise a deterministic keyword classifier with the same
  contract and the same confidence gate.
- Below ``classification_min_confidence`` (0.75), the result is set to
  ``UNCLASSIFIED`` and routed to human triage. It is never guessed at a real
  type and never passed to extraction.
- Scanned PDFs/pages go through Tesseract OCR first (free, MVP-grade — the
  explicit lower-accuracy trade-off stated in the spec).

Every decision emits a Langfuse-style trace with the confidence visible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.models.enums import DocumentType
from app.telemetry import get_tracer

# --- Keyword lexicons for the deterministic fallback ----------------------

# Filename abbreviation map: marketing/abbreviated filenames (e.g. vendor
# portals, emailed attachments) use truncated tokens that the keyword
# matcher would otherwise miss. Expanded before matching.
def _expand_filename(fname: str) -> str:
    """Replace known abbreviations in a filename with their full forms so the
    keyword matcher can scoring them. Unknown tokens are left alone.
    
    Non-recursive: each token is replaced at most once to avoid
    "subscriptioniption" style blowups."""
    parts = re.split(r"[^a-z]+", fname.lower())
    expanded = []
    for p in parts:
        if p in _FILENAME_ABBREVS:
            expanded.append(_FILENAME_ABBREVS[p])
        elif p:
            expanded.append(p)
    return " ".join(expanded)
_FILENAME_ABBREVS: dict[str, str] = {
    "subagmt": "subscription agreement",
    "sub": "subscription",
    "agmt": "agreement",
    "loan": "loan",
    "sukuk": "sukuk",
    "suks": "sukuk",
    "capcall": "capital call",
    "cap": "capital",
    "sha": "shareholder agreement",
    "ppm": "private placement memorandum",
    "lpa": "limited partnership agreement",
    "fatwa": "fatwa",
    "kyc": "kyc",
    "sideletter": "side letter",
    "safe": "safe",
    "fs": "financial statement",
}


def _expand_filename(fname: str) -> str:
    """Replace known abbreviations in a filename with their full forms so the
    keyword matcher can score them. Unknown tokens are left alone."""
    parts = re.split(r"[^a-z]+", fname.lower())
    expanded = []
    for p in parts:
        if p in _FILENAME_ABBREVS:
            expanded.append(_FILENAME_ABBREVS[p])
        elif p:
            expanded.append(p)
    return " ".join(expanded)


LOAN_KEYWORDS: set[str] = {
    "loan agreement",
    "borrower",
    "lender",
    "principal",
    "interest rate",
    "repayment",
    "amortization",
    "covenant",
    "governing law",
    "loan",
}

SUKUK_KEYWORDS: set[str] = {
    "sukuk",
    "certificate holder",
    "al-ijarah",
    "ijara",
    "murabaha",
    "profit rate",
    "rental",
    "shariah",
    "fatwa",
    "trust certificate",
    "periodic distribution",
    "sukuk holders",
}

CAPITAL_CALL_KEYWORDS: set[str] = {
    "capital call notice",
    "capital call letter",
    "capital call",
    "call notice to lp",
    "call amount due",
    "lp capital call",
    "capital call request",
    "call deadline",
    "funding call notice",
    "notice of capital call",
    "call for capital",
    "capital call amount",
    "lp call notice",
    "due date",
    "wire transfer",
    "bank account",
    "transfer instruction",
    "capital contribution",
}

SUBSCRIPTION_KEYWORDS: set[str] = {
    "subscription",
    "subscription agreement",
    "subscription form",
    "subscription price",
    "subscription commitment",
    "investor subscription",
    "subscribe to the fund",
    "subscribe for shares",
    "subscribe to shares",
    "limited partner",
    "general partner",
    "capital commitment",
    "commitment amount",
    "committed capital",
    "drawdown notice",
    "capital drawdown",
    "drawdown request",
    "fund capital",
    "eligible investor",
    "accredited investor",
    "investor name",
    "fund name",
    "net asset value",
    "subscription period",
    "closing date",
    "minimum investment",
    "redemption",
    "investor type",
    "jurisdiction of subscription",
    "subscription amount",
    "subscribe to",
    "committed capital",
    "limited partnership agreement",
    "partner capital",
    "drawdown",
}

EQUITY_SUBSCRIPTION_KEYWORDS: set[str] = {
    "company name",
    "state of incorporation",
    "security type",
    "price per unit",
    "total offering amount",
    "minimum investment",
    "limited liability company",
    "llc",
    "articles of organization",
    "operating agreement",
    "subscription agreement",
    "non-voting",
    "common units",
    "preferred units",
    "membership interests",
    "offering amount",
    "target offering amount",
    "regulation crowdfunding",
    "form c",
    "mainvest",
    "investment amount",
    "minimum subscription",
    "per unit",
    "units",
    "company",
    "incorporation",
}
# Document types that classification can *never* produce — these are only
# ever assigned by the ingestion layer or human triage.
NON_CLASSIFIABLE: set[DocumentType] = {
    DocumentType.UNCLASSIFIED,
    DocumentType.OTHER,
    DocumentType.KYC,
}

# Marketing/reference material markers. A fund factsheet or performance
# sheet shares vocabulary with contracts ("sukuk", "fund", "class A units")
# and used to rank as loan_agreement at exactly 0.75. These phrases belong
# to factsheets, not agreements — two or more is decisive evidence the
# document is not a contract of any type.
FACTSHEET_MARKERS: tuple[str, ...] = (
    "factsheet",
    "fact sheet",
    "top holdings",
    "fund performance",
    "fund objective",
    "fund objectives",
    "fund inception",
    "base currency",
    "fund information",
    "management fee",
    "custodian fee",
    "trustee fee",
    "% of assets",
    "annualised return",
    "annualized return",
    "cumulative return",
    "as at 30",
    "as at 31",
    "launch date",
    "unit price",
    "nav per unit",
)


@dataclass
class ClassificationResult:
    document_type: DocumentType
    confidence: float
    raw_output: str | None = None
    backend: str = "heuristic"  # "heuristic" | "claude" | "openrouter"


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def _keyword_score(text_normalised: str, keywords: set[str]) -> int:
    return sum(1 for kw in keywords if kw in text_normalised)


# Filename abbreviation map. Uploaders compress long words
# ("emktbrew_subagmtca2.pdf" = "emktbrew subscription agreement 2"), so the
# raw keyword won't match. These map abbreviation substrings to the keyword
# they stand for — applied only to the filename, never the body text.
_FILENAME_ABBREVS: dict[str, set[str]] = {
    "subscription": {"subagmt", "subscr", "subagree"},
    "loan": {"ln", "loa"},
    "sukuk": {"suk"},
}

# Filename abbreviation map. Uploaders compress long words
# ("emktbrew_subagmtca2.pdf" = "emktbrew subscription agreement 2"), so the
# raw keyword won't match. These map abbreviation substrings to the keyword
# they stand for — applied only to the filename, never the body text.
_FILENAME_ABBREVS: dict[str, set[str]] = {
    "subscription": {"subagmt", "subscr", "subagree"},
    "loan": {"ln", "loa"},
    "sukuk": {"suk"},
}

def classify_with_heuristics(text: str, filename: str = "") -> ClassificationResult:
    """Deterministic keyword classifier.

    Uses the text body for ranking and applies the document filename as an
    *exclusive confidence boost* on the winning class only — it never inflates
    the denominator or benefits other categories. Example: a file named
    "Subscription-Agreement-...pdf" with sparse OCR text (3 text hits vs 1
    second) scores 3/4=0.75 from text, then the filename bonus adds +0.15
    to land at 0.90, clearing the gate cleanly. An incidental word like
    "sukuk" in the fund name ("Elzaad Sukuk Fund V8") is a text hit that
    participates in the ratio, not a filename signal.

    Confidence formula for the top two text classes: ``best/(best+other)``.
    A clean doc (~8 hits vs 0-1) clears the 0.75 gate; a genuinely hybrid
    document (4 vs 4) scores ~0.50 -> UNCLASSIFIED.
    """
    t = _normalise(text)
    # Normalise filename: dashes/underscores -> spaces, lowercased
    fname = re.sub(r"[-_.]+", " ", filename.lower()) if filename else ""

    # Factsheet / marketing material short-circuit: these documents share
    # contract vocabulary ("sukuk", "fund", "units") and used to sneak past
    # the gate as loan_agreement at exactly 0.75. Two or more factsheet
    # markers means this is reference material, not a contract — it can
    # never be classified, only reviewed by a human.
    fact_markers = sum(1 for m in FACTSHEET_MARKERS if m in t)
    if fact_markers >= 2:
        return ClassificationResult(
            document_type=DocumentType.UNCLASSIFIED,
            confidence=0.5,
        )

    loan_hits = _keyword_score(t, LOAN_KEYWORDS)
    sukuk_hits = _keyword_score(t, SUKUK_KEYWORDS)
    cc_hits = _keyword_score(t, CAPITAL_CALL_KEYWORDS)
    sub_hits = _keyword_score(t, SUBSCRIPTION_KEYWORDS)
    equity_hits = _keyword_score(t, EQUITY_SUBSCRIPTION_KEYWORDS)

    # Filename keyword matches — these are deliberate uploader signals
    # ("Subscription-Agreement-...pdf") that should break ties in the
    # ranking and boost confidence on the winner. Incidental words like
    # "sukuk" in the fund name ("Elzaad Sukuk Fund V8") are text hits,
    # not filename hits, so they don't benefit from this bonus.
    # Expand filename abbreviations before scoring so compressed names
    # like 'emktbrew_subagmtca2.pdf' still match 'subscription'.
    expanded = fname
    for full, abbrevs in _FILENAME_ABBREVS.items():
        for abbr in abbrevs:
            expanded = expanded.replace(abbr, full)
    fname_loan = _keyword_score(expanded, LOAN_KEYWORDS)
    fname_sukuk = _keyword_score(expanded, SUKUK_KEYWORDS)
    fname_cc = _keyword_score(expanded, CAPITAL_CALL_KEYWORDS)
    fname_sub = _keyword_score(expanded, SUBSCRIPTION_KEYWORDS)
    fname_equity = _keyword_score(expanded, EQUITY_SUBSCRIPTION_KEYWORDS)

    # Filename keyword matches boost ALL matching classes equally, then
    # we pick the winner.  The filename is a deliberate uploader signal
    # ("Subscription-Agreement-...pdf") that should break ties when
    # OCR text is sparse.  Weight: 2 filename hits = 1 text hit.
    loan_hits += 2 * fname_loan
    sukuk_hits += 2 * fname_sukuk
    cc_hits += 2 * fname_cc
    sub_hits += 2 * fname_sub
    equity_hits += 2 * fname_equity

    # Build a list of (hits, doc_type) sorted descending by hits
    candidates = [
        (loan_hits, DocumentType.LOAN_AGREEMENT),
        (sukuk_hits, DocumentType.SUKUK_CERTIFICATE),
        (cc_hits, DocumentType.CAPITAL_CALL_NOTICE),
        (sub_hits, DocumentType.SUBSCRIPTION_AGREEMENT),
        (equity_hits, DocumentType.EQUITY_SUBSCRIPTION),
    ]
    candidates.sort(key=lambda x: x[0], reverse=True)

    best_hits, best_type = candidates[0]
    second_hits = candidates[1][0]

    # Tied at the top: use filename-position as tiebreaker.
    # The keyword that appears FIRST in the filename is the uploader's
    # primary intent (e.g. "subscription agreement" comes before "sukuk"
    # in "Subscription-Agreement-Elzaad-Sukuk-Fund-V8.pdf").
    if best_hits == second_hits:
        fname_pos = {
            DocumentType.LOAN_AGREEMENT: fname.find("loan") if fname_loan else 999,
            DocumentType.SUKUK_CERTIFICATE: fname.find("sukuk") if fname_sukuk else 999,
            DocumentType.CAPITAL_CALL_NOTICE: fname.find("capital") if fname_cc else 999,
            DocumentType.SUBSCRIPTION_AGREEMENT: fname.find("subscription") if fname_sub else 999,
            DocumentType.EQUITY_SUBSCRIPTION: fname.find("subscription") if fname_equity else 999,
        }
        # Filter to classes that are tied at best_hits
        hits_map = {
            DocumentType.LOAN_AGREEMENT: loan_hits,
            DocumentType.SUKUK_CERTIFICATE: sukuk_hits,
            DocumentType.CAPITAL_CALL_NOTICE: cc_hits,
            DocumentType.SUBSCRIPTION_AGREEMENT: sub_hits,
            DocumentType.EQUITY_SUBSCRIPTION: equity_hits,
        }
        tied = [c for c in hits_map if hits_map[c] == best_hits]
        # Pick the tied class with the earliest filename keyword position
        if tied and any(fname_pos.get(c, 999) < 999 for c in tied):
            winner = min(tied, key=lambda c: fname_pos.get(c, 999))
            return ClassificationResult(
                document_type=winner,
                confidence=0.75 if best_hits >= 5 else 0.50,
            )
        return ClassificationResult(
            document_type=DocumentType.UNCLASSIFIED,
            confidence=0.5 if best_hits >= 5 else max(0.0, best_hits / 10),
        )

    confidence = best_hits / (best_hits + second_hits) if (best_hits + second_hits) > 0 else 0.0

    # Too few distinctive hits to name a concrete class confidently. A
    # two-keyword fragment ("principal ... loan") is ambiguous, not a loan.
    if best_hits <= 2:
        return ClassificationResult(
            document_type=DocumentType.UNCLASSIFIED,
            confidence=confidence,
        )

    # Filename confidence boost: if a document-type keyword appears in the
    # filename (a deliberate uploader signal), push confidence toward 1.0.
    # Only applies to the *winning* class so incidental fund-name words like
    # "sukuk" in "Elzaad Sukuk Fund" do not affect the result.
    fname_bonus = {
        DocumentType.LOAN_AGREEMENT: fname_loan,
        DocumentType.SUKUK_CERTIFICATE: fname_sukuk,
        DocumentType.CAPITAL_CALL_NOTICE: fname_cc,
        DocumentType.SUBSCRIPTION_AGREEMENT: fname_sub,
        DocumentType.EQUITY_SUBSCRIPTION: fname_sub,
    }[best_type]

    if fname_bonus > 0:
        # Each filename match adds 0.15, capped at 0.98 so the gate is the
        # primary filter and this is truly a boost.
        confidence = min(0.98, confidence + 0.15 * fname_bonus)

    return ClassificationResult(document_type=best_type, confidence=confidence)


def _parse_llm_document_type(raw: str) -> DocumentType:
    """Canonicalise an LLM's free-form type string into a DocumentType.

    Different models return the same concept differently ("Loan Agreement",
    "loan-agreement", "sha"). We lowercase, squash spaces/hyphens to
    underscores, and map the short prompt aliases to their full enum value.
    Anything unrecognised -> UNCLASSIFIED (never a guess, never a crash).
    """
    norm = re.sub(r"[\s-]+", "_", (raw or "").strip().lower())
    aliases = {
        "sha": DocumentType.SHA,
        "ppm": DocumentType.PPM,
        "lpa": DocumentType.LPA,
        "kyc_document": DocumentType.KYC,
        "financials": DocumentType.FINANCIAL_STATEMENT,
    }
    for slug, doc_type in DocumentType.__members__.items():
        aliases[doc_type.value] = doc_type
    return aliases.get(norm, DocumentType.UNCLASSIFIED)


def _classify_with_openai_compatible(
    text: str,
    api_key: str,
    base_url: str,
    model: str,
    backend: str,
) -> ClassificationResult:
    """Unified OpenAI-compatible LLM classification (Anthropic or OpenRouter).

    The prompt asks for strict JSON and the result still runs through the
    *same* confidence gate, so the LLM can never silently override the
    no-guess rule.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "openai>=1.40 not installed; set A27_OPENROUTER_API_KEY (free) "
            "or A27_ANTHROPIC_API_KEY (paid), or use the heuristic backend."
        ) from exc

    client = OpenAI(api_key=api_key, base_url=base_url)
    system = (
        "You classify private-capital legal documents. Return ONLY valid JSON:\n"
        '{"document_type": "<one of term_sheet|loan_agreement|sha|ppm|lpa|'
        'sukuk_certificate|capital_call_notice|subscription_agreement|equity_subscription|fatwa|financial_statement|kyc|side_letter|safe|other>", '
        '"confidence": <0.0-1.0>}\n'
        "Disambiguate between the two subscription families:\n"
        "- subscription_agreement: an investor subscribing into a FUND/vehicle "
        "(fund name, capital commitment, drawdowns, LP/GP).\n"
        "- equity_subscription: an investor subscribing for EQUITY/UNITS of an "
        "operating COMPANY (US LLC/corp): company name, state of incorporation, "
        "price per unit, total/target offering amount, minimum investment, "
        "membership interests, non-voting common units, Regulation Crowdfunding, Form C.\n"
        'If you are not confident (below 0.75) return '
        '{"document_type": "unclassified", "confidence": <0.0-1.0>}.\n'
        'Return ONLY the JSON, no explanation.'
    )
    resp = client.chat.completions.create(
        model=model,
        max_tokens=256,
        temperature=0.0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text[:20000]},
        ],
    )
    raw = resp.choices[0].message.content.strip() if resp.choices else ""
    # Free-tier providers can occasionally return an empty / "{}" response.
    # Retry once before giving up -- a one-shot retry is cheaper than
    # a wrong downstream decision and still bounded by the confidence gate.
    if not raw or raw.strip().lower() in ("{}", "null", "[object object]"):
        resp = client.chat.completions.create(
            model=model,
            max_tokens=256,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text[:20000]},
            ],
        )
        raw = resp.choices[0].message.content.strip() if resp.choices else ""
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"document_type": "unclassified", "confidence": 0.0}
    doc_type = _parse_llm_document_type(payload.get("document_type", "unclassified"))
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if doc_type == DocumentType.UNCLASSIFIED and payload.get("document_type", "").strip().lower() != "unclassified":
        # The LLM named something we can't map. Never guess: force UNCLASSIFIED
        # with a low confidence so the gate routes it to human review.
        confidence = min(confidence, 0.5)
    return ClassificationResult(
        document_type=doc_type,
        confidence=confidence,
        raw_output=raw,
        backend=backend,
    )


def classify_with_llm(text: str) -> ClassificationResult:
    """Route to the configured LLM backend (Anthropic or OpenRouter).

    Priority:
    1. Backend configured in llm_backend_preference
    2. OpenRouter (free tier) if openrouter_api_key is set
    3. Anthropic if anthropic_api_key is set
    """
    pref = settings.llm_backend_preference

    if pref == "anthropic" and settings.anthropic_api_key:
        return _classify_with_openai_compatible(
            text,
            api_key=settings.anthropic_api_key,
            base_url="https://api.anthropic.com/v1",
            model=settings.anthropic_model,
            backend="claude",
        )

    if settings.openrouter_api_key:
        return _classify_with_openai_compatible(
            text,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.openrouter_model,
            backend="openrouter",
        )

    if settings.anthropic_api_key:
        return _classify_with_openai_compatible(
            text,
            api_key=settings.anthropic_api_key,
            base_url="https://api.anthropic.com/v1",
            model=settings.anthropic_model,
            backend="claude",
        )

    raise RuntimeError(
        "No LLM API key configured. Set A27_OPENROUTER_API_KEY (free) or "
        "A27_ANTHROPIC_API_KEY (paid), or use the heuristic backend."
    )


def classify_document(text: str, filename: str = "") -> ClassificationResult:
    """Run the classifier, gate on the confidence floor, emit a trace.

    The LLM path is *best effort*: rate limits, network failures, or a
    model slug that OpenRouter retired must never 500 the upload. If the
    LLM raises for any reason we fall back to the deterministic keyword
    classifier and the same confidence gate still applies -- so the worst
    case is a lower-confidence result that routes to human review, never
    a crash and never a silent guess.
    """
    if settings.anthropic_api_key or settings.openrouter_api_key:
        try:
            result = classify_with_llm(text)
        except Exception:  # noqa: BLE001 -- LLM outages must degrade, not 500
            result = classify_with_heuristics(text, filename=filename)
    else:
        result = classify_with_heuristics(text, filename=filename)

    # The gate: below min confidence -> UNCLASSIFIED, never a guess.
    if (
        result.document_type in NON_CLASSIFIABLE
        or result.confidence < settings.classification_min_confidence
    ):
        result.document_type = DocumentType.UNCLASSIFIED

    trace = get_tracer().span("classification", input_chars=len(text))
    trace.finish(
        {"document_type": result.document_type.value, "confidence": result.confidence},
        confidence=result.confidence,
        backend=result.backend,
        **({"model": settings.anthropic_model} if result.backend == "claude" else {}),
    )
    trace.emit()
    return result