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

# Document types that classification can *never* produce — these are only
# ever assigned by the ingestion layer or human triage.
NON_CLASSIFIABLE: set[DocumentType] = {
    DocumentType.UNCLASSIFIED,
    DocumentType.OTHER,
    DocumentType.KYC,
}


@dataclass
class ClassificationResult:
    document_type: DocumentType
    confidence: float
    raw_output: str | None = None
    backend: str = "heuristic"  # "heuristic" | "claude"


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def _keyword_score(text_normalised: str, keywords: set[str]) -> int:
    return sum(1 for kw in keywords if kw in text_normalised)


def classify_with_heuristics(text: str) -> ClassificationResult:
    """Deterministic keyword classifier.

    Confidence formula for the two dominant classes: ``best/(best+other+1)``.
    A clean loan doc (~8 hits vs 0-1) clears the 0.75 gate; a genuinely
    hybrid document (4 vs 4) scores ~0.44 -> UNCLASSIFIED.
    """
    t = _normalise(text)
    loan_hits = _keyword_score(t, LOAN_KEYWORDS)
    sukuk_hits = _keyword_score(t, SUKUK_KEYWORDS)

    if loan_hits == sukuk_hits:
        # Tied: ambiguous by construction — never a guess at a real type.
        return ClassificationResult(
            document_type=DocumentType.UNCLASSIFIED,
            confidence=0.5 if loan_hits >= 5 else max(0.0, loan_hits / 10),
        )

    if loan_hits > sukuk_hits:
        best, other, doc_type = loan_hits, sukuk_hits, DocumentType.LOAN_AGREEMENT
    else:
        best, other, doc_type = sukuk_hits, loan_hits, DocumentType.SUKUK_CERTIFICATE

    confidence = best / (best + other + 1)

    # Too few distinctive hits to name a concrete class confidently. A
    # two-keyword fragment ("principal ... loan") is ambiguous, not a loan.
    if best <= 3:
        return ClassificationResult(
            document_type=DocumentType.UNCLASSIFIED,
            confidence=confidence,
        )

    return ClassificationResult(document_type=doc_type, confidence=confidence)


def classify_with_llm(text: str) -> ClassificationResult:
    """Claude structured classification — used when ``ANTHROPIC_API_KEY`` is set.

    The prompt asks for strict JSON and the result still runs through the
    *same* confidence gate, so the LLM can never silently override the
    no-guess rule.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "anthropic not installed; set ANTHROPIC_API_KEY or use the heuristic backend"
        ) from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    system = (
        "You classify private-capital legal documents. Return ONLY JSON:\n"
        '{"document_type": "<one of term_sheet|loan_agreement|sha|ppm|lpa|'
        'sukuk_certificate|fatwa|financial_statement|kyc|side_letter|safe|other>", '
        '"confidence": <0-1>}\n'
        'If you are not confident (below 0.75) return '
        '{"document_type": "unclassified", "confidence": <0-1>}.'
    )
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": text[:20000]}],
    )
    raw = resp.content[0].text.strip() if resp.content else "{}"
    payload = json.loads(raw)
    doc_type = DocumentType(payload.get("document_type", "unclassified"))
    confidence = float(payload.get("confidence", 0.0))
    return ClassificationResult(
        document_type=doc_type,
        confidence=confidence,
        raw_output=raw,
        backend="claude",
    )


def classify_document(text: str) -> ClassificationResult:
    """Run the classifier, gate on the confidence floor, emit a trace."""
    if settings.anthropic_api_key:
        result = classify_with_llm(text)
    else:
        result = classify_with_heuristics(text)

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