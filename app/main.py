"""Flowgate API -- exposes the tested pipeline (classification, extraction,
configurable compliance, ledger) as a running service.

This is deliberately thin: every route is a wrapper around functions in
app.pipeline / app.review / app.compliance that already have their own test
coverage. The API layer's job is auth, request/response shaping, and error
handling -- not business logic.
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api_schemas import (
    CapitalCallCreate,
    CapitalCallOut,
    CapitalCallReviewRequest,
    CapTableEventCreate,
    CapTableEventOut,
    CapTableOut,
    CapTableProposalOut,
    DocumentOut,
    DocumentSubmit,
    ErrorOut,
    EvidenceDocumentSubmit,
    HoldingCreate,
    HoldingOut,
    HolderPositionOut,
    HumanReviewRequest,
    InstrumentCreate,
    InstrumentOut,
    InvestorCreate,
    InvestorOut,
    InvestorPortfolioOut,
    LedgerEntryOut,
    PipelineRunOut,
    PipelineUploadOut,
    PortfolioHoldingOut,
    SecurityCreate,
    SecurityOut,
)
from app.captable import CapTableError, compute_cap_table
from app.compliance import ComplianceGateway
from app.config import settings
from app.db import get_session, init_db
from app.models.enums import (
    CapTableEventType,
    CapitalCallStatus,
    ComplianceMode,
    DocumentStatus,
    IngestionSource,
    InvestorType,
    LedgerEntryType,
    ProposalStatus,
    ProposalType,
    SecurityType,
    ShariahReviewStatus,
)
from app.models.orm import (
    CapTableEvent,
    CapTableProposal,
    CapitalCall,
    Document,
    Holding,
    Instrument,
    Investor,
    LedgerEntry,
    Security as SecurityModel,
)
from app.models.orm import _utcnow
from app.pipeline import process_document
from app.ocr import TextExtractionError, UnsupportedFileType, UploadTooLarge, text_from_upload
from app.review import ShariahReviewError, submit_human_review_instrument
from app.schemas import EquitySubscriptionExtraction


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Flowgate API",
    description="Unified private-capital pipeline: classification, extraction, "
    "configurable compliance (traditional + Islamic), ledger.",
    version="0.1.0",
    lifespan=_lifespan,
)

# Wide open for the MVP demo UI to call from a static page during pilot
# conversations. Tighten to real origins before any actual customer data
# flows through this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
def root() -> FileResponse:
    """Serve the live demo page at the root URL."""
    index = _static_dir / "index.html"
    return FileResponse(str(index))


# --------------------------------------------------------------------------- #
# Auth -- a real, if minimal, gate. Not full OAuth, but not a fully open
# unauthenticated endpoint either, now that this is meant for real prospects.
# --------------------------------------------------------------------------- #

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Security(_api_key_header)) -> str:
    if not settings.api_keys:
        # No keys configured: fail closed, not open. An empty allowlist
        # should never silently mean "anyone can call this."
        raise HTTPException(
            status_code=503,
            detail="No API keys configured on the server (A27_API_KEYS unset).",
        )
    valid_keys = {k.strip() for k in settings.api_keys.split(",") if k.strip()}
    if not key or not any(secrets.compare_digest(key, k) for k in valid_keys):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")
    return key


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #


@app.post(
    "/instruments",
    response_model=InstrumentOut,
    status_code=201,
    responses={401: {"model": ErrorOut}},
)
def create_instrument(
    payload: InstrumentCreate,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> Instrument:
    instrument = Instrument(
        id=str(uuid4()),
        transaction_type=payload.transaction_type,
        compliance_mode=payload.compliance_mode,
        issuer_name=payload.issuer_name,
        issuer_type=payload.issuer_type,
        amount=payload.amount,
        currency=payload.currency,
        maturity_date=payload.maturity_date,
        type_specific_data={},
    )
    session.add(instrument)
    session.commit()
    session.refresh(instrument)
    return instrument


@app.get(
    "/instruments/{instrument_id}",
    response_model=InstrumentOut,
    responses={404: {"model": ErrorOut}},
)
def get_instrument(
    instrument_id: str,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> Instrument:
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Instrument {instrument_id!r} not found")
    return instrument


@app.get(
    "/instruments/{instrument_id}/ledger",
    response_model=list[LedgerEntryOut],
    responses={404: {"model": ErrorOut}},
)
def get_instrument_ledger(
    instrument_id: str,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> list[LedgerEntry]:
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Instrument {instrument_id!r} not found")
    rows = session.execute(
        select(LedgerEntry)
        .where(LedgerEntry.instrument_id == instrument_id)
        .order_by(LedgerEntry.created_at)
    ).scalars().all()
    return list(rows)
# --------------------------------------------------------------------------- #
# Documents -- the actual pipeline entry point
# --------------------------------------------------------------------------- #


@app.post(
    "/instruments/{instrument_id}/documents",
    response_model=PipelineRunOut,
    responses={404: {"model": ErrorOut}},
)
def submit_document(
    instrument_id: str,
    payload: DocumentSubmit,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> PipelineRunOut:
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Instrument {instrument_id!r} not found")

    result = process_document(
        session, instrument, ComplianceGateway(), payload.text, filename=payload.filename
    )
    session.refresh(instrument)

    return PipelineRunOut(
        document=DocumentOut.model_validate(result.document),
        instrument=InstrumentOut.model_validate(instrument),
        outcome=result.outcome,
        routed_to_review=result.routed,
    )


@app.post(
    "/instruments/{instrument_id}/documents/upload",
    response_model=PipelineUploadOut,
    responses={
        401: {"model": ErrorOut},
        404: {"model": ErrorOut},
        400: {"model": ErrorOut},
        413: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def upload_document(
    instrument_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> PipelineUploadOut:
    """Real-document intake: PDF / TXT / MD / HTML / image upload. The
    file's embedded text layer is read first; scans fall back to Tesseract
    OCR (the accepted MVP trade-off, stated in app.ocr). Everything
    downstream -- classification, extraction, compliance, ledger -- is the
    same pipeline the text endpoint feeds. Unparseable documents fail loudly
    (422) with the reason; the pipeline never guesses."""
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Instrument {instrument_id!r} not found")

    page_meta: dict = {}
    try:
        text = text_from_upload(file, meta=page_meta)
    except UnsupportedFileType as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except TextExtractionError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{exc} The document could not be parsed, so nothing was "
            "classified or extracted -- no guessing.",
        ) from exc

    # Persist the original so file_url in the DB points at a real file the
    # reviewer (or a developer tuning the extractors) can open later.
    stored_url: str | None = None
    try:
        upload_dir = Path(settings.uploads_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file.file.seek(0)
        raw = file.file.read()
        file.file.seek(0)
        safe_name = f"{uuid4().hex}_{(file.filename or 'upload').replace('/', '_').replace(chr(92), '_')}"
        (upload_dir / safe_name).write_bytes(raw)
        stored_url = str(upload_dir / safe_name)
    except OSError:
        stored_url = None  # storage failure must not block ingestion

    result = process_document(
        session, instrument, ComplianceGateway(), text, filename=file.filename or "upload",
        file_url=stored_url,
    )
    session.refresh(instrument)

    return PipelineUploadOut(
        document=DocumentOut.model_validate(result.document),
        instrument=InstrumentOut.model_validate(instrument),
        outcome=result.outcome,
        routed_to_review=result.routed,
        extracted_text=text,
        pages_read=page_meta.get("pages_read"),
        pages_total=page_meta.get("pages_total"),
    )


@app.post(
    "/instruments/{instrument_id}/evidence",
    response_model=DocumentOut,
    status_code=201,
    responses={404: {"model": ErrorOut}},
)
def submit_evidence_document(
    instrument_id: str,
    payload: EvidenceDocumentSubmit,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> Document:
    """Attach a supporting document (fatwa, KYC, side letter) that the
    compliance gateway needs to see as evidence, without running it through
    extraction -- there is no structured schema to extract a fatwa into, and
    forcing one through the same pipeline as loan/sukuk documents would
    either misclassify it or route it to review for the wrong reason.
    """
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Instrument {instrument_id!r} not found")

    document = Document(
        id=str(uuid4()),
        instrument_id=instrument.id,
        filename=payload.filename,
        file_url="mem://" + uuid4().hex,
        document_type=payload.document_type,
        classification_confidence=1.0,  # explicitly typed by the caller, not classified
        ingestion_source=IngestionSource.MANUAL_ENTRY,
        compliance_mode=instrument.compliance_mode,
        status=DocumentStatus.PROCESSED,
        extracted_data={"raw_text": payload.text},
    )
    session.add(document)
    session.add(
        LedgerEntry(
            id=str(uuid4()),
            entry_type=LedgerEntryType.DOCUMENT_RESULT,
            instrument_id=instrument.id,
            document_id=document.id,
            payload={"stage": "evidence_attached", "document_type": payload.document_type.value},
        )
    )
    session.commit()
    session.refresh(document)
    return document


# --------------------------------------------------------------------------- #
# Human review -- the only path to SCHOLAR_APPROVED / SCHOLAR_REJECTED
# --------------------------------------------------------------------------- #


@app.post(
    "/instruments/{instrument_id}/review",
    response_model=InstrumentOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
def review_instrument(
    instrument_id: str,
    payload: HumanReviewRequest,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> Instrument:
    try:
        return submit_human_review_instrument(
            session,
            instrument_id,
            reviewer_id=payload.reviewer_id,
            decision=payload.decision,  # type: ignore[arg-type]
            notes=payload.notes,
        )
    except ShariahReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
# --------------------------------------------------------------------------- #
# Cap table -- the live cap-table demo panel
# --------------------------------------------------------------------------- #


@app.post(
    "/investors",
    response_model=InvestorOut,
    status_code=201,
    responses={401: {"model": ErrorOut}},
)
def create_investor(
    payload: InvestorCreate,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> Investor:
    investor = Investor(
        id=str(uuid4()),
        name=payload.name,
        investor_type=InvestorType(payload.investor_type),
        kyc_verified=False,
    )
    session.add(investor)
    session.commit()
    session.refresh(investor)
    return investor


@app.get(
    "/investors",
    response_model=list[InvestorOut],
    responses={401: {"model": ErrorOut}},
)
def list_investors(
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> list[Investor]:
    """Picker list for the proposal review UI: when a proposal's subscriber
    is unresolved, the reviewer links one of *these* known investors to it --
    the holder on a cap-table event must always be a real Investor row."""
    return list(
        session.execute(select(Investor).order_by(Investor.name)).scalars().all()
    )


# --------------------------------------------------------------------------- #
# Cross-fund portfolio -- one investor's holdings across many funds and both
# compliance tracks, unified in a single response.
# --------------------------------------------------------------------------- #


@app.post(
    "/instruments/{instrument_id}/holdings",
    response_model=HoldingOut,
    status_code=201,
    responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
def create_holding(
    instrument_id: str,
    payload: HoldingCreate,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> Holding:
    """Record an investor's stake in an instrument. Both sides must exist --
    no silent dangling references into the portfolio."""
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(
            status_code=404, detail=f"Instrument {instrument_id!r} not found"
        )
    investor = session.get(Investor, payload.investor_id)
    if investor is None:
        raise HTTPException(
            status_code=404, detail=f"Investor {payload.investor_id!r} not found"
        )

    holding = Holding(
        id=str(uuid4()),
        investor_id=investor.id,
        instrument_id=instrument.id,
        stake_amount=payload.stake_amount,
        ownership_percentage=payload.ownership_percentage,
    )
    session.add(holding)
    session.commit()
    session.refresh(holding)
    return holding


@app.get(
    "/investors/{investor_id}/portfolio",
    response_model=InvestorPortfolioOut,
    responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
def get_investor_portfolio(
    investor_id: str,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> InvestorPortfolioOut:
    """The cross-fund view: every holding an investor has, across funds and
    across both compliance tracks, with per-track exposure totals -- the same
    unification thesis as the compliance gateway, on the investor side."""
    investor = session.get(Investor, investor_id)
    if investor is None:
        raise HTTPException(
            status_code=404, detail=f"Investor {investor_id!r} not found"
        )

    holding_rows = session.execute(
        select(Holding).where(Holding.investor_id == investor_id)
    ).scalars().all()

    holdings_out: list[PortfolioHoldingOut] = []
    traditional_total = 0.0
    islamic_total = 0.0
    seen_instruments: set[str] = set()
    for holding in holding_rows:
        instrument = session.get(Instrument, holding.instrument_id)
        if instrument is None:  # pragma: no cover -- FK-guarded at write time
            continue
        seen_instruments.add(instrument.id)
        if instrument.compliance_mode == ComplianceMode.TRADITIONAL:
            traditional_total += holding.stake_amount
        elif instrument.compliance_mode == ComplianceMode.ISLAMIC:
            islamic_total += holding.stake_amount
        holdings_out.append(
            PortfolioHoldingOut(
                holding=HoldingOut.model_validate(holding),
                instrument=InstrumentOut.model_validate(instrument),
            )
        )

    return InvestorPortfolioOut(
        investor=InvestorOut.model_validate(investor),
        holdings=holdings_out,
        total_traditional_exposure=round(traditional_total, 2),
        total_islamic_exposure=round(islamic_total, 2),
        fund_count=len(seen_instruments),
    )


@app.post(
    "/securities",
    response_model=SecurityOut,
    status_code=201,
    responses={401: {"model": ErrorOut}},
)
def create_security(
    payload: SecurityCreate,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> SecurityModel:
    security = SecurityModel(
        id=str(uuid4()),
        issuer_name=payload.issuer_name,
        name=payload.name,
        security_type=payload.security_type,
        authorized_shares=payload.authorized_shares,
        par_value=payload.par_value,
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


@app.post(
    "/cap-table-events",
    response_model=CapTableEventOut,
    status_code=201,
    responses={
        401: {"model": ErrorOut},
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
    },
)
def record_cap_table_event(
    payload: CapTableEventCreate,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapTableEvent:
    # The event must reference securities (and, for exercise/conversion,
    # the target security) and holders that actually exist -- no silent
    # dangling references into the event log.
    security = session.get(SecurityModel, payload.security_id)
    if security is None:
        raise HTTPException(
            status_code=404, detail=f"Security {payload.security_id!r} not found"
        )
    if payload.target_security_id is not None:
        target = session.get(SecurityModel, payload.target_security_id)
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"Target security {payload.target_security_id!r} not found",
            )
    for holder_id in (payload.holder_id, payload.from_holder_id):
        if holder_id is None:
            continue
        if session.get(Investor, holder_id) is None:
            raise HTTPException(
                status_code=400, detail=f"Investor {holder_id!r} not found"
            )

    event = CapTableEvent(
        id=str(uuid4()),
        security_id=payload.security_id,
        target_security_id=payload.target_security_id,
        event_type=payload.event_type,
        holder_id=payload.holder_id,
        from_holder_id=payload.from_holder_id,
        quantity=payload.quantity,
        price_per_share=payload.price_per_share,
        effective_date=payload.effective_date,
        notes=payload.notes,
    )
    session.add(event)
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- surfaced as a real 400, not a 500
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.refresh(event)

    # Validate the whole log is still consistent -- catches an overdraft
    # transfer/cancellation/exercise at write time (400 + the offending
    # event rolled back), not silently at the next unrelated read.
    try:
        compute_cap_table(session, security.issuer_name)
    except CapTableError as exc:
        session.delete(event)
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return event


@app.get(
    "/cap-table/{issuer_name}",
    response_model=CapTableOut,
    responses={401: {"model": ErrorOut}, 400: {"model": ErrorOut}},
)
def get_cap_table(
    issuer_name: str,
    as_of: datetime | None = None,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapTableOut:
    try:
        snapshot = compute_cap_table(session, issuer_name, as_of=as_of)
    except CapTableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    holder_ids = {p.holder_id for p in snapshot.positions}
    security_ids = {p.security_id for p in snapshot.positions}
    holders = {
        h.id: h.name
        for h in session.execute(
            select(Investor).where(Investor.id.in_(holder_ids))
        ).scalars()
    } if holder_ids else {}
    securities = {
        s.id: s.name
        for s in session.execute(
            select(SecurityModel).where(SecurityModel.id.in_(security_ids))
        ).scalars()
    } if security_ids else {}

    ownership = snapshot.ownership_by_holder()
    total = snapshot.total_fully_diluted_shares
    positions_out = [
        HolderPositionOut(
            holder_id=p.holder_id,
            holder_name=holders.get(p.holder_id, "unknown"),
            security_id=p.security_id,
            security_name=securities.get(p.security_id, "unknown"),
            shares=p.shares,
            # Per-position share of the fully-diluted total. The holder's
            # overall percentage is ownership_by_holder -- repeating it on
            # every row invited clients to double-count a holder with
            # shares in more than one security class (a real bug the demo
            # JS hit).
            ownership_percent=(
                round(p.shares / total * 100, 4) if total > 0 else 0.0
            ),
        )
        for p in snapshot.positions
    ]

    return CapTableOut(
        issuer_name=snapshot.issuer_name,
        as_of=snapshot.as_of,
        total_fully_diluted_shares=total,
        shares_by_security=snapshot.shares_by_security,
        ownership_by_holder=ownership,
        positions=positions_out,
    )


# ---------------------------------------------------------------------------
# Document -> cap table proposal flow
#
# The pipeline classifies, extracts, and writes the deal container, but the
# cap table is a *ledger of ownership facts*, and per the no-guessing
# guarantee the system never writes those on its own authority. What it CAN
# do on its own is prepare the exact write for a human: a proposed issuance
# with every number filled from an extraction that already cleared the
# confidence gates, never invented. Approval is one endpoint, one click --
# and the reviewer sees the real numbers, not a blank form to re-type.
#
# Investor auto-provisioning follows the same rule: when the *document itself*
# names the subscriber in its own preamble convention ("X (the 'Subscriber')
# hereby subscribes..."), creating a PROVISIONAL investor with that exact
# name is transcription, not a guess. Signature-block-only names are never
# auto-provisioned -- they stay None and the proposal carries a
# subscriber_unresolved flag for the reviewer.
# ---------------------------------------------------------------------------


def _provisional_investor(
    session: Session, name: str
) -> tuple[Investor, bool]:
    """Return the investor with exactly this name, creating a PROVISIONAL
    one when absent. The bool is True only for a freshly created row, so the
    audit trail distinguishes transcription from reuse."""
    existing = session.execute(
        select(Investor).where(Investor.name == name)
    ).scalars().first()
    if existing is not None:
        return existing, False
    investor = Investor(
        id=str(uuid4()),
        name=name,
        investor_type=InvestorType.INDIVIDUAL,
    )
    session.add(investor)
    session.flush()  # id available for the event payload below
    return investor, True


def _provisional_security(
    session: Session, issuer_name: str, security_type: str | None,
    share_count: float | None,
) -> tuple[SecurityModel, bool]:
    """Return the issuer's security class, creating a placeholder when absent.

    A class with the same name may already exist; otherwise the extraction's
    security_type text ('Series A Preferred stock') becomes the class name.
    authorized_shares is set from the document's stated share count ONLY --
    never padded with a guessed round number."""
    name = (security_type or "common").strip() or "common"
    existing = session.execute(
        select(SecurityModel).where(
            SecurityModel.issuer_name == issuer_name,
            SecurityModel.name == name,
        )
    ).scalars().first()
    if existing is not None:
        return existing, False
    security = SecurityModel(
        id=str(uuid4()),
        issuer_name=issuer_name,
        name=name,
        security_type=SecurityType.PREFERRED
        if "preferred" in name.lower() else SecurityType.COMMON,
        authorized_shares=share_count,
    )
    session.add(security)
    session.flush()
    return security, True


@app.post(
    "/documents/{document_id}/cap-table-proposal",
    response_model=CapTableProposalOut,
    status_code=201,
    responses={
        401: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
    },
)
def create_cap_table_proposal(
    document_id: str,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapTableProposal:
    """Prepare a cap-table issuance proposal from a processed document.

    The document must have passed both confidence gates (classification >=
    0.75, extraction >= 0.85) -- a reviewed/unprocessed document carries
    numbers nobody validated, so it can never seed a proposal. The proposal
    itself is inert: no Security, Investor, or CapTableEvent rows exist until
    a human approves (see the approval endpoint below)."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=404, detail=f"Document {document_id!r} not found"
        )
    if doc.status != DocumentStatus.PROCESSED:
        raise HTTPException(
            status_code=409,
            detail=f"Document {document_id!r} is {doc.status.value}, not "
            "processed -- only a document that cleared both confidence "
            "gates can seed a cap-table proposal",
        )

    data = (doc.extracted_data or {}).get("data", {})
    parsed = EquitySubscriptionExtraction.model_validate(data)
    if not parsed.company_name or parsed.share_count is None:
        raise HTTPException(
            status_code=409,
            detail="Extraction lacks the fields a cap-table event requires "
            "(company_name / share_count) -- route to human review instead",
        )

    investor_id: str | None = None
    provisional_investor_created = False
    if parsed.subscriber_name:
        investor, provisional_investor_created = _provisional_investor(
            session, parsed.subscriber_name
        )
        investor_id = investor.id

    payload = {
        "document_id": doc.id,
        "instrument_id": doc.instrument_id,
        "issuer_name": parsed.company_name,
        "security_name": parsed.security_type,
        "holder_id": investor_id,
        "holder_name": parsed.subscriber_name,
        "share_count": parsed.share_count,
        "price_per_share": parsed.subscription_price_per_share
        or parsed.price_per_unit,
        "amount": parsed.investment_amount or parsed.total_offering_amount,
        "currency": parsed.currency,
        "document_date": (
            parsed.document_date.isoformat() if parsed.document_date else None
        ),
        "subscriber_unresolved": investor_id is None,
        "provisional_investor_created": provisional_investor_created,
    }
    proposal = CapTableProposal(
        id=str(uuid4()),
        document_id=doc.id,
        instrument_id=doc.instrument_id,
        proposal_type=ProposalType.CAP_TABLE_PROPOSAL,
        status=ProposalStatus.PROPOSED,
        payload=payload,
    )
    session.add(proposal)
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- surfaced as a real 409, not a 500
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.refresh(proposal)
    return proposal


