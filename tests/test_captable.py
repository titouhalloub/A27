"""Tests for the actual cap table mechanic: event-sourced ownership,
point-in-time computation, and dilution across rounds.

This is deliberately tested independently of the API layer -- the
computation is pure and the thing whose correctness actually matters.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.captable import CapTableError, compute_cap_table
from app.models.enums import CapTableEventType, InvestorType, SecurityType
from app.models.orm import CapTableEvent, Investor, Security


def _dt(days_from_now: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days_from_now)


def _make_investor(session, name: str, investor_type: str = "individual") -> Investor:
    inv = Investor(
        id=f"inv-{name}", name=name, investor_type=InvestorType(investor_type)
    )
    session.add(inv)
    session.flush()
    return inv


def _make_security(session, issuer: str, name: str, sec_type: SecurityType, authorized: float) -> Security:
    sec = Security(
        id=f"sec-{issuer}-{name}".replace(" ", "-"),
        issuer_name=issuer, name=name, security_type=sec_type, authorized_shares=authorized,
    )
    session.add(sec)
    session.flush()
    return sec


def _issue(session, security, holder, quantity, effective_date, price=None):
    event = CapTableEvent(
        id=f"evt-{security.id}-{holder.id}-{quantity}-{effective_date.isoformat()}",
        security_id=security.id, event_type=CapTableEventType.ISSUANCE,
        holder_id=holder.id, quantity=quantity, price_per_share=price,
        effective_date=effective_date,
    )
    session.add(event)
    session.flush()
    return event


def test_single_founder_issuance_is_100_percent(session):
    founder = _make_investor(session, "founder")
    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    _issue(session, common, founder, 8_000_000, _dt(-100))

    snap = compute_cap_table(session, "Acme Inc")
    assert snap.total_fully_diluted_shares == 8_000_000
    assert snap.ownership_by_holder() == {founder.id: 100.0}


def test_series_a_dilutes_founder(session):
    """The actual claim being tested: founder starts at 100%, a new
    investor buys newly-issued preferred shares, and the founder's
    percentage drops -- computed, not hand-entered -- exactly the
    mechanic a real cap table product has to get right."""
    founder = _make_investor(session, "founder")
    series_a_investor = _make_investor(session, "series-a-vc", "institution")

    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    preferred = _make_security(session, "Acme Inc", "Series A Preferred", SecurityType.PREFERRED, 5_000_000)

    _issue(session, common, founder, 8_000_000, _dt(-200))
    # Before the round: founder is 100%.
    snap_before = compute_cap_table(session, "Acme Inc", as_of=_dt(-150))
    assert snap_before.ownership_by_holder() == {founder.id: 100.0}

    _issue(session, preferred, series_a_investor, 2_000_000, _dt(-100), price=5.0)

    snap_after = compute_cap_table(session, "Acme Inc", as_of=_dt(-50))
    ownership = snap_after.ownership_by_holder()
    assert snap_after.total_fully_diluted_shares == 10_000_000
    assert ownership[founder.id] == 80.0
    assert ownership[series_a_investor.id] == 20.0


def test_option_pool_dilutes_before_any_exercise(session):
    """Fully-diluted convention: an unexercised option pool still dilutes
    everyone else the moment it's issued, not only once exercised."""
    founder = _make_investor(session, "founder")
    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    pool = _make_security(session, "Acme Inc", "2026 Option Pool", SecurityType.OPTION, 2_000_000)

    _issue(session, common, founder, 8_000_000, _dt(-100))
    _issue(session, pool, founder, 2_000_000, _dt(-90))  # pool "held" by founder/company pre-grant, MVP simplification

    snap = compute_cap_table(session, "Acme Inc")
    assert snap.total_fully_diluted_shares == 10_000_000
    assert snap.ownership_by_holder()[founder.id] == 100.0  # still one holder, but total denominator grew


