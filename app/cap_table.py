"""Cap-table computation -- event-sourced positions, replayed at query time.

The demo's "Live cap table" panel (static/index.html) posts investors,
securities and issuance events, then reads a *replayed* position snapshot at
either a point in time (as_of) or the present. No ownership number is stored;
it is always derived from the event log. This is the whole point of the demo
panel ("recomputed by replaying an event log, not stored as a number").
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import CapTableEvent, Investor, Security


def replay_cap_table(
    session: Session,
    issuer_name: str,
    as_of: datetime | None = None,
) -> list[dict]:
    """Return per (holder, security) share positions for an issuer.

    Events are filtered to those effective on or before ``as_of`` (when given);
    otherwise all events are included (i.e. "now"). Only issuance events
    affect share counts in this MVP computation.
    """
    securities = session.execute(
        select(Security).where(Security.issuer_name == issuer_name)
    ).scalars().all()
    if not securities:
        return []

    security_ids = [s.id for s in securities]
    query = select(CapTableEvent).where(
        CapTableEvent.security_id.in_(security_ids),
        CapTableEvent.event_type == "issuance",
    )
    if as_of is not None:
        query = query.where(CapTableEvent.effective_date <= as_of)
    events = session.execute(query).scalars().all()

    if not events:
        return []

    holder_ids = {e.holder_id for e in events}
    holders = session.execute(
        select(Investor).where(Investor.id.in_(holder_ids))
    ).scalars().all()
    holder_names = {h.id: h.name for h in holders}

    by_security = {s.id: s for s in securities}

    # Sum shares per (holder, security).
    totals: dict[tuple[str, str], int] = {}
    for e in events:
        key = (e.holder_id, e.security_id)
        totals[key] = totals.get(key, 0) + e.quantity

    grand_total = sum(totals.values()) or 1

    positions = []
    for (holder_id, security_id), shares in sorted(totals.items()):
        sec = by_security[security_id]
        positions.append(
            {
                "holder_id": holder_id,
                "holder_name": holder_names.get(holder_id, holder_id),
                "security_name": sec.name,
                "security_type": sec.security_type,
                "shares": shares,
                "ownership_percent": round(shares / grand_total * 100.0, 4),
            }
        )
    return positions