"""Document text extraction — PDF text layer first, Tesseract OCR fallback.

Explicit MVP decision (spec section 9): Tesseract is free and lower-accuracy
on scanned documents than AWS Textract / Azure Document Intelligence. That
trade-off is accepted for proof-of-pipeline and is stated here — it is not a
silent discovery later.

- PDFs are read via their embedded text layer first (pdfplumber). A PDF with
  an empty text layer is treated as scanned and OCR'd.
- Images are OCR'd via pytesseract.
- The OCR engine is injectable so tests can use a fake, and environments
  without the Tesseract binary fail with a clear, actionable error.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable

OCRCallable = Callable[[Path], str]

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
_UPLOADABLE_SUFFIXES = {".txt", ".md", ".html", ".htm", ".pdf"} | _IMAGE_SUFFIXES

# MVP guard against accidental multi-gigabyte uploads, not a product limit.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

# Bounded PDF intake. Classification and extraction work on a text window,
# so pages past these caps add latency without adding signal -- a 300-page
# prospectus ran ~32s and blew through Railway's ~30s proxy timeout (502).
# Not silent truncation: the upload response reports pages_read/pages_total
# so the client can state exactly what was read.
MAX_PDF_PAGES = 40
MAX_PDF_CHARS = 200_000


class TextExtractionError(RuntimeError):
    """Raised when no text can be extracted; the caller routes to review."""


class UnsupportedFileType(ValueError):
    """Raised when an upload's type is not one we know how to parse."""


class UploadTooLarge(ValueError):
    """Raised when an upload exceeds MAX_UPLOAD_BYTES."""


def _ocr_with_tesseract(path: Path) -> str:
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise TextExtractionError(
            "OCR requested but pytesseract/Pillow are not installed."
        ) from exc
    try:
        return pytesseract.image_to_string(Image.open(str(path)))
    except pytesseract.TesseractNotFoundError as exc:
        # The Python package is installed but the OS binary is missing
        # (the case on a bare container). Fail with an actionable message,
        # not an unhandled EnvironmentError.
        raise TextExtractionError(
            "OCR requires the Tesseract binary, which is not installed on "
            "this server. Install it (e.g. 'apt-get install tesseract-ocr') "
            "or send a document with a text layer."
        ) from exc


def extract_text(path: str | Path, *, ocr: OCRCallable | None = None) -> str:
    """Return extracted text from a file.

    Raises ``TextExtractionError`` when no text can be extracted -- the caller
    must route the document to human review in that case.
    """
    return extract_text_meta(path, ocr=ocr)[0]


def extract_text_meta(
    path: str | Path, *, ocr: OCRCallable | None = None
) -> tuple[str, dict]:
    """Return ``(text, meta)`` where meta states exactly what was read from
    the file: ``pages_read`` / ``pages_total`` for PDFs (after the
    MAX_PDF_PAGES / MAX_PDF_CHARS caps), ``None`` for other types.

    Raises ``TextExtractionError`` when no text can be extracted -- the caller
    must route the document to human review in that case.
    """
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    text = ""
    meta: dict = {"pages_read": None, "pages_total": None}

    if suffix in {".txt", ".md", ".html", ".htm"}:
        text = file_path.read_text(encoding="utf-8", errors="replace")

    elif suffix == ".pdf":
        try:
            import pdfplumber  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise TextExtractionError("pdfplumber is not installed") from exc

        with pdfplumber.open(str(file_path)) as pdf:
            meta["pages_total"] = len(pdf.pages)
            pages_read = 0
            for page in pdf.pages:
                if pages_read >= MAX_PDF_PAGES or len(text) >= MAX_PDF_CHARS:
                    break
                text += (page.extract_text() or "") + "\n"
                pages_read += 1
        meta["pages_read"] = pages_read

        if not text.strip():
            text = _run_ocr(file_path, ocr)

    elif suffix in _IMAGE_SUFFIXES:
        text = _run_ocr(file_path, ocr)

    else:
        raise TextExtractionError(f"Unsupported file type: {suffix!r}")

    if not text.strip():
        raise TextExtractionError(f"No text could be extracted from {file_path.name}")
    return text, meta


def _run_ocr(path: Path, ocr_callback: OCRCallable | None) -> str:
    callback = ocr_callback or _ocr_with_tesseract
    try:
        return callback(path)
    except TextExtractionError:
        raise
    except Exception as exc:
        raise TextExtractionError(f"OCR failed on {path.name}: {exc}") from exc


def text_from_upload(
    upload, *, ocr: OCRCallable | None = None, meta: dict | None = None
) -> str:
    """Extract text from an uploaded file (FastAPI's UploadFile, or any
    object with a ``.filename`` and a binary ``.file``).

    If ``meta`` is a dict, it is updated in place with ``pages_read`` /
    ``pages_total`` so the caller can report what was actually parsed.

    The bytes are spooled to a real temp file first: pdfplumber and the
    Tesseract CLI both want a filesystem path. The size cap is enforced
    *during* the copy, so an oversized upload never lands on disk in full,
    and cleanup is guaranteed in the finally block. Raises
    ``UnsupportedFileType`` / ``UploadTooLarge`` / ``TextExtractionError`` --
    the caller decides how loudly to fail.
    """
    filename = upload.filename or "upload.bin"
    suffix = Path(filename).suffix.lower()
    if suffix not in _UPLOADABLE_SUFFIXES:
        raise UnsupportedFileType(
            f"Unsupported file type {suffix or '(no extension)'!r}. Supported: "
            ".txt, .md, .html, .pdf, and images (.png, .jpg, .jpeg, .tif, .tiff, .bmp)."
        )
    upload.file.seek(0)
    fd, tmp_name = tempfile.mkstemp(suffix=suffix)
    tmp_path = Path(tmp_name)
    try:
        copied = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_UPLOAD_BYTES:
                    raise UploadTooLarge(
                        f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."
                    )
                out.write(chunk)
        text, page_meta = extract_text_meta(tmp_path, ocr=ocr)
        if meta is not None:
            meta.update(page_meta)
        return text
    finally:
        tmp_path.unlink(missing_ok=True)