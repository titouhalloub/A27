"""Domain enums for the A27 unified private-capital pipeline.

These enum definitions are the single source of truth used by both the
SQLAlchemy ORM layer and the Pydantic validation layer.
"""

from enum import Enum


class ComplianceMode(str, Enum):
    TRADITIONAL = "traditional"
    ISLAMIC = "islamic"


class TransactionType(str, Enum):
    LOAN = "loan"
    EQUITY = "equity"
    SUKUK = "sukuk"
    PRIVATE_EQUITY = "private_equity"
    VENTURE_CAPITAL = "venture_capital"
    REAL_ASSET = "real_asset"
    FUND_INTEREST = "fund_interest"


class ShariahContractType(str, Enum):
    MURABAHA = "murabaha"
    IJARA = "ijara"
    MUSHARAKAH = "musharakah"
    WAKALAH = "wakalah"
    NONE = "none"


class DocumentType(str, Enum):
    TERM_SHEET = "term_sheet"
    LOAN_AGREEMENT = "loan_agreement"
    SHA = "shareholder_agreement"
    PPM = "private_placement_memorandum"
    LPA = "limited_partnership_agreement"
    SUKUK_CERTIFICATE = "sukuk_certificate"
    FATWA = "fatwa"
    FINANCIAL_STATEMENT = "financial_statement"
    KYC = "kyc"
    SIDE_LETTER = "side_letter"
    SAFE = "safe"
    UNCLASSIFIED = "unclassified"
    OTHER = "other"


class ShariahReviewStatus(str, Enum):
    """Replaces the bare bool. The system may only ever set
    SYSTEM_FLAGGED_NONCOMPLIANT or PENDING_SCHOLAR_REVIEW.

    SCHOLAR_APPROVED and SCHOLAR_REJECTED are *human-only* transitions and
    are enforced by the model validator / human-review context in
    ``app.models.review``.
    """

    NOT_APPLICABLE = "not_applicable"
    SYSTEM_FLAGGED_NONCOMPLIANT = "system_flagged_noncompliant"
    PENDING_SCHOLAR_REVIEW = "pending_scholar_review"
    SCHOLAR_APPROVED = "scholar_approved"
    SCHOLAR_REJECTED = "scholar_rejected"

    # System may set these on its own authority:
    SYSTEM_SETTABLE = frozenset(
        {NOT_APPLICABLE, SYSTEM_FLAGGED_NONCOMPLIANT, PENDING_SCHOLAR_REVIEW}
    )
    # Only a human reviewer may set these:
    HUMAN_ONLY = frozenset({SCHOLAR_APPROVED, SCHOLAR_REJECTED})


class IngestionSource(str, Enum):
    NATIVE_EXTRACTION = "native_extraction"
    VENDOR_IMPORT = "vendor_import"  # e.g. Canoe / Passthrough API
    MANUAL_ENTRY = "manual_entry"


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    REVIEW_NEEDED = "review_needed"  # low confidence / unclassified
    FAILED = "failed"


class LedgerEntryType(str, Enum):
    CATEGORY_MUTATION = "category_mutation"  # a line item, e.g. capital call
    DOCUMENT_RESULT = "document_result"
    COMPLIANCE_EVENT = "compliance_event"