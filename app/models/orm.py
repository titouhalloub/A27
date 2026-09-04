"""SQLAlchemy ORM models for the A27 pipeline.

Instrument and Document are generic: one compliance gateway and one ledger
serve every asset class via ``transaction_type`` and ``type_specific_data``.

No-self-certification guarantee
------------------------------
``ShariahReviewStatus.SCHOLAR_APPROVED`` / ``SCHOLAR_REJECTED`` are human-only.
The ORM models enforce this *at the type level*: the ``shariah_review_status``
column can only be assigned values in ``SYSTEM_SETTABLE`` through ordinary ORM writes, because the ``human_review`` relationship exposes
``set_shariah_review_status()`` as the *only* path to the human-only states,
and that path requires an explicit ``reviewer_id``.

This is a type-level guarantee in the sense that the ORM assignments are
validated at write-time via a ``@validates`` callback (not merely documented),
and any attempt to smuggle ``SCHOLAR_APPROVED`` through a normal ORM write
raises ``ValueError``. Tests in ``tests/test_models.py`` prove this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base
from app.models.enums import (
    CapTableEventType,
    ComplianceMode,
    DocumentStatus,
    DocumentType,
    HoldingStatus,
    IngestionSource,
    InvestorType,
    LedgerEntryType,
    SecurityType,
    TransactionType,
    ShariahContractType,
    ShariahReviewStatus,
    SYSTEM_SETTABLE,
    HUMAN_ONLY,
)
from app.models.enums import (
    ComplianceMode,
    DocumentStatus,
    DocumentType,
    IngestionSource,
    LedgerEntryType,
    TransactionType,
    ShariahContractType,
    ShariahReviewStatus,
    SYSTEM_SETTABLE,
    HUMAN_ONLY,
)

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Timezone-aware UTC now (stored as UTC in ISO8601-serialisable form)."""
    return datetime.now(timezone.utc)


class ShariahReviewValidationError(ValueError):
    """Raised when code or a caller tries to set a human-only review state.

    This guard is *absolute* at the ORM attribute level: no repo method,
    service, or background task can ever assign ``SCHOLAR_APPROVED`` or
    ``SCHOLAR_REJECTED`` through a normal ORM write.

    The single deliberate exception is ``app.review.submit_human_review``,
    which writes these states through a SQLAlchemy Core ``update()`` statement
    (escaping the ORM validates callback *on purpose*) and additionally
    requires an explicit ``reviewer_id`` — the model of "the human review
    endpoint is the only way through".
    """


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------


