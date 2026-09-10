"""Phase A capital-call endpoint tests -- approval parity with the cap-table
proposal gate: create, review (approve/reject), list, overdue.

Auth is mocked (the `client` fixture accepts `test-key-123`); the real
auth-gate logic is covered in test_api.py.
"""
from __future__ import annotations

import pytest

from app.models.enums import CapitalCallStatus

HEADERS = {"X-API-Key": "test-key-123"}


def _make_instrument(client):
    return client.post(
        "/instruments",
        headers=HEADERS,
        json={
            "transaction_type": "private_equity",
            "compliance_mode": "traditional",
            "issuer_name": "Northwind Ventures",
            "issuer_type": "Fund",
            "amount": 10_000_000,
            "currency": "USD",
        },
    )


def _make_investor(client, name="Dana White", investor_type="individual"):
    return client.post(
        "/investors",
        headers=HEADERS,
        json={"name": name, "investor_type": investor_type},
    )


def _create_call(client, instrument_id, funder_name=None, **overrides):
    payload = {
        "funder_name": funder_name or "Unresolved Fund",
        "currency": "USD",
        "capital_owing": 500_000.0,
        "due_date": "2026-12-31T00:00:00",
        "wire_details": "Wire to Northwind Ventures, ABA 021000021",
        "source_text": "Capital Call Notice -- Series A Preferred",
    }
    payload.update(overrides)
    return client.post(
        f"/instruments/{instrument_id}/capital-calls",
        headers=HEADERS,
        json=payload,
    )
# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def test_create_capital_call_resolves_funder_by_name(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    r = _create_call(client, inst["id"], funder_name="Dana White")
    assert r.status_code == 201
    body = r.json()
    assert body["funder_id"] is not None
    # Read-side resolution: the funder's registry name rides along so
    # clients never have to map a funder_id UUID themselves.
    assert body["funder_name"] == "Dana White"
    assert body["status"] == CapitalCallStatus.PENDING_APPROVAL.value
    assert body["requires_manual_review"] is False
    assert body["amount_due"] == 500_000.0


def test_create_capital_call_leaves_funder_unresolved_when_not_in_registry(client):
    inst = _make_instrument(client).json()
    r = _create_call(client, inst["id"], funder_name="ghost-investor")
    assert r.status_code == 201
    body = r.json()
    assert body["funder_id"] is None
    assert body["funder_name"] is None
    assert body["status"] == CapitalCallStatus.PENDING_APPROVAL.value
    assert body["requires_manual_review"] is True


def test_create_capital_call_without_funder_name_is_unresolved(client):
    inst = _make_instrument(client).json()
    r = _create_call(client, inst["id"], funder_name=None)
    assert r.status_code == 201
    assert r.json()["funder_id"] is None
    assert r.json()["requires_manual_review"] is True
# --------------------------------------------------------------------------- #
# Review -- approve
# --------------------------------------------------------------------------- #


def test_approve_capital_call_with_resolved_funder(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    call = _create_call(client, inst["id"], funder_name="Dana White").json()
    r = client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "Alice Auditor", "action": "approve"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == CapitalCallStatus.APPROVED.value
    assert body["funder_name"] == "Dana White"
    ledger = client.get(f"/instruments/{inst['id']}/ledger", headers=HEADERS).json()
    review_entries = [e for e in ledger if e["entry_type"] == "capital_call_review"]
    assert review_entries
    assert review_entries[-1]["payload"]["event"] == "capital_call_approved"
    assert review_entries[-1]["payload"]["reviewer"] == "Alice Auditor"


def test_approve_blocks_when_funder_unresolved(client):
    inst = _make_instrument(client).json()
    call = _create_call(client, inst["id"], funder_name="ghost-investor").json()
    r = client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "Alice Auditor", "action": "approve"},
    )
    assert r.status_code == 409
    assert "no resolved funder" in r.json()["detail"]


def test_approve_blocks_on_already_decided_call(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    call = _create_call(client, inst["id"], funder_name="Dana White").json()
    client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "Alice Auditor", "action": "approve"},
    )
    r = client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "Bob Reviewer", "action": "reject"},
    )
    assert r.status_code == 409
    assert "not pending_approval" in r.json()["detail"]


def test_approve_blocks_when_no_reviewer_named(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    call = _create_call(client, inst["id"], funder_name="Dana White").json()
    # Empty string is rejected at schema validation (422, min_length=1)...
    r = client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "", "action": "approve"},
    )
    assert r.status_code == 422
    # ...while whitespace-only survives Pydantic and hits the route's 409 guard.
    r = client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "   ", "action": "approve"},
    )
    assert r.status_code == 409
    assert "named reviewer" in r.json()["detail"]


