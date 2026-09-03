"""A27 API -- exposes the tested pipeline (classification, extraction,
configurable compliance, ledger) as a running service.

This is deliberately thin: every route is a wrapper around functions in
app.pipeline / app.review / app.compliance that already have their own test
coverage. The API layer's job is auth, request/response shaping, and error
handling -- not business logic.
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api_schemas import (
    DocumentOut,
    DocumentSubmit,
    ErrorOut,
    EvidenceDocumentSubmit,
    HumanReviewRequest,
    InstrumentCreate,
    InstrumentOut,
    LedgerEntryOut,
    PipelineRunOut,
)
from app.compliance import ComplianceGateway
from app.config import settings
from app.db import get_session, init_db
from app.models.enums import (
    DocumentStatus,
    IngestionSource,
    LedgerEntryType,
    ShariahReviewStatus,
)
from app.models.orm import Document, Instrument, LedgerEntry
from app.pipeline import process_document
from app.review import ShariahReviewError, submit_human_review_instrument


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="A27 API",
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