@app.post(
    "/cap-table-proposals/{proposal_id}/approve",
    response_model=CapTableEventOut,
    responses={
        401: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
    },
)
def approve_cap_table_proposal(
    proposal_id: str,
    reviewer: str,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapTableEvent:
    """The human gate. Materializes the proposed issuance as a real
    CapTableEvent -- the ONLY path from an extracted document into the
    cap table, matching how CapitalCall approval already works."""
    proposal = session.get(CapTableProposal, proposal_id)
    if proposal is None:
        raise HTTPException(
            status_code=404, detail=f"Proposal {proposal_id!r} not found"
        )
    if proposal.status != ProposalStatus.PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"Proposal {proposal_id!r} is {proposal.status.value}, "
            "not proposed -- a proposal is approved at most once",
        )
    if not reviewer or not reviewer.strip():
        raise HTTPException(status_code=409, detail="A named reviewer is required")
    payload = proposal.payload or {}
    if payload.get("subscriber_unresolved"):
        raise HTTPException(
            status_code=409,
            detail="The extraction never identified the subscriber -- "
            "link a real investor (or edit the proposal) before approving",
        )

    issuer_name = payload.get("issuer_name") or ""
    holder_id = payload.get("holder_id") or ""
    holder = session.get(Investor, holder_id)
    if holder is None:
        raise HTTPException(
            status_code=409,
            detail=f"Investor {holder_id!r} no longer exists -- create it "
            "and resubmit the proposal",
        )

    security, security_created = _provisional_security(
        session, issuer_name, payload.get("security_name"),
        payload.get("share_count"),
    )
    # document_date is stored in the payload as an ISO string; the event
    # column is a real DateTime, so parse it back (never store a string in a
    # datetime column -- SQLite would fail to coerce it on later reads).
    raw_date = payload.get("document_date")
    effective = datetime.fromisoformat(raw_date) if raw_date else _utcnow()
    event = CapTableEvent(
        id=str(uuid4()),
        security_id=security.id,
        event_type=CapTableEventType.ISSUANCE,
        holder_id=holder.id,
        quantity=payload.get("share_count"),
        price_per_share=payload.get("price_per_share"),
        effective_date=effective,
        notes=f"From document {proposal.document_id}, approved by {reviewer.strip()}",
    )
    session.add(event)
    proposal.status = ProposalStatus.APPROVED
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- surfaced as a real 409, not a 500
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.refresh(event)

    # Write-time consistency check, exactly like the manual event endpoint:
    # a proposal can never push the log into an inconsistent state.
    try:
        compute_cap_table(session, issuer_name)
    except CapTableError as exc:
        session.delete(event)
        if security_created:
            session.delete(security)
        proposal.status = ProposalStatus.PROPOSED
        session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    session.add(LedgerEntry(
        id=str(uuid4()),
        entry_type=LedgerEntryType.CAP_TABLE_EVENT,
        instrument_id=proposal.instrument_id,
        document_id=proposal.document_id,
        payload={
            "event": "cap_table_proposal_approved",
            "proposal_id": proposal.id,
            "cap_table_event_id": event.id,
            "security_id": security.id,
            "holder_id": holder.id,
            "share_count": payload.get("share_count"),
            "reviewer": reviewer.strip(),
        },
    ))
    session.commit()
    return event