def test_approve_blocks_for_nonexistent_call(client):
    r = client.post(
        "/capital-calls/does-not-exist/review",
        headers=HEADERS,
        json={"reviewer": "Alice", "action": "approve"},
    )
    assert r.status_code == 404
# --------------------------------------------------------------------------- #
# Review -- reject
# --------------------------------------------------------------------------- #


def test_reject_capital_call(client):
    inst = _make_instrument(client).json()
    call = _create_call(client, inst["id"]).json()
    r = client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "Alice Auditor", "action": "reject"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == CapitalCallStatus.REJECTED.value
    ledger = client.get(f"/instruments/{inst['id']}/ledger", headers=HEADERS).json()
    review_entries = [e for e in ledger if e["entry_type"] == "capital_call_review"]
    assert review_entries
    assert review_entries[-1]["payload"]["event"] == "capital_call_rejected"


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #


def test_list_capital_calls_filtered_by_status(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    pending = _create_call(client, inst["id"], funder_name="Dana White").json()
    _create_call(client, inst["id"], funder_name="ghost-investor")

    r = client.get(
        "/capital-calls",
        headers=HEADERS,
        params={"status": CapitalCallStatus.PENDING_APPROVAL.value},
    )
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()}
    assert pending["id"] in ids
    assert len(r.json()) >= 2

    client.post(
        f"/capital-calls/{pending['id']}/review",
        headers=HEADERS,
        json={"reviewer": "Alice", "action": "approve"},
    )
    r = client.get(
        "/capital-calls",
        headers=HEADERS,
        params={"status": CapitalCallStatus.APPROVED.value},
    )
    assert r.status_code == 200
    assert all(c["status"] == CapitalCallStatus.APPROVED.value for c in r.json())
    # The approved call resolves to its registry name on the list too.
    approved = next(c for c in r.json() if c["id"] == pending["id"])
    assert approved["funder_name"] == "Dana White"
    # The unresolved one stays nameless even in a queue listing.
    r = client.get("/capital-calls", headers=HEADERS)
    unresolved = next(c for c in r.json() if c["funder_id"] is None)
    assert unresolved["funder_name"] is None


def test_list_capital_calls_no_filter_returns_all(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    _create_call(client, inst["id"], funder_name="Dana White")
    r = client.get("/capital-calls", headers=HEADERS)
    assert r.status_code == 200
    assert len(r.json()) >= 1
# --------------------------------------------------------------------------- #
# Overdue
# --------------------------------------------------------------------------- #


def test_overdue_returns_pending_calls_with_passed_due_date(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    call = _create_call(
        client,
        inst["id"],
        funder_name="Dana White",
        due_date="2020-01-01T00:00:00",
    ).json()
    r = client.get("/capital-calls/overdue", headers=HEADERS)
    assert r.status_code == 200
    assert call["id"] in {c["id"] for c in r.json()}


def test_overdue_excludes_approved_calls(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    call = _create_call(
        client,
        inst["id"],
        funder_name="Dana White",
        due_date="2020-01-01T00:00:00",
    ).json()
    client.post(
        f"/capital-calls/{call['id']}/review",
        headers=HEADERS,
        json={"reviewer": "Alice", "action": "approve"},
    )
    r = client.get("/capital-calls/overdue", headers=HEADERS)
    assert r.status_code == 200
    assert call["id"] not in {c["id"] for c in r.json()}


def test_overdue_accepts_as_of(client):
    inst = _make_instrument(client).json()
    call = _create_call(
        client,
        inst["id"],
        due_date="2025-06-15T00:00:00",
        funder_name="Dana White",
    ).json()
    r = client.get(
        "/capital-calls/overdue",
        headers=HEADERS,
        params={"as_of": "2025-06-01T00:00:00"},
    )
    assert r.status_code == 200
    assert call["id"] not in {c["id"] for c in r.json()}
    r = client.get(
        "/capital-calls/overdue",
        headers=HEADERS,
        params={"as_of": "2025-07-01T00:00:00"},
    )
    assert r.status_code == 200
    assert call["id"] in {c["id"] for c in r.json()}


def test_overdue_returns_oldest_first(client):
    inst = _make_instrument(client).json()
    _make_investor(client, name="Dana White")
    early = _create_call(
        client,
        inst["id"],
        funder_name="Dana White",
        due_date="2020-01-01T00:00:00",
    ).json()
    late = _create_call(
        client,
        inst["id"],
        funder_name="Dana White",
        due_date="2020-12-31T00:00:00",
    ).json()
    r = client.get("/capital-calls/overdue", headers=HEADERS)
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()]
    assert ids.index(early["id"]) < ids.index(late["id"])