"""Security: PII (KYC/AML) protection, encryption at rest, retention."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from app.config import settings
from app.models.enums import DocumentType


class PIISecurityError(RuntimeError):
    pass


PII_DOCUMENT_TYPES = frozenset({DocumentType.KYC})


def _get_fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise PIISecurityError(
            "cryptography not installed; add it to dependencies to store PII."
        ) from exc

    key = settings.document_encryption_key
    if not key:
        raise PIISecurityError(
            "A27_DOCUMENT_ENCRYPTION_KEY is not set. KYC/AML documents must be "
            "encrypted at rest before they can be stored. Generate with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise PIISecurityError(f"Invalid Fernet key: {exc}") from exc


def is_pii_document(document_type: DocumentType) -> bool:
    return document_type in PII_DOCUMENT_TYPES


def encrypt_pii(text: str) -> str:
    if not text:
        return text
    return _get_fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_pii(token: str) -> str:
    if not token:
        return token
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise PIISecurityError(f"Failed to decrypt PII payload: {exc}") from exc


def require_pii_role(role: str) -> None:
    if role not in {"kyc_reviewer", "admin"}:
        raise PIISecurityError(
            f"Role {role!r} is not permitted to access PII documents."
        )


def retention_deadline() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=settings.pii_retention_days)


def is_retention_expired(uploaded_at: datetime) -> bool:
    if uploaded_at.tzinfo is None:
        uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
    return uploaded_at < retention_deadline()


def get_expired_pii_documents(documents: Iterable[Any]) -> list[Any]:
    return [
        doc
        for doc in documents
        if is_pii_document(doc.document_type) and is_retention_expired(doc.uploaded_at)
    ]


def expunge_expired_pii(
    documents: Iterable[Any],
    storage_backend: Callable[[str], None],
    delete_row: Callable[[Any], None],
) -> list[str]:
    purged = []
    for doc in documents:
        storage_backend(doc.file_url)
        delete_row(doc)
        purged.append(doc.id)
    return purged