@app.post(
    "/cap-table-proposals/{proposal_id}/link-investor",
    response_model=CapTableProposalOut,
    responses={
        401: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
    },
)
def link_investor_to_proposal(
    proposal_id: str,
    investor_id: str,
    reviewer: str = "",
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapTableProposal:
    """Resolve an unresolved subscriber: attach a REAL investor to a pending
    proposal. This is the remedy the approval gate's 409 message promises --
    extraction can transcribe a subscriber the document names, but when the
    document only carries a signature block, the human decides *who* this
    position belongs to, and the cap table only ever issues to an investor
    row that exists. Only a PROPOSED (undecided) proposal can be edited;
    the link (and who did it) lands in the audit ledger."""
    proposal = session.get(CapTableProposal, proposal_id)
    if proposal is None:
        raise HTTPException(
            status_code=404, detail=f"Proposal {proposal_id!r} not found"
        )
    if proposal.status != ProposalStatus.PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"Proposal {proposal_id!r} is {proposal.status.value}, "
            "not proposed -- it was already decided",
        )
    investor = session.get(Investor, investor_id)
    if investor is None:
        raise HTTPException(
            status_code=404, detail=f"Investor {investor_id!r} not found"
        )

    payload = dict(proposal.payload or {})
    previous_name = payload.get("holder_name")
    payload["holder_id"] = investor.id
    payload["holder_name"] = investor.name
    payload["subscriber_unresolved"] = False
    payload["investor_linked"] = {
        "investor_id": investor.id,
        "investor_name": investor.name,
        "previous_holder_name": previous_name,
        "reviewer": reviewer.strip() or None,
    }
    # Reassign the whole dict: in-place mutation of a JSON column is not
    # detected by SQLAlchemy's change tracking.
    proposal.payload = payload
    session.add(LedgerEntry(
        id=str(uuid4()),
        entry_type=LedgerEntryType.CAP_TABLE_PROPOSAL,
        instrument_id=proposal.instrument_id,
        document_id=proposal.document_id,
        payload={
            "event": "cap_table_proposal_investor_linked",
            "proposal_id": proposal.id,
            "investor_id": investor.id,
            "investor_name": investor.name,
            "previous_holder_name": previous_name,
            "reviewer": reviewer.strip() or None,
        },
    ))
    session.commit()
    session.refresh(proposal)
    return proposal