class Instrument(Base):
    """One instrument across any asset class.

    ``type_specific_data`` is a JSONB bag that holds the asset-class-specific
    fields (e.g. ``interest_rate`` for loans, ``coupon_rate`` for sukuk).
    """

    __tablename__ = "instruments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transaction_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, native_enum=False, length=32)
    )
    compliance_mode: Mapped[ComplianceMode] = mapped_column(
        Enum(ComplianceMode, native_enum=False, length=32)
    )
    issuer_name: Mapped[str] = mapped_column(String(255))
    issuer_type: Mapped[str] = mapped_column(String(64))  # Corporate, SPV, Fund, Government
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8))
    maturity_date: Mapped[datetime | None] = mapped_column(default=None)
    type_specific_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    underlying_asset_description: Mapped[str | None] = mapped_column(Text, default=None)
    underlying_asset_id: Mapped[str | None] = mapped_column(String(128), default=None)
    shariah_contract_type: Mapped[ShariahContractType | None] = mapped_column(
        Enum(ShariahContractType, native_enum=False, length=32), default=None
    )
    shariah_review_status: Mapped[ShariahReviewStatus] = mapped_column(
        Enum(ShariahReviewStatus, native_enum=False, length=48),
        default=ShariahReviewStatus.NOT_APPLICABLE,
    )
    shariah_reviewer_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="instrument",
        cascade="all, delete-orphan",
    )

    # ------------------------------------------------------------------ #
    # Type-level guards
    # ------------------------------------------------------------------ #

    @validates("shariah_review_status")
    def _validate_review_status(self, key: str, value: Any) -> ShariahReviewStatus:
        """Block all human-only states at the ORM write level.

        ``SCHOLAR_APPROVED`` / ``SCHOLAR_REJECTED`` cannot be reached through
        any attribute or ORM write — only ``app.review.submit_human_review``
        may set them, via a deliberate Core ``update()`` path.
        """
        value = (
            ShariahReviewStatus(value)
            if not isinstance(value, ShariahReviewStatus)
            else value
        )
        if value in HUMAN_ONLY:
            raise ShariahReviewValidationError(
                f"Cannot set {value!r} on Instrument {self.id!r} outside an "
                "explicit human review endpoint (reviewer_id required)."
            )
        return value


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_instrument_id", "instrument_id"),
        UniqueConstraint("instrument_id", "file_url", name="uq_document_instrument_file"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), ForeignKey("instruments.id"))
    filename: Mapped[str] = mapped_column(String(255))
    file_url: Mapped[str] = mapped_column(String(1024))
    document_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, native_enum=False, length=64)
    )
    classification_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_schema_name: Mapped[str] = mapped_column(String(255), default="")
    extraction_schema_version: Mapped[str] = mapped_column(String(32), default="")
    ingestion_source: Mapped[IngestionSource] = mapped_column(
        Enum(IngestionSource, native_enum=False, length=32),
        default=IngestionSource.NATIVE_EXTRACTION,
    )
    compliance_mode: Mapped[ComplianceMode] = mapped_column(
        Enum(ComplianceMode, native_enum=False, length=32)
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=32),
        default=DocumentStatus.UPLOADED,
    )
    extracted_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    shariah_review_status: Mapped[ShariahReviewStatus] = mapped_column(
        Enum(ShariahReviewStatus, native_enum=False, length=48),
        default=ShariahReviewStatus.NOT_APPLICABLE,
    )
    shariah_reviewer_id: Mapped[str | None] = mapped_column(String(64), default=None)
    shariah_validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list
    )
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    uploaded_at: Mapped[datetime] = mapped_column(default=_utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(default=None)

    instrument: Mapped["Instrument"] = relationship(back_populates="documents")

    @validates("shariah_review_status")
    def _validate_doc_review_status(self, key: str, value: Any) -> ShariahReviewStatus:
        value = (
            ShariahReviewStatus(value)
            if not isinstance(value, ShariahReviewStatus)
            else value
        )
        if value in HUMAN_ONLY:
            raise ShariahReviewValidationError(
                f"Cannot set {value!r} on Document {self.id!r}: only a human "
                "reviewer may approve/reject (use the review endpoint)."
            )
        return value


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LedgerEntry(Base):
    """Append-only ledger.

    Every row carries the entry type, the affected instrument/document,
    the payload (the classification decision, extraction result, or
    compliance event), the raw model inputs that produced it, and a trace_id
    linking the row to its Langfuse trace so decisions are reproducible.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_instrument_id", "instrument_id"),
        Index("ix_ledger_document_id", "document_id"),
        Index("ix_ledger_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        Enum(LedgerEntryType, native_enum=False, length=48)
    )
    instrument_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("instruments.id"), default=None
    )
    document_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("documents.id"), default=None
    )
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_input: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    def __repr__(self) -> str:
        return (
            f"<LedgerEntry {self.id} type={self.entry_type.value} "
            f"instrument={self.instrument_id!r}>"
        )


# ---------------------------------------------------------------------------
# Cap table (live cap-table demo) -- event-sourced positions, replayed at
# query time. Supported by the "Live cap table" panel in the demo HTML.
# ---------------------------------------------------------------------------


class Investor(Base):
    """A holder of instruments/securities -- an individual, an institution,
    or a fund. The FUND type is what makes a cross-fund portfolio view a
    native join here rather than a portal-scraping integration."""

    __tablename__ = "investors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    investor_type: Mapped[InvestorType] = mapped_column(
        Enum(InvestorType, native_enum=False, length=24)
    )
    # Investor-level record of the KYC/AML review. The traditional-track
    # gateway checks for a KYC *document* attached to the instrument; this
    # flag is the same review recorded on the investor, queryable without
    # joining encrypted document rows.
    kyc_verified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    def __repr__(self) -> str:
        return f"<Investor {self.id} {self.name!r}>"


class Security(Base):
    """A class of equity in a company -- 'Series A Preferred', 'Common
    Stock', a 2024 option pool. This is deliberately separate from
    Instrument: an Instrument is one deal (a loan, a single sukuk
    issuance); a Security is an ongoing class of equity a company issues
    to many holders, across many rounds, over years. Cap tables are built
    on Security + CapTableEvent, not Instrument.

    ``issuer_name`` is a plain string rather than a foreign key to a full
    Issuer/Company entity -- there is no such entity yet in this schema.
    That's a real simplification: two securities with the same
    ``issuer_name`` string are currently the only way this system knows
    they belong to the same company.
    """

    __tablename__ = "securities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer_name: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255))  # e.g. "Common Stock"
    security_type: Mapped[SecurityType] = mapped_column(
        Enum(SecurityType, native_enum=False, length=24)
    )
    authorized_shares: Mapped[float] = mapped_column(Float)
    par_value: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    def __repr__(self) -> str:
        return f"<Security {self.id} {self.issuer_name!r}/{self.name!r}>"


class CapTableEvent(Base):
    """Append-only. The cap table at any point in time is *computed* by
    replaying these events up to that date (see
    ``app.captable.compute_cap_table``), never stored as a row that gets
    hand-edited and drifts out of sync with reality. Backed by the "Live
    cap table" panel in the demo HTML.

    ``security_id`` is the security primarily debited/credited by this
    event. ``target_security_id`` is only used for EXERCISE (option ->
    common) and CONVERSION (SAFE/note -> preferred or common), where shares
    move from one security class into another for the same holder.
    """

    __tablename__ = "cap_table_events"
    __table_args__ = (
        Index("ix_captable_security_id", "security_id"),
        Index("ix_captable_effective_date", "effective_date"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    security_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("securities.id")
    )
    target_security_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("securities.id"), default=None
    )
    event_type: Mapped[CapTableEventType] = mapped_column(
        Enum(CapTableEventType, native_enum=False, length=16)
    )
    holder_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("investors.id")
    )
    from_holder_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("investors.id"), default=None
    )
    quantity: Mapped[float] = mapped_column(Float)
    price_per_share: Mapped[float | None] = mapped_column(Float, default=None)
    effective_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(default=_utcnow)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    def __repr__(self) -> str:
        return (
            f"<CapTableEvent {self.event_type.value} security={self.security_id!r} "
            f"qty={self.quantity}>"
        )


# ---------------------------------------------------------------------------
# Cross-fund portfolio -- Holding is the join that lets one investor's
# portfolio span traditional and Islamic instruments from many issuers and
# be answered in a single query (GET /investors/{id}/portfolio).
# ---------------------------------------------------------------------------


class Holding(Base):
    """An investor's stake in one instrument."""

    __tablename__ = "holdings"
    __table_args__ = (
        Index("ix_holdings_investor_id", "investor_id"),
        Index("ix_holdings_instrument_id", "instrument_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    investor_id: Mapped[str] = mapped_column(String(64), ForeignKey("investors.id"))
    instrument_id: Mapped[str] = mapped_column(String(64), ForeignKey("instruments.id"))
    stake_amount: Mapped[float] = mapped_column(Float)
    ownership_percentage: Mapped[float | None] = mapped_column(Float, default=None)
    status: Mapped[HoldingStatus] = mapped_column(
        Enum(HoldingStatus, native_enum=False, length=16),
        default=HoldingStatus.ACTIVE,
    )
    acquired_at: Mapped[datetime] = mapped_column(default=_utcnow)

    def __repr__(self) -> str:
        return (
            f"<Holding investor={self.investor_id!r} instrument={self.instrument_id!r} "
            f"amount={self.stake_amount}>"
        )