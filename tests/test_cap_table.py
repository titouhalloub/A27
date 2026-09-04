"""Cap-table API tests -- the exact sequence the demo HTML performs.

The demo's "Live cap table" panel posts investors, securities and issuance
events, then reads a *replayed* snapshot both before and after the Series A
round via ``as_of``. These tests exercise that over HTTP against a scratch DB.
"""
from datetime import datetime, timedelta, timezone

import pytest

HEADERS = {"X-API-Key": "test-key-123"}


@pytest.fixture()
def cap_table_ids(client):
    """Create the demo company (founder common stock), return the IDs the
    demo stores in JS globals."""
    r = client.post("/investors", headers=HEADERS, json={
        "name": "Founder", "investor_type": "individual",
    })
    assert r.status_code == 201
    founder_id = r.json()["id"]

    r = client.post("/investors", headers=HEADERS, json={
        "name": "Series A Fund", "investor_type": "institution",
    })
    assert r.status_code == 201
    vc_id = r.json()["id"]

    r = client.post("/securities", headers=HEADERS, json={
        "issuer_name": "Demo Acme Inc", "name": "Common Stock",
        "security_type": "common", "authorized_shares": 10_000_000,
    })
    assert r.status_code == 201
    common_id = r.json()["id"]

    ids = {
        "client": client, "founder_id": founder_id, "vc_id": vc_id,
        "common_id": common_id,
    }

    # Founder issuance, 200 days ago (matches the HTML's timing).
    now = datetime.now(timezone.utc)
    before = (now - timedelta(days=200)).isoformat()
    r = client.post("/cap-table-events", headers=HEADERS, json={
        "security_id": common_id, "event_type": "issuance", "holder_id": founder_id,
        "quantity": 8_000_000, "effective_date": before,
    })
    assert r.status_code == 201
    return ids


def test_before_round_founder_owns_all(client, cap_table_ids):
    """An as_of date before the round shows only the founder's common stock."""
    ids = cap_table_ids
    before_iso = (datetime.now(timezone.utc) - timedelta(days=150)).isoformat()
    r = ids["client"].get(
        f"/cap-table/{'Demo Acme Inc'.replace(' ', '%20')}",
        headers=HEADERS, params={"as_of": before_iso},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["issuer_name"] == "Demo Acme Inc"
    assert len(data["positions"]) == 1
    pos = data["positions"][0]
    assert pos["holder_id"] == ids["founder_id"]
    assert pos["holder_name"] == "Founder"
    assert pos["security_name"] == "Common Stock"
    assert pos["shares"] == 8_000_000
    assert pos["ownership_percent"] == pytest.approx(100.0)


def test_after_round_splits_ownership(client, cap_table_ids):
    """After posting a Series A preferred issuance to the VC, ownership
    recomputes to founder 80% / VC 20% -- from the same event log."""
    ids = cap_table_ids

    r = client.post("/securities", headers=HEADERS, json={
        "issuer_name": "Demo Acme Inc", "name": "Series A Preferred",
        "security_type": "preferred", "authorized_shares": 5_000_000,
    })
    assert r.status_code == 201
    pref_id = r.json()["id"]

    after_iso = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    r = client.post("/cap-table-events", headers=HEADERS, json={
        "security_id": pref_id, "event_type": "issuance", "holder_id": ids["vc_id"],
        "quantity": 2_000_000, "price_per_share": 5.0, "effective_date": after_iso,
    })
    assert r.status_code == 201

    r = client.get("/cap-table/Demo%20Acme%20Inc", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    holder_pct = {p["holder_id"]: p["ownership_percent"] for p in data["positions"]}
    assert holder_pct[ids["founder_id"]] == pytest.approx(80.0)
    assert holder_pct[ids["vc_id"]] == pytest.approx(20.0)


def test_position_before_round_ignores_later_events_by_as_of(client, cap_table_ids):
    """The same events, but a mid-point snapshot, must still show 100% to the
    founder -- the Series A issuance has not happened yet as-of that date."""
    ids = cap_table_ids
    mid_iso = (datetime.now(timezone.utc) - timedelta(days=150)).isoformat()

    r = client.post("/securities", headers=HEADERS, json={
        "issuer_name": "Demo Acme Inc", "name": "Series A Preferred",
        "security_type": "preferred", "authorized_shares": 5_000_000,
    })
    pref_id = r.json()["id"]
    r = client.post("/cap-table-events", headers=HEADERS, json={
        "security_id": pref_id, "event_type": "issuance", "holder_id": ids["vc_id"],
        "quantity": 2_000_000, "effective_date": (
            datetime.now(timezone.utc) - timedelta(days=100)
        ).isoformat(),
    })
    assert r.status_code == 201

    r = client.get("/cap-table/Demo%20Acme%20Inc", headers=HEADERS,
                   params={"as_of": mid_iso})
    data = r.json()
    assert len(data["positions"]) == 1
    assert data["positions"][0]["holder_name"] == "Founder"
    assert data["positions"][0]["ownership_percent"] == pytest.approx(100.0)


def test_cap_table_event_requires_existing_security(client, cap_table_ids):
    r = client.post("/cap-table-events", headers=HEADERS, json={
        "security_id": "no-such-security", "event_type": "issuance",
        "holder_id": cap_table_ids["founder_id"], "quantity": 1000,
        "effective_date": datetime.now(timezone.utc).isoformat(),
    })
    assert r.status_code == 404


def test_unknown_issuer_returns_empty(client):
    r = client.get("/cap-table/No%20Such%20Issuer", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["positions"] == []