@app.get(
    "/cap-table-proposals",
    response_model=list[CapTableProposalOut],
    responses={401: {"model": ErrorOut}, 400: {"model": ErrorOut}},
)
def list_cap_table_proposals(
    status: str | None = None,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> list[CapTableProposal]:
    """Review queue for the human approval gate (newest first).

    ``?status=proposed`` shows only the pending decisions; omit it to see
    recent approvals/rejections as well. This is what the UI's proposals
    panel renders -- a proposal only ever becomes shares when a named human
    approves it, so this queue is the single place to see what awaits."""
    stmt = select(CapTableProposal)
    if status:
        try:
            status_enum = ProposalStatus(status.strip().lower())
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown status {status!r} "
                "(use proposed, approved, or rejected)",
            ) from exc
        stmt = stmt.where(CapTableProposal.status == status_enum)
    stmt = stmt.order_by(CapTableProposal.created_at.desc()).limit(100)
    return list(session.execute(stmt).scalars().all())


@app.post(
    "/cap-table-proposals/{proposal_id}/reject",
    response_model=CapTableProposalOut,
    responses={
        401: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
    },
)
def reject_cap_table_proposal(
    proposal_id: str,
    reviewer: str,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapTableProposal:
    """The other half of the human gate. A named reviewer rejects the
    proposal: nothing is written to the cap table, the proposal is marked
    REJECTED, and the decision lands in the audit ledger. A rejected
    proposal can never be approved afterwards -- the path forward is a new
    proposal from a corrected document."""
    proposal = session.get(CapTableProposal, proposal_id)
    if proposal is None:
        raise HTTPException(
            status_code=404, detail=f"Proposal {proposal_id!r} not found"
        )
    if proposal.status != ProposalStatus.PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"Proposal {proposal_id!r} is {proposal.status.value}, "
            "not proposed -- it was already decided",
        )
    if not reviewer or not reviewer.strip():
        raise HTTPException(status_code=409, detail="A named reviewer is required")
    proposal.status = ProposalStatus.REJECTED
    session.add(LedgerEntry(
        id=str(uuid4()),
        entry_type=LedgerEntryType.CAP_TABLE_PROPOSAL,
        instrument_id=proposal.instrument_id,
        document_id=proposal.document_id,
        payload={
            "event": "cap_table_proposal_rejected",
            "proposal_id": proposal.id,
            "reviewer": reviewer.strip(),
        },
    ))
    session.commit()
    session.refresh(proposal)
    return proposal


