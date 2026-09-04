"""Cap table computation: the current (or point-in-time) ownership picture
is always *derived* from the CapTableEvent log, never stored directly.

This is the actual mechanic that makes a cap table correct under dilution,
transfers, and cancellations, instead of a spreadsheet someone has to
remember to keep in sync. Every function here is pure -- given the same
events and the same as_of date, it always produces the same answer, which
is what makes "what did ownership look like on March 3rd" an answerable
question rather than a lost fact.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import CapTableEventType
from app.models.orm import CapTableEvent, Security


@dataclass
class HolderPosition:
    holder_id: str
    security_id: str
    shares: float


@dataclass
class CapTableSnapshot:
    as_of: datetime
    issuer_name: str
    positions: list[HolderPosition] = field(default_factory=list)
    shares_by_security: dict[str, float] = field(default_factory=dict)
    total_fully_diluted_shares: float = 0.0

    def ownership_by_holder(self) -> dict[str, float]:
        """Fully-diluted ownership percentage per holder, across all
        securities. 'Fully diluted' here means every outstanding share of
        every security counts in the denominator, including unexercised
        options and warrants -- the standard cap-table convention, and the
        reason a founder's percentage drops the moment an option pool is
        created, not only when options are actually exercised.
        """
        if self.total_fully_diluted_shares <= 0:
            return {}
        totals: dict[str, float] = defaultdict(float)
        for p in self.positions:
            totals[p.holder_id] += p.shares
        return {
            holder_id: round(shares / self.total_fully_diluted_shares * 100, 4)
            for holder_id, shares in totals.items()
        }


class CapTableError(ValueError):
    """Raised when the event log itself is inconsistent -- e.g. a transfer
    or cancellation that would take a holder's position negative. This is
    a data-integrity signal, not a business decision the caller should
    silently paper over."""


def compute_cap_table(
    session: Session, issuer_name: str, as_of: datetime | None = None
) -> CapTableSnapshot:
    as_of = as_of or datetime.now(timezone.utc)

    security_rows = session.execute(
        select(Security).where(Security.issuer_name == issuer_name)
    ).scalars().all()
    security_ids = {s.id for s in security_rows}
    if not security_ids:
        return CapTableSnapshot(as_of=as_of, issuer_name=issuer_name)

    events = session.execute(
        select(CapTableEvent)
        .where(
            CapTableEvent.security_id.in_(security_ids),
            CapTableEvent.effective_date <= as_of,
        )
        .order_by(CapTableEvent.effective_date, CapTableEvent.recorded_at)
    ).scalars().all()

    # (security_id, holder_id) -> shares. A plain dict replay, in effective
    # -date order, is the whole algorithm -- deliberately no shortcuts, so
    # the answer for any as_of date is trustworthy, not approximated.
    positions: dict[tuple[str, str], float] = defaultdict(float)

    for event in events:
        if event.event_type == CapTableEventType.ISSUANCE:
            if not event.holder_id:
                raise CapTableError(f"Issuance event {event.id} has no holder_id")
            positions[(event.security_id, event.holder_id)] += event.quantity

        elif event.event_type == CapTableEventType.TRANSFER:
            if not event.from_holder_id or not event.holder_id:
                raise CapTableError(
                    f"Transfer event {event.id} needs both from_holder_id and holder_id"
                )
            key_from = (event.security_id, event.from_holder_id)
            if positions[key_from] < event.quantity:
                raise CapTableError(
                    f"Transfer event {event.id} moves {event.quantity} shares but "
                    f"holder {event.from_holder_id!r} only has {positions[key_from]}"
                )
            positions[key_from] -= event.quantity
            positions[(event.security_id, event.holder_id)] += event.quantity

        elif event.event_type == CapTableEventType.CANCELLATION:
            if not event.from_holder_id:
                raise CapTableError(f"Cancellation event {event.id} has no from_holder_id")
            key = (event.security_id, event.from_holder_id)
            if positions[key] < event.quantity:
                raise CapTableError(
                    f"Cancellation event {event.id} cancels {event.quantity} shares but "
                    f"holder {event.from_holder_id!r} only has {positions[key]}"
                )
            positions[key] -= event.quantity

        elif event.event_type in (CapTableEventType.EXERCISE, CapTableEventType.CONVERSION):
            if not event.holder_id or not event.target_security_id:
                raise CapTableError(
                    f"{event.event_type.value} event {event.id} needs holder_id and "
                    "target_security_id"
                )
            key_source = (event.security_id, event.holder_id)
            if positions[key_source] < event.quantity:
                raise CapTableError(
                    f"{event.event_type.value} event {event.id} moves {event.quantity} "
                    f"shares but holder {event.holder_id!r} only has {positions[key_source]}"
                )
            positions[key_source] -= event.quantity
            positions[(event.target_security_id, event.holder_id)] += event.quantity

        else:  # pragma: no cover - guarded by the enum, defensive only
            raise CapTableError(f"Unknown event type {event.event_type!r}")

    result_positions = [
        HolderPosition(holder_id=h, security_id=s, shares=qty)
        for (s, h), qty in positions.items()
        if qty > 0
    ]
    shares_by_security: dict[str, float] = defaultdict(float)
    for p in result_positions:
        shares_by_security[p.security_id] += p.shares

    return CapTableSnapshot(
        as_of=as_of,
        issuer_name=issuer_name,
        positions=result_positions,
        shares_by_security=dict(shares_by_security),
        total_fully_diluted_shares=sum(shares_by_security.values()),
    )