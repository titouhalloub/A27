"""File-upload intake -- real documents (PDF/TXT/HTML/images) through the
same classify -> extract -> comply -> ledger pipeline as pasted text. The
OCR fallback is exercised via fakes so the suite never needs the Tesseract
binary installed."""
import io
import sys

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
    # The deal container must be backfilled from the extracted record -- not
    # left at creation-time placeholders ("Upload Test Issuer" / 1,000,000 USD).
    inst = body["instrument"]
    assert inst["issuer_name"] == "Alpha Manufacturing Sdn Bhd"
    assert inst["amount"] == 2_500_000.0


SUKUK_TEXT = """SUKUK CERTIFICATE
Issuer: Petra Energy Sukuk SPV
Total Issue Size: USD 500,000,000
Structure: al-Ijara
Profit Rate: 4.25% per annum"""


def test_upload_sukuk_backfills_deal_container_and_corrects_txn(client):
    # The deal container was created as a loan; the document is a sukuk.
    iid = _make_instrument(client, mode="traditional", txn="loan")
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("sukuk.txt", SUKUK_TEXT.encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 200
    body = r.json()
    inst = body["instrument"]
    # Backfilled from the extracted record, not creation-time placeholders.
    assert inst["issuer_name"] == "Petra Energy Sukuk SPV"
    assert inst["amount"] == 500_000_000.0
    assert inst["currency"] == "USD"
    # The confident classification corrected the container's transaction type.
    assert inst["transaction_type"] == "sukuk"


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
    def fake_extract(path, *, ocr=None, meta=None):
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


def test_upload_image_without_ocr_installed_fails_loudly(client, monkeypatch):
    """The one gap the other review flagged: when pytesseract genuinely isn't
    importable, the upload must fail with a CLEAR 422 explaining that OCR
    isn't available -- never a silent wrong result and never a 500. We make
    the import fail the same way a missing install would."""
    import sys

    monkeypatch.setitem(sys.modules, "pytesseract", None)

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), color="white").save(buf, format="PNG")

    iid = _make_instrument(client)
    _attach_kyc(client, iid)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("scan-page.png", buf.getvalue(), "image/png")},
    )
    assert r.status_code == 422
    assert "pytesseract" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Bounded PDF intake -- the Railway 502 fix. A 300-page prospectus took ~32s
# of pdfplumber time and blew through the ~30s proxy timeout. Extraction now
# stops at MAX_PDF_PAGES / MAX_PDF_CHARS, and the response reports what was
# actually read. pdfplumber is faked (like pytesseract above) so the tests
# need no real multi-page PDF fixture.
# --------------------------------------------------------------------------- #


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakePdfPlumber:
    def __init__(self, pages):
        self._pages = pages

    def open(self, path):
        return _FakePdf(self._pages)


def test_pdf_page_cap_stops_at_limit(monkeypatch):
    """A 45-page PDF is read only up to MAX_PDF_PAGES, and meta says so."""
    import app.ocr as ocr

    pages = [_FakePage(f"marker-{i} Principal: USD 1,000,000") for i in range(45)]
    monkeypatch.setitem(sys.modules, "pdfplumber", _FakePdfPlumber(pages))

    text, meta = ocr.extract_text_meta("whatever.pdf")
    assert meta == {"pages_read": ocr.MAX_PDF_PAGES, "pages_total": 45}
    assert "marker-0 " in text  # first page read
    assert f"marker-{ocr.MAX_PDF_PAGES - 1} " in text  # last page read
    assert "marker-44" not in text  # pages past the cap never parsed


def test_pdf_char_cap_stops_at_limit(monkeypatch):
    """A PDF with enormous pages stops on the character cap, not the page
    count: 3 pages x 150k chars crosses MAX_PDF_CHARS (200k) on page 2."""
    import app.ocr as ocr

    pages = [_FakePage("A" * 150_000) for _ in range(3)]
    monkeypatch.setitem(sys.modules, "pdfplumber", _FakePdfPlumber(pages))

    text, meta = ocr.extract_text_meta("huge.pdf")
    assert meta == {"pages_read": 2, "pages_total": 3}
    assert len(text) < 400_000  # page 3 never parsed


def test_upload_pdf_reports_pages_read(client, monkeypatch):
    """End to end: the upload response carries pages_read/pages_total so the
    dashboard can say 'parsed the first 40 of 312 pages' instead of implying
    the whole document was read."""
    pages = [_FakePage(LOAN_TEXT) for _ in range(45)]
    monkeypatch.setitem(sys.modules, "pdfplumber", _FakePdfPlumber(pages))

    iid = _make_instrument(client)
    _attach_kyc(client, iid)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("prospectus.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pages_read"] == 40
    assert body["pages_total"] == 45
    assert body["document"]["document_type"] == "loan_agreement"


def test_small_pdf_reports_all_pages(client, monkeypatch):
    """A PDF under the caps is fully read -- and honestly reported as such."""
    pages = [_FakePage(LOAN_TEXT) for _ in range(3)]
    monkeypatch.setitem(sys.modules, "pdfplumber", _FakePdfPlumber(pages))

    iid = _make_instrument(client)
    _attach_kyc(client, iid)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("small.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pages_read"] == 3
    assert body["pages_total"] == 3


def test_tesseract_binary_missing_fails_clearly(monkeypatch, tmp_path):
    """The other Railway gap: the pytesseract package is installed but the
    OS binary is not. That must be a clear TextExtractionError naming the
    fix, not an unhandled EnvironmentError (which the endpoint would turn
    into an opaque 500)."""
    import types

    import app.ocr as ocr
    from PIL import Image

    class FakeTesseractNotFound(EnvironmentError):
        pass

    fake = types.ModuleType("pytesseract")
    fake.TesseractNotFoundError = FakeTesseractNotFound
    fake.image_to_string = lambda img: (_ for _ in ()).throw(FakeTesseractNotFound("tesseract is not installed"))
    monkeypatch.setitem(sys.modules, "pytesseract", fake)

    img_path = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), color="white").save(img_path, format="PNG")

    with pytest.raises(TextExtractionError) as excinfo:
        ocr._ocr_with_tesseract(img_path)
    assert "Tesseract binary" in str(excinfo.value)


def test_upload_persists_original_file(client, tmp_path, monkeypatch):
    """Uploaded originals must be saved under the uploads dir and the
    document's file_url must point at the stored file (never a mem://
    placeholder), so reviewers can always open the exact document that
    the pipeline processed."""
    from pathlib import Path

    from app.config import settings

    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path / "uploads"))
    iid = _make_instrument(client)
    _attach_kyc(client, iid)
    r = client.post(
        f"/instruments/{iid}/documents/upload",
        headers=HEADERS,
        files={"file": ("loan-agreement.txt", LOAN_TEXT.encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 200
    file_url = r.json()["document"]["file_url"]
    assert file_url.startswith(str(tmp_path / "uploads"))
    stored = Path(file_url)
    assert stored.exists()
    assert stored.read_bytes() == LOAN_TEXT.encode("utf-8")