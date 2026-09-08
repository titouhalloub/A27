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
    contract_type: ShariahContractType | None = Field(
        default=None,
        description="Murabaha/Ijara/Musharakah/Wakalah. None when unparseable, "
        "so the compliance gateway can flag it instead of masking a default.",
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


class CapitalCallExtraction(BaseModel):
    """Capital call notice extraction (v1).

    Extracted from subscription documents, capital call notices, and
    capital commitment confirmations. Drives the CapitalCall ingestion
    workflow — but the system never moves money on its own; an extracted
    call is PENDING_APPROVAL until a human reviewer confirms.
    """

    funder_name: str = Field(description="Name of the LP / fund being called")
    currency: str = Field(min_length=3, max_length=8, description="ISO 4217")
    capital_owing: float = Field(gt=0, description="Amount being called")
    due_date: date | None = None
    wire_details: str | None = None
    # The raw text block the amounts were parsed from — kept as the audit
    # trail of what the extractor actually read from this notice.
    source_text: str = Field(description="Parsed text window used for extraction")


class SubscriptionAgreementExtraction(BaseModel):
    """Subscription agreement extraction (v1).

    Extracted from investor subscription / subscription agreement / subscription
    commitment documents. Used by the CapitalCall ingestion workflow to match
    investor commitments against calls.
    """

    fund_name: str = Field(description="Name of the fund / vehicle")
    investor_name: str = Field(description="Name of the subscribing investor")
    commitment_amount: float = Field(gt=0, description="Total capital commitment")
    currency: str = Field(min_length=3, max_length=8, description="ISO 4217")
    payment_due_date: date | None = None
    payment_instructions: str | None = None
    investor_type: Literal["individual", "institutional", "family_office", "other"] = "other"
    source_text: str = Field(description="Parsed text window used for extraction")

    source_text: str = Field(description="Parsed text window used for extraction")


class EquitySubscriptionExtraction(BaseModel):
    """US LLC / Corp equity subscription agreement.

    Also covers SEC-form style exhibits (Regulation D/Crowdfunding
    subscription agreements): the issuer is often named in a preamble like
    'Investview, Inc. (the "Company")', the offering size appears as a
    'maximum offering of $5,000,000', the subscriber count as
    'authorized for sale 100,000 shares', and the per-investor price as a
    'cash purchase price of $5,000,000'. A blank Category A-H accreditation
    form stays None -- only an explicit mark counts.
    """

    schema_name: Literal["EquitySubscriptionExtraction"] = (
        "EquitySubscriptionExtraction")
    schema_version: Literal["v1"] = "v1"
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    company_name: str | None = None
    state_of_incorporation: str | None = None
    security_type: str | None = None
    price_per_unit: float | None = None
    total_offering_amount: float | None = None
    minimum_investment: float | None = None
    currency: str | None = None
    # --- SEC-form / subscription-specific fields -------------------------
    share_count: int | None = None
    subscription_price_per_share: float | None = None
    investment_amount: float | None = None
    accredited_investor_category: str | None = None
    document_date: date | None = None
    source_text: str | None = None


def extract_result_to_document_data(
    extraction: BaseModel, schema_name: str
) -> dict[str, Any]:
    """Serialise a typed extraction result into the ``Document.extracted_data``
    envelope (spec gap #2): the Pydantic object is ``model_dump()``'d and the
    schema name + version are stored alongside the payload, so the JSON field
    is never an implicit, unversioned bag.

    ``mode="json"`` keeps the payload JSON-serialisable for the SQLAlchemy
    JSON column (datetimes -> ISO strings, str-Enums -> their values, which
    still compare equal to the enum).
    """
    return {
        "schema_name": schema_name,
        "schema_version": SCHEMA_VERSION,
        "data": extraction.model_dump(mode="json"),
    }


# The docstring above historically named this ``extraction_result_to_data()``;
# keep the alias so both names resolve.
extraction_result_to_data = extract_result_to_document_data