# --------------------------------------------------------------------------- #
# Capital calls -- approval parity (Phase A)
#
# Mirrors the cap-table proposal gate: an extracted call is PENDING_APPROVAL
# until a named human reviewer approves it. funder_id is resolved against the
# Investor registry at creation; a call whose funder could not be resolved
# (no Investor row / no name) is flagged for manual review and approval of an
# unresolved call blocks with 409. Rejects never write event facts.
# --------------------------------------------------------------------------- #


capital_call_out_schema = CapitalCallOut


@app.post(
    "/instruments/{instrument_id}/capital-calls",
    response_model=capital_call_out_schema,
    status_code=201,
    responses={401: {"model": ErrorOut}},
)
def create_capital_call(
    instrument_id: str,
    payload: CapitalCallCreate,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapitalCall:
    """Create a capital call notice (extracted data) pending approval.

    The funder_name is matched against the Investor registry by name; if found,
    the funder_id is resolved at creation time. If not found, funder_id stays
    NULL and the call is flagged ``funder_unresolved`` -- the reviewer is told
    to create the investor before approving (same semantics as the
    ``subscriber_unresolved`` flag on cap-table proposals).
    """
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Instrument {instrument_id!r} not found")

    funder_id: str | None = None
    funder_name: str | None = payload.funder_name
    if funder_name:
        resolved = session.execute(
            select(Investor).where(Investor.name == funder_name)
        ).scalars().first()
        if resolved is not None:
            funder_id = resolved.id

    call = CapitalCall(
        id=str(uuid4()),
        instrument_id=instrument_id,
        funder_id=funder_id,
        capital_owing=payload.capital_owing,
        currency=payload.currency[:3],
        amount_due=payload.capital_owing,
        due_date=payload.due_date,
        wire_details=payload.wire_details,
        source_text=payload.source_text or "",
        status=CapitalCallStatus.PENDING_APPROVAL,
        requires_manual_review=funder_id is None,
    )
    session.add(call)
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.refresh(call)
    return call


@app.post(
    "/capital-calls/{call_id}/review",
    response_model=CapitalCallOut,
    responses={
        401: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
    },
)
def review_capital_call(
    call_id: str,
    payload: CapitalCallReviewRequest,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> CapitalCall:
    """Named-reviewer approve/reject gate for a capital call.

    Mirrors the cap-table proposal gate: a call is PENDING_APPROVAL until
    a human reviewer confirms it. Rejects write a ledger entry but no cap-table
    event -- capital calls track cash obligations, not ownership facts.

    A fully unresolved funder (no Investor row at all) blocks approval with
    409. A PROVISIONAL investor (created by the system from a name-match) is
    linkable -- the reviewer sees the flag and decides.
    """
    call = session.get(CapitalCall, call_id)
    if call is None:
        raise HTTPException(
            status_code=404, detail=f"Capital call {call_id!r} not found"
        )
    if call.status != CapitalCallStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Capital call {call_id!r} is {call.status.value}, "
                "not pending_approval -- a call is reviewed at most once"
            ),
        )
    reviewer = payload.reviewer.strip()
    if not reviewer:
        raise HTTPException(status_code=409, detail="A named reviewer is required")

    if payload.action == "approve":
        if call.funder_id is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The capital call has no resolved funder (funder_id is NULL) -- "
                    "create the investor and re-link before approving"
                ),
            )
        call.status = CapitalCallStatus.APPROVED
        event_type = LedgerEntryType.CAPITAL_CALL_REVIEW
        event_detail = "capital_call_approved"
    else:
        call.status = CapitalCallStatus.REJECTED
        event_type = LedgerEntryType.CAPITAL_CALL_REVIEW
        event_detail = "capital_call_rejected"

    session.add(
        LedgerEntry(
            id=str(uuid4()),
            entry_type=event_type,
            instrument_id=call.instrument_id,
            payload={
                "event": event_detail,
                "capital_call_id": call.id,
                "funder_id": call.funder_id,
                "capital_owing": call.capital_owing,
                "reviewer": reviewer,
                "action": payload.action,
            },
        )
    )
    session.commit()
    session.refresh(call)
    return call


