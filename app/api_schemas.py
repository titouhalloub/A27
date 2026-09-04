"""API-layer request/response schemas -- separate from the extraction schemas in
app/schemas.py, which model document data, not HTTP payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import (
    ComplianceMode,
    DocumentType,
    ShariahContractType,
    ShariahReviewStatus,
    TransactionType,
)


class InstrumentCreate(BaseModel):
    transaction_type: TransactionType
    compliance_mode: ComplianceMode
    issuer_name: str
    issuer_type: str = Field(..., description="Corporate, SPV, Fund, Government")
    amount: float
    currency: str = Field(min_length=3, max_length=8)
    maturity_date: datetime | None = None


class InstrumentOut(BaseModel):
    id: str
    transaction_type: TransactionType
    compliance_mode: ComplianceMode
    issuer_name: str
    amount: float
    currency: str
    shariah_contract_type: ShariahContractType | None
    shariah_review_status: ShariahReviewStatus
    underlying_asset_description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentSubmit(BaseModel):
    """MVP intake: raw text, not a file upload -- OCR/file handling is a
    separate, already-built concern (app/ocr.py) this endpoint can grow into
    without changing its shape."""
    text: str = Field(..., min_length=1)
    filename: str = "document.txt"


class EvidenceDocumentSubmit(BaseModel):
    """For supporting/evidence documents (fatwas, KYC, side letters) that the
    compliance gateway needs *attached and typed*, not extracted -- there is
    no LoanExtraction-style schema for a fatwa, and there shouldn't be one.
    """
    text: str = Field(..., min_length=1)
    document_type: DocumentType
    filename: str = "evidence.txt"


class DocumentOut(BaseModel):
    id: str
    instrument_id: str
    filename: str
    document_type: DocumentType
    classification_confidence: float
    extraction_confidence: float
    extraction_schema_name: str | None = None
    ingestion_source: str
    status: str
    shariah_review_status: ShariahReviewStatus
    error_message: str | None
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class PipelineRunOut(BaseModel):
    document: DocumentOut
    instrument: InstrumentOut
    outcome: ShariahReviewStatus
    routed_to_review: bool


class HumanReviewRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=1)
    decision: str = Field(..., pattern="^(approved|rejected)$")
    notes: str = ""


class LedgerEntryOut(BaseModel):
    id: str
    entry_type: str
    instrument_id: str | None
    document_id: str | None
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ErrorOut(BaseModel):
    detail: str


# --------------------------------------------------------------------------- #
# Cap table schemas -- the live cap-table demo panel
# --------------------------------------------------------------------------- #


class InvestorCreate(BaseModel):
    name: str = Field(..., min_length=1)
    investor_type: str = Field("individual", pattern="^(individual|institution)$")


class InvestorOut(BaseModel):
    id: str
    name: str
    investor_type: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SecurityCreate(BaseModel):
    issuer_name: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    security_type: str = Field(
        ..., pattern="^(common|preferred|option|warrant|safe|convertible_note)$"
    )
    authorized_shares: float = Field(gt=0)
    par_value: float | None = Field(default=None, ge=0)


class SecurityOut(BaseModel):
    id: str
    issuer_name: str
    name: str
    security_type: str
    authorized_shares: float
    par_value: float | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CapTableEventCreate(BaseModel):
    security_id: str
    target_security_id: str | None = None
    event_type: str = Field(
        ..., pattern="^(issuance|transfer|cancellation|exercise|conversion)$"
    )
    holder_id: str | None = None
    from_holder_id: str | None = None
    quantity: float = Field(gt=0)
    price_per_share: float | None = Field(default=None, ge=0)
    effective_date: datetime
    notes: str | None = None


class CapTableEventOut(BaseModel):
    id: str
    security_id: str
    target_security_id: str | None
    event_type: str
    holder_id: str | None
    from_holder_id: str | None
    quantity: float
    price_per_share: float | None
    effective_date: datetime
    notes: str | None

    model_config = {"from_attributes": True}


class HolderPositionOut(BaseModel):
    holder_id: str
    holder_name: str
    security_id: str
    security_name: str
    shares: float
    # This POSITION's share of the fully-diluted total -- NOT the holder's
    # overall percentage. The holder's total across all their securities is
    # reported once, explicitly, in CapTableOut.ownership_by_holder.
    # Repeating the holder total on every row made clients double-count.
    ownership_percent: float


class CapTableOut(BaseModel):
    issuer_name: str
    as_of: datetime
    total_fully_diluted_shares: float
    shares_by_security: dict[str, float]
    ownership_by_holder: dict[str, float]
    positions: list[HolderPositionOut]