def test_exercise_moves_shares_from_option_to_common_same_holder(session):
    founder = _make_investor(session, "founder")
    employee = _make_investor(session, "employee")
    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    pool = _make_security(session, "Acme Inc", "2026 Option Pool", SecurityType.OPTION, 2_000_000)

    _issue(session, common, founder, 8_000_000, _dt(-100))
    _issue(session, pool, employee, 500_000, _dt(-90))

    exercise = CapTableEvent(
        id="evt-exercise-1", security_id=pool.id, target_security_id=common.id,
        event_type=CapTableEventType.EXERCISE, holder_id=employee.id,
        quantity=500_000, price_per_share=0.10, effective_date=_dt(-10),
    )
    session.add(exercise)
    session.flush()

    snap = compute_cap_table(session, "Acme Inc")
    by_security = {(p.security_id, p.holder_id): p.shares for p in snap.positions}
    assert (pool.id, employee.id) not in by_security  # fully exercised, no option shares left
    assert by_security[(common.id, employee.id)] == 500_000
    # Total fully-diluted shares unchanged -- exercise moves shares between
    # securities, it doesn't create or destroy them.
    assert snap.total_fully_diluted_shares == 8_500_000


def test_transfer_moves_shares_between_holders(session):
    seller = _make_investor(session, "seller")
    buyer = _make_investor(session, "buyer")
    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    _issue(session, common, seller, 1_000_000, _dt(-100))

    transfer = CapTableEvent(
        id="evt-transfer-1", security_id=common.id, event_type=CapTableEventType.TRANSFER,
        holder_id=buyer.id, from_holder_id=seller.id, quantity=400_000, effective_date=_dt(-10),
    )
    session.add(transfer)
    session.flush()

    snap = compute_cap_table(session, "Acme Inc")
    ownership = snap.ownership_by_holder()
    assert ownership[seller.id] == 60.0
    assert ownership[buyer.id] == 40.0


def test_cancellation_reduces_holder_shares(session):
    founder = _make_investor(session, "founder")
    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    _issue(session, common, founder, 1_000_000, _dt(-100))

    cancel = CapTableEvent(
        id="evt-cancel-1", security_id=common.id, event_type=CapTableEventType.CANCELLATION,
        from_holder_id=founder.id, quantity=200_000, effective_date=_dt(-10),
    )
    session.add(cancel)
    session.flush()

    snap = compute_cap_table(session, "Acme Inc")
    assert snap.total_fully_diluted_shares == 800_000


def test_overdraft_transfer_raises_captable_error(session):
    """Data-integrity guard: a transfer that would take a holder negative
    is a real inconsistency in the event log, not something to silently
    clamp to zero and hide."""
    seller = _make_investor(session, "seller")
    buyer = _make_investor(session, "buyer")
    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    _issue(session, common, seller, 100_000, _dt(-100))

    bad_transfer = CapTableEvent(
        id="evt-bad-transfer", security_id=common.id, event_type=CapTableEventType.TRANSFER,
        holder_id=buyer.id, from_holder_id=seller.id, quantity=999_999, effective_date=_dt(-10),
    )
    session.add(bad_transfer)
    session.flush()

    with pytest.raises(CapTableError):
        compute_cap_table(session, "Acme Inc")


def test_events_after_as_of_date_are_excluded(session):
    """The actual point-in-time claim: an event dated in the future
    relative to as_of must not affect the snapshot."""
    founder = _make_investor(session, "founder")
    investor = _make_investor(session, "later-investor")
    common = _make_security(session, "Acme Inc", "Common Stock", SecurityType.COMMON, 10_000_000)
    preferred = _make_security(session, "Acme Inc", "Series A", SecurityType.PREFERRED, 5_000_000)

    _issue(session, common, founder, 1_000_000, _dt(-100))
    _issue(session, preferred, investor, 500_000, _dt(50))  # dated in the future

    snap = compute_cap_table(session, "Acme Inc", as_of=_dt(0))
    assert snap.total_fully_diluted_shares == 1_000_000
    assert investor.id not in snap.ownership_by_holder()


def test_empty_issuer_returns_empty_snapshot(session):
    snap = compute_cap_table(session, "Nonexistent Co")
    assert snap.total_fully_diluted_shares == 0
    assert snap.positions == []