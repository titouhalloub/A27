"""File-upload intake -- real documents (PDF/TXT/HTML/images) through the
same classify -> extract -> comply -> ledger pipeline as pasted text. The
OCR fallback is exercised via fakes so the suite never needs the Tesseract
binary installed."""
import io

import pytest

import app.ocr as ocr_module
from app.ocr import TextExtractionError

HEADERS = {"X-API-Key": "test-key-123"}

LOAN_TEXT = """LOAN AGREEMENT
Borrower: Alpha Manufacturing Sdn Bhd
Lender: Meridian Bank Ltd
Principal: USD 2,500,000
Interest Rate: 6.5%
Repayment: quarterly amortization over 5 years
Maturity Date: 2029-12-31
Governing Law: English law
"""


def _make_instrument(client, mode: str = "traditional", txn: str = "loan") -> str:
    r = client.post("/instruments", headers=HEADERS, json={
        "transaction_type": txn, "compliance_mode": mode,
        "issuer_name": "Upload Test Issuer", "issuer_type": "Corporate",
        "amount": 1_000_000, "currency": "USD",
    })
    assert r.status_code == 201
    return r.json()["id"]


def _attach_kyc(client, iid: str) -> None:
    """A traditional instrument without a KYC document is correctly flagged
    (TRAD_KYC_MISSING is blocking) -- attach one so a clean upload can pass."""
    r = client.post(f"/instruments/{iid}/evidence", headers=HEADERS, json={
        "text": "KYC/AML verification on record for the instrument parties.",
        "document_type": "kyc", "filename": "kyc-pack.pdf",
    })
    assert r.status_code == 201


def test_upload_txt_runs_the_full_pipeline(client):
    iid = _make_instrument(client)
    _attach_kyc(client, iid)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("loan-agreement.txt", LOAN_TEXT.encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"]["document_type"] == "loan_agreement"
    assert body["document"]["filename"] == "loan-agreement.txt"
    assert body["document"]["status"] == "processed"
    assert body["outcome"] == "not_applicable"
    assert "LOAN AGREEMENT" in body["extracted_text"]


def test_upload_unsupported_type_rejected(client):
    iid = _make_instrument(client)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("payload.exe", b"MZfakebinary", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.json()["detail"]


def test_upload_oversized_rejected(client, monkeypatch):
    monkeypatch.setattr(ocr_module, "MAX_UPLOAD_BYTES", 64)
    iid = _make_instrument(client)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("big.txt", b"x" * 200, "text/plain")},
    )
    assert r.status_code == 413


def test_upload_unreadable_document_fails_loudly(client, monkeypatch):
    """No text layer + no OCR -> a loud 422 with the reason. Never a silent
    guess, never a 500."""
    def fake_extract(path, *, ocr=None):
        raise TextExtractionError("No text could be extracted from scan.pdf")

    monkeypatch.setattr("app.main.text_from_upload", fake_extract)
    iid = _make_instrument(client)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("scan.pdf", b"%PDF-1.4 not really", "application/pdf")},
    )
    assert r.status_code == 422
    assert "No text could be extracted" in r.json()["detail"]


def test_upload_scanned_image_goes_through_ocr(client, monkeypatch):
    """A scanned page: the image branch runs, the OCR callback's text feeds
    the same pipeline, and the response carries what was actually read."""
    from PIL import Image

    def fake_tesseract(path):
        return LOAN_TEXT

    monkeypatch.setattr(ocr_module, "_ocr_with_tesseract", fake_tesseract)
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), color="white").save(buf, format="PNG")

    iid = _make_instrument(client)
    _attach_kyc(client, iid)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("scan-page.png", buf.getvalue(), "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"]["document_type"] == "loan_agreement"
    assert "LOAN AGREEMENT" in body["extracted_text"]