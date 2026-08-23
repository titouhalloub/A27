"""Human review service — the *only* path to SCHOLAR_APPROVED/SCHOLAR_REJECTED.

The ORM ``@validates`` guards on Instrument.shariah_review_status and
Document.shariah_review_status make it impossible to set a human-only state
through any normal ORM/attribute write. This module is the single deliberate
escape hatch: it writes those states through SQLAlchemy Core ``update()``
statements (which do not invoke ORM attribute validation), requires an
explicit ``reviewer_id``, and writes a permanent audit entry to the ledger.
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.enums import LedgerEntryType, ShariahReviewStatus
from app.models.orm import Document, Instrument, LedgerEntry

HumanDecision = Literal["approved", "rejected"]


class ShariahReviewError(ValueError):
    """Raised when a review transition is not permitted."""


def _verify_decision_allowed(
    current: ShariahReviewStatus, decision: HumanDecision
) -> None:
    """A human may only review records the system actually routed for review.

    No reviewer should ever be able to approve a record the system never
    routed through PENDING_SCHOLAR_REVIEW (or flagged) — that would be the
    same silent self-certification the spec forbids, just with a human
    signature attached.
    """
    if current not in (
        ShariahReviewStatus.PENDING_SCHOLAR_REVIEW,
        ShariahReviewStatus.SYSTEM_FLAGGED_NONCOMPLIANT,
    ):
        raise ShariahReviewError(
            f"Cannot {decision} a record in status {current.value!r}; only "
            "PENDING_SCHOLAR_REVIEW or SYSTEM_FLAGGED_NONCOMPLIANT may be "
            "reviewed by a human."
        )
    if decision not in ("approved", "rejected"):
        raise ValueError(f"Invalid decision {decision!r}")


def submit_human_review_instrument(
    session: Session,
    instrument_id: str,
    reviewer_id: str,
    decision: HumanDecision,
    notes: str = "",
) -> Instrument:
    """Approve or reject an *instrument*'s Shariah review.

    This is the only place in the codebase allowed to write
    ``ShariahReviewStatus.SCHOLAR_APPROVED`` / ``SCHOLAR_REJECTED``.
    """
    if not reviewer_id:
        raise ValueError("reviewer_id is required for human review")

    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise ValueError(f"Instrument {instrument_id!r} not found")

    _verify_decision_allowed(instrument.shariah_review_status, decision)

    new_status = (
        ShariahReviewStatus.SCHOLAR_APPROVED
        if decision == "approved"
        else ShariahReviewStatus.SCHOLAR_REJECTED
    )

    # Core-level update: intentionally bypasses the ORM validates guard.
    session.execute(
        update(Instrument)
        .where(Instrument.id == instrument_id)
        .values(
            shariah_review_status=new_status,
            shariah_reviewer_id=reviewer_id,
        )
    )

    session.add(
        LedgerEntry(
            id=str(uuid4()),
            entry_type=LedgerEntryType.COMPLIANCE_EVENT,
            instrument_id=instrument_id,
            payload={
                "event": "human_review",
                "decision": decision,
                "reviewer_id": reviewer_id,
                "notes": notes,
                "new_status": new_status.value,
            },
        )
    )
    session.commit()
    session.refresh(instrument)
    return instrument


def submit_human_review_document(
    session: Session,
    document_id: str,
    reviewer_id: str,
    decision: HumanDecision,
    notes: str = "",
) -> Document:
    """Approve or reject a *document*'s review status (human-only)."""
    if not reviewer_id:
        raise ValueError("reviewer_id is required for human review")

    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id!r} not found")

    _verify_decision_allowed(document.shariah_review_status, decision)

    new_status = (
        ShariahReviewStatus.SCHOLAR_APPROVED
        if decision == "approved"
        else ShariahReviewStatus.SCHOLAR_REJECTED
    )

    session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(
            shariah_review_status=new_status,
            shariah_reviewer_id=reviewer_id,
        )
    )

    session.add(
        LedgerEntry(
            id=str(uuid4()),
            entry_type=LedgerEntryType.COMPLIANCE_EVENT,
            document_id=document_id,
            payload={
                "event": "human_review",
                "actor": reviewer_id,
                "decision": decision,
                "notes": notes,
                "new_status": new_status.value,
            },
        )
    )
    session.commit()
    session.refresh(document)
    return document