"""Per-asset-class Pydantic extraction schemas, routed by document type.

Phase 4 ships the Loan pipeline end to end. The remaining schemas (Sukuk,
Equity, Real Asset, Fund Interest) are defined now so the routing table covers
every document type today — their extractor functions land in Phase 6; the
schema shapes are the contract.

Every schema maps into ``Document.extracted_data`` through the *explicit*
``extraction_result_to_data()`` function below (spec gap #2): the typed
Pydantic object is ``model_dump()``'d and the schema name + version are
stored alongside the payload, so the JSON field is never an implicit,
unversioned bag.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, Type

from pydantic import BaseModel, Field

from app.models.enums import DocumentType, ShariahContractType

SCHEMA_VERSION = "v1"


class LoanExtraction(BaseModel):
    """Loan agreement extraction schema (v1)."""

    issuer_name: str = Field(description="Legal name of the borrower/issuer")
    lender_name: str = Field(description="Legal name of the lender")
    principal_amount: float = Field(gt=0, description="Principal in the loan currency")
    currency: str = Field(min_length=3, max_length=8)
    interest_rate: float = Field(ge=0, le=0.5, description="Annual rate as decimal")
    maturity_date: date | None = None
    repayment_schedule: str | None = None
    governing_law: str | None = None
    covenants: list[str] = Field(default_factory=list)
    secured: bool = False
    collateral_description: str | None = None


class SukukExtraction(BaseModel):
    """Sukuk certificate / issuance (v1)."""

    issuer_name: str
    certificate_title: str | None = None
    total_size: float = Field(gt=0, description="Issuance size in base currency")
    currency: str = Field(min_length=3, max_length=8)
    contract_type: ShariahContractType = Field(
        description="Murabaha/Ijara/Musharakah/Wakalah"
    )
    profit_rate: float | None = Field(default=None, ge=0, le=0.5)
    rental_rate: float | None = Field(default=None, ge=0, le=0.5)
    asset_type: str | None = None
    asset_description: str | None = None
    fatwa_reference: str | None = None
    listing_exchange: str | None = None
    isin: str | None = None
    maturity_date: date | None = None
    periodic_distributions: list[str] = Field(default_factory=list)


class EquityExtraction(BaseModel):
    """Equity / shareholder agreement extraction (v1)."""

    company_name: str
    investor_name: str | None = None
    post_money_valuation: float | None = Field(default=None, ge=0)
    equity_percent: float | None = Field(default=None, ge=0, le=1)
    share_class: str | None = None
    vesting_terms: str | None = None
    board_seats: int = Field(default=0, ge=0)
    right_terms: list[str] = Field(default_factory=list)


class RealAssetExtraction(BaseModel):
    """Real asset / infrastructure extraction (v1)."""

    asset_name: str
    asset_type: str | None = None
    location: str | None = None
    purchase_price: float | None = Field(default=None, ge=0)
    current_value: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=8)
    lease_terms: str | None = None
    maintenance_obligations: str | None = None


class FundInterestExtraction(BaseModel):
    """Fund interest (LPA / PE / VC) extraction (v1)."""

    fund_name: str
    fund_type: Literal["private_equity", "venture_capital", "hedge", "other"] = "private_equity"
    partnership_terms: str | None = None
    capital_commitment: float | None = Field(default=None, ge=0)
    distribution_waterfall: str | None = None
    management_fee_bps: int | None = Field(default=None, ge=0, le=500)
    carried_interest: float | None = Field(default=None, ge=0, le=0.5)
    general_partner: str | None = None


# ---------------------------------------------------------------------------
# Routing table: document type -> extraction schema
# ---------------------------------------------------------------------------

SCHEMA_BY_DOCUMENT_TYPE: dict[DocumentType, Type[BaseModel] | None] = {
    DocumentType.LOAN_AGREEMENT: LoanExtraction,
    DocumentType.TERM_SHEET: LoanExtraction,
    DocumentType.SUKUK_CERTIFICATE: SukukExtraction,
    DocumentType.FATWA: None,  # evidence document: attached, not extracted
    DocumentType.SHA: EquityExtraction,
    DocumentType.PPM: EquityExtraction,
    DocumentType.LPA: FundInterestExtraction,
    DocumentType.SIDE_LETTER: EquityExtraction,
    DocumentType.SAFE: EquityExtraction,
    DocumentType.FINANCIAL_STATEMENT: RealAssetExtraction,
    DocumentType.KYC: None,  # encrypted PII, never auto-extracted
    DocumentType.OTHER: None,
    DocumentType.UNCLASSIFIED: None,  # human triage, never auto-extracted
}

EXTRACTION_ROUTE_NAMES: dict[DocumentType, str] = {
    DocumentType.LOAN_AGREEMENT: "LoanExtraction",
    DocumentType.TERM_SHEET: "LoanExtraction",
    DocumentType.SUKUK_CERTIFICATE: "SukukExtraction",
    DocumentType.FATWA: "SukukExtraction",
    DocumentType.SHA: "EquityExtraction",
    DocumentType.PPM: "EquityExtraction",
    DocumentType.LPA: "FundInterestExtraction",
    DocumentType.SIDE_LETTER: "EquityExtraction",
    DocumentType.SAFE: "EquityExtraction",
    DocumentType.FINANCIAL_STATEMENT: "RealAssetExtraction",
}


def extraction_schema_for(document_type: DocumentType) -> Type[BaseModel] | None:
    """Return the Pydantic schema routed to by a document type, or None."""
    return SCHEMA_BY_DOCUMENT_TYPE.get(document_type)


def extract_result_to_document_data(
    extraction: BaseModel,
    schema_name: str,
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    """Explicit, tested mapping from a typed extraction into the JSON field.

    Spec gap #2 fix: the transformation is never implicit. The Pydantic
    object is dumped, and the schema name + version are stored alongside the
    payload so old documents are never ambiguous to read back when schemas
    evolve.
    """
    if not isinstance(extraction, BaseModel):
        raise TypeError(
            f"extraction must be a Pydantic model, got {type(extraction).__name__}"
        )
    return {
        "schema_name": schema_name,
        "schema_version": schema_version,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "data": extraction.model_dump(),
    }