@app.get(
    "/capital-calls",
    response_model=list[CapitalCallOut],
    responses={401: {"model": ErrorOut}},
)
def list_capital_calls(
    status_filter: CapitalCallStatus | None = Query(default=None, alias="status"),
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> list[CapitalCall]:
    """Review queue for capital calls.

    ``?status=`` filters to a single lifecycle state (e.g. ``pending_approval``
    for the call queue). Omit the filter to see all calls across all states.
    """
    stmt = select(CapitalCall)
    if status_filter is not None:
        stmt = stmt.where(CapitalCall.status == status_filter)
    stmt = stmt.order_by(CapitalCall.created_at.desc())
    rows = session.execute(stmt).scalars().all()
    return list(rows)


@app.get(
    "/capital-calls/overdue",
    response_model=list[CapitalCallOut],
    responses={401: {"model": ErrorOut}},
)
def list_overdue_capital_calls(
    as_of: datetime | None = None,
    session: Session = Depends(get_session),
    _: str = Depends(require_api_key),
) -> list[CapitalCall]:
    """Calls whose ``due_date`` has passed and that are still ``PENDING_APPROVAL``.

    ``?as_of=`` lets the caller evaluate overdue-ness against an arbitrary point
    in time (useful for backfill / reconciliation). Defaults to *now*.
    """
    cutoff = as_of or datetime.now(timezone.utc)
    rows = session.execute(
        select(CapitalCall)
        .where(CapitalCall.due_date < cutoff)
        .where(CapitalCall.status == CapitalCallStatus.PENDING_APPROVAL)
        .order_by(CapitalCall.due_date.asc())
    ).scalars().all()
    return list(rows)
